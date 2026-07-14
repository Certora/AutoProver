"""Artifact store for a Rust application.

A thin :class:`composer.spec.artifacts.ArtifactStore` subclass: the base already
writes everything identical across backends (``properties.json``,
``commentary.md``, the property→units map, ``token_usage.json``) and materializes
the artifact bytes from ``result.artifact_text``. All this subclass supplies is
the on-disk layout, taken from the descriptor's :class:`ArtifactLayout`.
"""

from pathlib import Path
from typing import override

from composer.rustapp.descriptor import ArtifactLayout, DeliverableMode
from composer.rustapp.result import RustArtifact, RustFormalResult
from composer.spec.artifacts import ArtifactStore
from composer.spec.util import ensure_dir


class RustArtifactStore(ArtifactStore[RustArtifact, RustFormalResult]):
    """Persist a Rust backend's deliverables under the descriptor's layout.

    ``deliverable_mode`` selects how the *source* deliverable is written:
    ``per_component`` (the default) writes one ``{prefix}_{slug}.{ext}`` file per component from
    its ``artifact_text``; ``callout`` writes no per-component source — the wheel's ``finalize``
    renders the whole deliverable (e.g. Crucible's one shared crate). Either way the shared
    metadata (``commentary.md`` / the property→units map) is written per component."""

    def __init__(
        self,
        project_root: str,
        layout: ArtifactLayout,
        *,
        deliverable_mode: DeliverableMode = DeliverableMode.PER_COMPONENT,
        program: str = "",
    ):
        self._layout = layout
        self._deliverable_mode = deliverable_mode
        self._program = program
        super().__init__(
            project_root,
            layout.property_suffix,
            deliverable_dir=layout.deliverable_dir,
            internal_dir=layout.internal_dir,
            report_dir=layout.report_dir,
        )

    @override
    def _artifact_dir(self) -> Path:
        return ensure_dir(Path(self._project_root) / self._layout.artifact_dir)

    @override
    def write_artifact(self, i: RustArtifact, artifact: RustFormalResult) -> Path:
        """In ``callout`` mode, write only the shared metadata and return the (whole-deliverable)
        report link — the source files come from the wheel's ``finalize``. Otherwise defer to the
        base one-file-per-component writer."""
        if self._deliverable_mode is not DeliverableMode.CALLOUT:
            return super().write_artifact(i, artifact)
        self._write_commentary(i.stem, artifact.commentary)
        self._write_property_map(
            i.stem, self._property_suffix, {k: v for k, v in artifact.property_units()}
        )
        primary = self._layout.deliverable_primary
        return Path(primary.format(program=self._program) if primary else self._layout.deliverable_dir)
