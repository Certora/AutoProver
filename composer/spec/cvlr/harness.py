"""A CVLR harness module, written into the crate under verification.

The prover sees it only as part of that crate's build. The artifact store
writes it under the package's ``src/certora/specs/``
(:mod:`composer.spec.cvlr.scaffold`). The modules are ``pub`` because the
program's own file reaches into ``specs``
(:func:`composer.spec.cvlr.scaffold._harness_files`).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import override

from pydantic import BaseModel, Field

from composer.authoring.state import SkippedProperty
from composer.cargo.features import CargoFeature
from composer.diagnostics.timing import RunSummary
from composer.prover.conf import Conf, dump_conf
from composer.spec.artifacts import ArtifactStore
from composer.spec.cvlr.conf import TunableConf
from composer.spec.cvlr.scaffold import SPECS_DIR
from composer.spec.cvlr.state import PropertyRuleMapping, RuleSubject
from composer.spec.cvlr.tuning import SummaryDirective
from composer.spec.gen_types import CVLR_DELIVERABLE_DIR, CVLR_INTERNAL_DIR
from composer.spec.source.munge.edit_store import AppliedEdit
from composer.spec.types import CheckName, PropertyTitle, RuleName
from composer.spec.util import ensure_dir

#: One prover conf per delivered unit, for rerunning its rules.
CONFS_DIR = CVLR_DELIVERABLE_DIR / "confs"


@dataclass(frozen=True)
class HarnessModule:
    #: A ``snake_case`` module name, from
    #: :func:`~composer.spec.util.slugify_rust_module`. Unique within the program.
    module: str

    @property
    def stem(self) -> str:
        return f"cvlr_{self.module}"

    @property
    def feature(self) -> CargoFeature:
        """The cargo feature that compiles this unit, and only this unit.

        The name is prefixed with ``certora_unit_``. It shares a namespace with the
        project's own features. A module such as ``serde`` would otherwise select
        the project's feature.
        """
        return CargoFeature(f"certora_unit_{self.module}")

    @property
    def artifact_file(self) -> str:
        return f"{self.module}.rs"


class GeneratedHarness(BaseModel):
    """A published harness: the module source, what it claims, and where it ran."""

    commentary: str
    harness: str
    skipped: list[SkippedProperty] = Field(default_factory=list)
    property_rules: list[PropertyRuleMapping] = Field(default_factory=list)
    #: What each rule drives: a program function, or harness code standing in for
    #: one. A verdict on a stand-in is not a verdict on the program.
    rule_subjects: list[RuleSubject] = Field(default_factory=list)
    #: Points-to summaries the author added. Each replaces a function with a
    #: stand-in, so the verdicts depend on the summary being sound here.
    summaries: list[SummaryDirective] = Field(default_factory=list)
    #: The editor's account of each edit to the program's source. The verdicts
    #: are about the edited program. The report reads these as ``source_edits``.
    applied_edits: list[AppliedEdit] = Field(default_factory=list)
    #: The edited files, whole, by project-relative path. The report diffs these
    #: against the project.
    vfs: dict[str, str] = Field(default_factory=dict)
    #: The rules the last verifying build listed in its ``.cvlr_rules`` section. Empty when no
    #: build listed them, which is also what a result cached before this field loads as.
    declared_rules: list[RuleName] = Field(default_factory=list)
    #: The conf settings the published harness was checked under. ``None`` on a result cached
    #: before they were recorded.
    settings: TunableConf | None = None
    final_link: str | None = None

    def property_checks(self) -> list[tuple[PropertyTitle, list[CheckName]]]:
        return [(m.property_title, list(m.rules)) for m in self.property_rules]

    @property
    def artifact_text(self) -> str:
        return self.harness

    @property
    def output_link(self) -> str | None:
        return self.final_link


class CvlrArtifactStore(ArtifactStore[HarnessModule, GeneratedHarness]):
    """Writes CVLR harnesses into the verified package, and everything else under ``certora/cvlr/``.

    ``package_dir`` is that package relative to the project root. In a workspace
    it is not the project root, and the harness still has to be written into the package.
    """

    def __init__(self, project_root: str | Path, package_dir: Path) -> None:
        super().__init__(
            project_root,
            "property_rules",
            deliverable_dir=CVLR_DELIVERABLE_DIR,
            internal_dir=CVLR_INTERNAL_DIR,
            report_dir=CVLR_DELIVERABLE_DIR / "reports",
        )
        self._package_dir = package_dir

    @override
    def _artifact_dir(self) -> Path:
        return ensure_dir(Path(self._project_root) / self._package_dir / SPECS_DIR)

    @override
    def _job_info_payload(
        self, summary: RunSummary, *, user_id: str, run_mode: str
    ) -> dict[str, object]:
        """Adds the prover-reported runtime to the shared manifest.

        Same field the autoprove store records. A manifest of model tokens alone
        cannot separate a run that spent its time in the prover from one that
        spent it in the model.
        """
        return {
            **super()._job_info_payload(summary, user_id=user_id, run_mode=run_mode),
            "prover_usage": summary.prover_usage_summary(),
        }

    def write_confs(self, confs: dict[HarnessModule, Conf]) -> dict[HarnessModule, Path]:
        """Write each unit's conf under :data:`CONFS_DIR`, and return the paths, project-relative.

        A conf this call does not write is removed, so a resumed run does not keep one for a unit
        it did not deliver.
        """
        directory = Path(self._project_root) / CONFS_DIR
        written: dict[HarnessModule, Path] = {}
        if confs:
            directory.mkdir(parents=True, exist_ok=True)
            for unit, conf in confs.items():
                path = directory / f"{unit.module}.conf"
                path.write_text(dump_conf(conf))
                written[unit] = path.relative_to(self._project_root)
        if directory.is_dir():
            keep = {p.name for p in written.values()}
            for stale in directory.glob("*.conf"):
                if stale.name not in keep:
                    stale.unlink()
            if not any(directory.iterdir()):
                directory.rmdir()
        return written

    def declare_modules(self, modules: list[HarnessModule]) -> tuple[Path, ...]:
        """Write ``specs/mod.rs``, with every unit's module behind its cargo feature.

        Called once, before any unit stages a draft, so the file names every unit.
        rustc does not read a module whose ``cfg`` is disabled, and that module
        does not enter dep-info: a missing file does not fail another unit's
        build, and one unit's draft does not rebuild another's. The units are
        never compiled together, so two ``impl Nondet for Foo`` do not collide
        (``E0119``).

        Names are unique. Component analysis rejects sibling components whose
        slugs collide.
        """
        target = self._artifact_dir()
        declarations = "".join(
            f'#[cfg(feature = "{m.feature}")]\npub mod {m.module};\n'
            for m in sorted(modules, key=lambda m: m.module)
        )
        mod_rs = target / "mod.rs"
        mod_rs.write_text(
            "//! The rules. One module per unit, each gated on its own cargo feature, so a\n"
            "//! build selects exactly one unit's rules.\n"
            "\n" + declarations
        )
        return (mod_rs,)
