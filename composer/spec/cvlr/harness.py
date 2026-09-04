"""The authored artifact: a CVLR harness module, and where it is persisted.

A CVL spec is a standalone file the prover is pointed at. A CVLR harness is **a Rust module inside
the crate under verification** — it only compiles from there, and the prover reaches it through the
build rather than through a path. So the deliverable and the source file are the same file, and the
artifact store writes into the package's ``src/certora/specs/`` that the preflight scaffold created
(:mod:`composer.spec.cvlr.scaffold`).

Two consequences that are not obvious from the CVL side:

* **The module has to be declared.** ``specs/mod.rs`` needs a ``pub mod <name>;`` line per unit, and
  every declared module must exist as a file or *no* unit compiles. ``pub`` because a
  ``cvlr::mock_fn`` stand-in is named by path from the program's own file, which is outside
  ``certora`` — see :data:`composer.spec.cvlr.scaffold._HARNESS_FILES`. Declaring them all up front is
  what :class:`composer.pipeline.core.StagedFormalizer` is for, and §5.6 predicted this exact shape:
  one shared harness module, many rules.
* **The module name is an identifier, not a slug.** A component slug may carry characters Rust will
  not accept in a path, so the name is derived once, here, rather than at each call site.
"""

import dataclasses
import json
import re
from pathlib import Path
from typing import override

from pydantic import BaseModel, Field

from composer.authoring.state import SkippedProperty
from composer.spec.artifacts import ArtifactStore
from composer.spec.cvlr.munge import FunctionMunge
from composer.spec.cvlr.scaffold import SPECS_DIR
from composer.spec.cvlr.state import PropertyRuleMapping, RuleSubject
from composer.spec.cvlr.tuning import SummaryDirective
from composer.spec.types import CheckName, PropertyTitle, RuleName
from composer.spec.util import ensure_dir

#: Where the report and diagnostics go. The harness itself does *not* live here — see the module
#: docstring — but the property maps, commentary and reports are ordinary deliverables.
DELIVERABLE_DIR = Path("certora") / "cvlr"
INTERNAL_DIR = Path(".certora_internal") / "cvlr"


def module_name(slug: str) -> str:
    """A Rust module identifier for a component slug.

    Rust accepts ``[A-Za-z_][A-Za-z0-9_]*``; a slug may carry ``-`` and, in principle, a leading
    digit. Both are fixed here rather than assumed away, because the failure is a module path the
    compiler rejects long after the name was chosen.

    The result is also lowercased, and runs of separators collapse. Validity is not the only bar for
    a name this code writes into somebody else's repository: a component slug like
    ``Vault_Initialization`` is a legal module path, but ``mod Vault_Initialization;`` earns a
    ``non_snake_case`` warning and reads as though nobody looked. A crate that denies that lint — or
    a CI that passes ``-D warnings`` — turns the same name into a compile error the *author* cannot
    do anything about, since the module name is chosen here and not in the draft.
    """
    cleaned = "".join(c if c.isalnum() or c == "_" else "_" for c in slug).lower()
    collapsed = re.sub(r"_+", "_", cleaned).strip("_")
    if not collapsed:
        return "spec"
    return collapsed if collapsed[:1].isalpha() else f"spec_{collapsed}"


@dataclasses.dataclass(frozen=True)
class HarnessModule:
    """One component's harness module."""

    slug: str

    @property
    def module(self) -> str:
        return module_name(self.slug)

    @property
    def stem(self) -> str:
        return f"cvlr_{self.module}"

    @property
    def feature(self) -> str:
        """The cargo feature that compiles this unit — and only this unit — into the crate.

        The whole of ``docs/single-working-tree.md`` rests on this name. Every unit's module is
        declared behind ``#[cfg(feature = "<this>")]``, so a module that is not selected is never
        read by rustc, never appears in its dep-info, and therefore cannot break or dirty another
        unit's build. That is the property the per-unit workdir used to buy.

        Prefixed rather than bare because it shares a namespace with the project's own features, and
        a component slug like ``serde`` or ``staging`` would otherwise silently mean something else.
        """
        return f"unit_{self.module}"

    @property
    def artifact_file(self) -> str:
        return f"{self.module}.rs"

    @property
    def run_key(self) -> str:
        """Key under which this unit's prover run is recorded in the run-link map."""
        return self.slug


class GeneratedHarness(BaseModel):
    """A published harness: the module source, what it claims, and where it ran."""

    commentary: str
    harness: str
    skipped: list[SkippedProperty] = Field(default_factory=list)
    property_rules: list[PropertyRuleMapping] = Field(default_factory=list)
    #: Rule name → why the author asserts it should fail. On this backend an expected failure is
    #: usually a finding, so it is carried into the report rather than smoothed over.
    expected_failures: dict[CheckName, str] = Field(default_factory=dict)
    #: What each rule drives — a program function, or harness-local code standing in for one. Same
    #: reasoning as ``expected_failures``: a verdict earned against a stand-in is worth something
    #: different from one earned against the program, and the report is where that has to be said.
    rule_subjects: list[RuleSubject] = Field(default_factory=list)
    #: Points-to summaries the author added to make a path analyzable. Reported, because each one
    #: replaced a function with an unconstrained stand-in: the verdicts are conditional on them
    #: being sound for these properties, and the ``why`` is the only argument that they were.
    summaries: list[SummaryDirective] = Field(default_factory=list)
    #: Verification-only attributes the author put on the program's own functions. Reported for a
    #: stronger version of the same reason: the verdicts are about the munged program, and the
    #: report's ``source_edits`` is where that is said in the shared vocabulary every backend uses.
    munges: list[FunctionMunge] = Field(default_factory=list)
    #: Every rule the draft declares, as read from the draft rather than transcribed by the model.
    declared_rules: list[RuleName] = Field(default_factory=list)
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
    """Persists CVLR harnesses into the crate, and everything else under ``certora/cvlr/``.

    ``package_dir`` is the verified package's directory relative to the project root — the harness
    has to land inside *that* crate, and in a workspace it is not the root.
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
    def write_artifact(self, i: HarnessModule, artifact: GeneratedHarness) -> Path:
        """The harness, plus the sidecar of things its verdicts are conditional on.

        The base store writes the module, the commentary and the property→rule map. Neither of the
        two claims this backend needs a reader to see survives that: which rules drive the program's
        own code rather than a stand-in, and which functions the prover was told to stop analyzing.
        Both were reaching the checkpoint and dying there — an end-to-end run published seven rules
        with their subjects correctly declared and nothing on disk said so.
        """
        written = super().write_artifact(i, artifact)
        self._write_assumptions(i.stem, artifact)
        return written

    def _write_assumptions(self, stem: str, artifact: GeneratedHarness) -> None:
        (self._properties_dir() / f"{stem}.assumptions.json").write_text(
            json.dumps(
                {
                    "rule_subjects": [s.model_dump() for s in artifact.rule_subjects],
                    "summaries": [dataclasses.asdict(s) for s in artifact.summaries],
                    "munges": [dataclasses.asdict(m) for m in artifact.munges],
                },
                indent=2,
            )
        )

    def declare_modules(self, modules: list[HarnessModule]) -> tuple[Path, ...]:
        """Write ``specs/mod.rs`` declaring every unit's module behind its own cargo feature.

        Returns every path it is responsible for, ``mod.rs`` first, so a caller sharing one working
        tree across units can re-sync exactly these
        (:meth:`composer.spec.cvlr.tree.SharedTree.adopt`).

        Called once, before any unit authors, because the file has to name every unit and no single
        unit knows them all. Each declaration is gated on :attr:`HarnessModule.feature`, which is
        what lets the units share one working tree: a module behind a disabled ``cfg`` is never
        compiled, never enters rustc's dep-info, and so cannot break — or force a rebuild of —
        another unit's build (``docs/single-working-tree.md`` §2.1–2.2).

        Files are still created up front. Under the gate a missing file is only an error for the
        unit that selects it, but an empty module is a better starting state than a compile error
        the *author* cannot act on, and creating them here keeps one writer for this directory. The
        placeholder is a doc comment: an empty module compiles and declares no rules.

        A second consequence worth naming: two units are now never compiled together, so two units
        that each write ``impl Nondet for Foo`` in their own module no longer collide (``E0119``).
        That hazard was previously hidden by each unit having its own workspace, and would have
        surfaced the moment the trees were shared.

        Two units whose slugs reduce to one module name are refused here rather than tolerated.
        This is the only place that sees every name at once, and the alternative is silent: they
        would share a file, so one unit's harness would overwrite the other's, both compile gates
        would pass, and the report would claim two delivered units on one body of work — a false
        verification claim rather than a crash. A duplicate ``mod`` line would not compile anyway;
        failing here is the same outcome with a message that names the cause.
        """
        by_module: dict[str, list[str]] = {}
        for module in modules:
            by_module.setdefault(module.module, []).append(module.slug)
        clashes = {name: slugs for name, slugs in by_module.items() if len(slugs) > 1}
        if clashes:
            detail = "; ".join(f"{sorted(s)} all become {name!r}" for name, s in clashes.items())
            raise ValueError(
                f"two or more components share a harness module name ({detail}). Module names are "
                f"lowercased and separator-collapsed, so slugs differing only in case or "
                f"punctuation collide; rename the components so they differ in more than that."
            )
        target = self._artifact_dir()
        written: list[Path] = []
        for module in modules:
            path = target / module.artifact_file
            written.append(path)
            if not path.exists():
                path.write_text(
                    f"//! Harness for {module.slug}. Written by the CVLR author.\n"
                )
        declarations = "".join(
            f'#[cfg(feature = "{m.feature}")]\npub mod {m.module};\n'
            for m in sorted(modules, key=lambda m: m.module)
        )
        mod_rs = target / "mod.rs"
        mod_rs.write_text(
            "//! The rules. One module per unit, declared here by the CVLR backend.\n"
            "//!\n"
            "//! Each module is gated on its own cargo feature, so a build selects exactly one\n"
            "//! unit's rules. That is what lets every unit share one working tree: a module\n"
            "//! behind a disabled `cfg` is never compiled and never enters rustc's dep-info, so\n"
            "//! one unit's draft cannot break or dirty another's build.\n"
            "//!\n"
            "//! Written once for the whole run rather than appended to per unit.\n"
            "\n" + declarations
        )
        # `mod.rs` first: it is the one a caller logs, and the rest are what a shared working tree
        # has to be re-synced with when a resumed run's component set has changed.
        return (mod_rs, *written)
