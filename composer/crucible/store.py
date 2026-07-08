"""Crucible artifact writer: a harness *crate* under ``fuzz/<program>/`` plus
per-component metadata under ``certora/crucible/``.

Unlike the generic single-file :class:`composer.rustapp.store.RustArtifactStore`,
a Crucible deliverable is one Cargo crate assembled from a shared fixture + one
feature-gated test section per component (``docs/crucible-application.md`` §7.1).
So each component's ``write_artifact`` folds its section into the crate and
re-renders it, while the shared base still writes ``properties.json`` /
``commentary.md`` / the property→tests map under ``certora/crucible/`` — exactly
the split Foundry uses (``.t.sol`` in ``test/``, metadata under ``certora/foundry/``).
"""

from __future__ import annotations

from pathlib import Path
from typing import override

from composer.crucible.harness import CrucibleDep, CrucibleHarness
from composer.rustapp.result import RustArtifact, RustFormalResult
from composer.spec.artifacts import ArtifactStore
from composer.spec.util import ensure_dir

CRUCIBLE_DELIVERABLE_DIR = "certora/crucible"
CRUCIBLE_INTERNAL_DIR = ".certora_internal/crucible"


class CrucibleArtifactStore(ArtifactStore[RustArtifact, RustFormalResult]):
    """Persists the Crucible harness crate + its metadata.

    The crate is assembled incrementally: each ``write_artifact`` adds one
    component's feature-gated test section to :attr:`harness` and re-writes the
    crate under ``fuzz/<program>/``. The metadata bundle lands under
    ``certora/crucible/`` so a co-located EVM (autoprove/foundry) run shares the
    project root without collision.

    The shared fixture/actions are set on :attr:`harness` before formalization
    (produced by the authoring loop in ``prepare_formalization``; a later phase).
    """

    def __init__(self, project_root: str, *, program: str, dep: CrucibleDep):
        super().__init__(
            project_root,
            "property_tests",
            deliverable_dir=CRUCIBLE_DELIVERABLE_DIR,
            internal_dir=CRUCIBLE_INTERNAL_DIR,
            report_dir=f"{CRUCIBLE_DELIVERABLE_DIR}/reports",
        )
        self._program = program
        self.harness = CrucibleHarness(program=program, dep=dep)

    def fuzz_dir(self) -> Path:
        """``fuzz/<program>/`` — where ``crucible run`` expects the harness."""
        return ensure_dir(Path(self._project_root) / "fuzz" / self._program)

    @override
    def _artifact_dir(self) -> Path:
        # The crate's source dir. (The base's one-file-per-component writer is not
        # used — write_artifact is overridden to assemble the crate instead.)
        return ensure_dir(self.fuzz_dir() / "src")

    @override
    def write_artifact(self, i: RustArtifact, artifact: RustFormalResult) -> Path:
        """Fold this component's test section into the crate and re-render it, then
        write the component's metadata under ``certora/crucible/``. Returns the
        crate's ``main.rs`` (project-relative) — the component's "deliverable"."""
        feature = CrucibleHarness.feature_for(i.slug)
        self.harness.add_component(feature, artifact.artifact_text)
        main = self.harness.write(self.fuzz_dir())

        # Metadata bundle (shared base helpers), under certora/crucible/properties/.
        self._write_commentary(i.stem, artifact.commentary)
        self._write_property_map(
            i.stem, self._property_suffix, {k: v for k, v in artifact.property_units()}
        )
        return main.relative_to(self._project_root)
