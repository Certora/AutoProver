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

from pydantic import BaseModel

from composer.cargo.features import CargoFeature
from composer.spec.artifacts import ArtifactStore
from composer.spec.cvlr.scaffold import SPECS_DIR
from composer.spec.types import CheckName, PropertyTitle
from composer.spec.util import ensure_dir

DELIVERABLE_DIR = Path("certora") / "cvlr"
INTERNAL_DIR = Path(".certora_internal") / "cvlr"

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

        The name is prefixed with ``unit_``. It shares a namespace with the
        project's own features. A module such as ``serde`` would otherwise select
        the project's feature.
        """
        return CargoFeature(f"unit_{self.module}")

    @property
    def artifact_file(self) -> str:
        return f"{self.module}.rs"


class GeneratedHarness(BaseModel):
    commentary: str
    harness: str

    def property_checks(self) -> list[tuple[PropertyTitle, list[CheckName]]]:
        """No property-to-rule map is part of this result."""
        return []

    @property
    def artifact_text(self) -> str:
        return self.harness


class CvlrArtifactStore(ArtifactStore[HarnessModule, GeneratedHarness]):
    """Writes CVLR harnesses into the verified package, and everything else under ``certora/cvlr/``.

    ``package_dir`` is that package relative to the project root. In a workspace
    it is not the project root, and the harness still has to be written into the package.
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
