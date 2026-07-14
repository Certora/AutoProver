"""Artifact store for a Rust application.

A thin :class:`composer.spec.artifacts.ArtifactStore` subclass: the base already
writes everything identical across backends (``properties.json``,
``commentary.md``, the property→units map, ``token_usage.json``) and materializes
the artifact bytes from ``result.artifact_text``. All this subclass supplies is
the on-disk layout, taken from the descriptor's :class:`ArtifactLayout`.
"""

from pathlib import Path
from typing import override

from composer.rustapp.descriptor import ArtifactLayout
from composer.rustapp.result import RustArtifact, RustFormalResult
from composer.spec.artifacts import ArtifactStore
from composer.spec.util import ensure_dir


class RustArtifactStore(ArtifactStore[RustArtifact, RustFormalResult]):
    """Persist a Rust backend's deliverables under the descriptor's layout."""

    def __init__(self, project_root: str, layout: ArtifactLayout):
        self._layout = layout
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
