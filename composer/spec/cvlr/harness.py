"""A CVLR harness module, and where it is written.

A CVLR harness is a Rust module inside the crate under verification. It
compiles only from there, and the prover reaches it through the build. The
artifact store writes it into the package's ``src/certora/specs/``
(:mod:`composer.spec.cvlr.scaffold`).

``specs/mod.rs`` declares one ``pub mod <name>;`` per unit, each behind that
unit's cargo feature. A module behind a disabled ``cfg`` is never read by
rustc, so a unit that has not written its file yet does not stop another unit
from compiling. The modules are ``pub`` because a path into ``specs`` has to
be visible from the program's own file
(:func:`composer.spec.cvlr.scaffold._harness_files`).
:class:`composer.pipeline.core.StagedFormalizer` declares every module before
any unit writes.
"""

import dataclasses
from pathlib import Path
from typing import override

from pydantic import BaseModel, Field

from composer.authoring.state import SkippedProperty
from composer.cargo.features import CargoFeature
from composer.diagnostics.timing import RunSummary
from composer.spec.artifacts import ArtifactStore
from composer.spec.cvlr.scaffold import SPECS_DIR
from composer.spec.cvlr.state import PropertyRuleMapping, RuleSubject
from composer.spec.cvlr.tuning import SummaryDirective
from composer.spec.source.munge.edit_store import AppliedEdit
from composer.spec.types import CheckName, PropertyTitle
from composer.spec.util import ensure_dir

#: Reports and diagnostics. The harness source is written into the crate.
DELIVERABLE_DIR = Path("certora") / "cvlr"
INTERNAL_DIR = Path(".certora_internal") / "cvlr"

@dataclasses.dataclass(frozen=True)
class HarnessModule:
    #: The unit's slug. The Rust ecosystems produce it with
    #: :func:`~composer.spec.util.slugify_rust_module`, so it is already a
    #: ``snake_case`` module identifier, unique within the program.
    module: str

    @property
    def stem(self) -> str:
        return f"cvlr_{self.module}"

    @property
    def feature(self) -> CargoFeature:
        """The cargo feature that compiles this unit, and only this unit.

        Every unit's module is declared behind ``#[cfg(feature = "<this>")]``.
        A module that is not selected is never read by rustc and never appears
        in its dep-info, so it cannot break or dirty another unit's build.

        The name is prefixed with ``unit_``. It shares a namespace with the
        project's own features. A module such as ``serde`` would otherwise select
        the project's feature.
        """
        return CargoFeature(f"unit_{self.module}")

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
    """Writes CVLR harnesses into the crate, and everything else under ``certora/cvlr/``.

    ``package_dir`` is the verified package's directory relative to the project
    root. The harness has to land in that crate. In a workspace that is not
    the project root.
    """

    def __init__(self, project_root: str | Path, package_dir: Path) -> None:
        super().__init__(
            project_root,
            "property_rules",
            deliverable_dir=DELIVERABLE_DIR,
            internal_dir=INTERNAL_DIR,
            report_dir=DELIVERABLE_DIR / "reports",
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

    def declare_modules(self, modules: list[HarnessModule]) -> tuple[Path, ...]:
        """Write ``specs/mod.rs``, declaring every unit's module behind its cargo feature.

        Returns the path it writes.

        Called once, before any unit stages a draft. The file has to name every
        unit. Each declaration is gated on :attr:`HarnessModule.feature`. A
        module behind a disabled ``cfg`` is never compiled and never enters
        rustc's dep-info, so it cannot break or rebuild another unit's build.
        The module file itself is written when that unit's draft is staged.

        Two units are never compiled together, so two ``impl Nondet for Foo``
        in different modules do not collide (``E0119``).

        Module names are unique because slugs are: component analysis rejects
        sibling components whose slugs collide.
        """
        target = self._artifact_dir()
        declarations = "".join(
            f'#[cfg(feature = "{m.feature}")]\npub mod {m.module};\n'
            for m in sorted(modules, key=lambda m: m.module)
        )
        mod_rs = target / "mod.rs"
        mod_rs.write_text(
            "//! The rules. One module per unit.\n"
            "//!\n"
            "//! Each module is gated on its own cargo feature, so a build selects exactly one\n"
            "//! unit's rules. A module behind a disabled `cfg` is never compiled and never\n"
            "//! enters rustc's dep-info, so one unit's draft cannot break or dirty another's\n"
            "//! build.\n"
            "\n" + declarations
        )
        # Callers log this path.
        return (mod_rs,)
