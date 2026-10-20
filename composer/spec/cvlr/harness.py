"""A CVLR harness module, and where it is written.

A CVLR harness is a Rust module inside the crate under verification. It
compiles only from there, and the prover reaches it through the build. The
artifact store writes it into the package's ``src/certora/specs/``
(:mod:`composer.spec.cvlr.scaffold`). The modules are ``pub`` because a path
into ``specs`` has to be visible from the program's own file
(:func:`composer.spec.cvlr.scaffold._harness_files`).
"""

import dataclasses
from pathlib import Path
from typing import override

from pydantic import BaseModel

from composer.cargo.features import CargoFeature
from composer.spec.artifacts import ArtifactStore
from composer.spec.cvlr.scaffold import SPECS_DIR
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

        The name is prefixed with ``unit_``. It shares a namespace with the
        project's own features. A module such as ``serde`` would otherwise select
        the project's feature.
        """
        return CargoFeature(f"unit_{self.module}")

    @property
    def artifact_file(self) -> str:
        return f"{self.module}.rs"


class GeneratedHarness(BaseModel):
    """A published harness: the module source, and what the author said about it."""

    commentary: str
    harness: str

    def property_checks(self) -> list[tuple[PropertyTitle, list[CheckName]]]:
        """No property-to-rule map is part of this result."""
        return []

    @property
    def artifact_text(self) -> str:
        return self.harness


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

    def declare_modules(self, modules: list[HarnessModule]) -> tuple[Path, ...]:
        """Write ``specs/mod.rs``, declaring every unit's module behind its cargo feature.

        Returns the path it writes.

        Called once, before any unit stages a draft, so the file has to name
        every unit. A module behind a disabled ``cfg`` is never read by rustc and
        never enters its dep-info. So a unit that has not written its file yet
        does not stop another unit from compiling, and one unit's draft cannot
        break or rebuild another unit's build. Two units are never compiled
        together, so two ``impl Nondet for Foo`` in different modules do not
        collide (``E0119``).

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
            "//! The rules. One module per unit, each gated on its own cargo feature, so a\n"
            "//! build selects exactly one unit's rules.\n"
            "\n" + declarations
        )
        # Callers log this path.
        return (mod_rs,)
