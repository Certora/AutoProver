"""The authored artifact: a CVLR harness module, and where it is persisted.

A CVL spec is a standalone file the prover is pointed at. A CVLR harness is **a Rust module inside
the crate under verification** — it only compiles from there, and the prover reaches it through the
build rather than through a path. So the deliverable and the source file are the same file, and the
artifact store writes into the package's ``src/certora/specs/`` that the preflight scaffold created
(:mod:`composer.spec.cvlr.scaffold`).

Two consequences that are not obvious from the CVL side:

* **The module has to be declared.** ``specs/mod.rs`` needs a ``mod <name>;`` line per unit, and
  every declared module must exist as a file or *no* unit compiles. Declaring them all up front is
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
                },
                indent=2,
            )
        )

    def declare_modules(self, modules: list[HarnessModule]) -> Path:
        """Write ``specs/mod.rs`` declaring every unit's module, and create any that do not exist.

        Called once, before any unit authors, because ``mod x;`` with no ``x.rs`` is a compile error
        — so a unit whose sibling has not been written yet would fail its own compile gate for a
        reason that has nothing to do with it. The placeholder is a doc comment: an empty module
        compiles and declares no rules, which is exactly the right starting state.

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
        for module in modules:
            path = target / module.artifact_file
            if not path.exists():
                path.write_text(
                    f"//! Harness for {module.slug}. Written by the CVLR author.\n"
                )
        declarations = "".join(f"mod {m.module};\n" for m in sorted(modules, key=lambda m: m.module))
        mod_rs = target / "mod.rs"
        mod_rs.write_text(
            "//! The rules. One module per unit, declared here by the CVLR backend.\n"
            "//!\n"
            "//! Every module named here must exist as a file, or the crate does not compile — so\n"
            "//! this is written once for the whole run rather than appended to per unit.\n"
            "\n" + declarations
        )
        return mod_rs
