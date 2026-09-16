"""An artifact registrar that writes files.

In the pipeline a plugin's artifacts are registered in memory and the driver
persists them through the run's artifact store. The resolver has no such
store: it owns an output directory, and every registered artifact lands there
under its own name.
"""
from dataclasses import dataclass, field
from pathlib import Path

from composer.spec.types import VerificationArtifact


@dataclass
class FileArtifactRegistrar:
    """Implements :class:`composer.pipeline.plugin_api.ArtifactRegistrar`."""
    out_dir: Path
    written: list[tuple[VerificationArtifact, Path]] = field(default_factory=list)

    def register(self, artifact: VerificationArtifact) -> None:
        # The name is a basename by contract; keep it one even if a caller slips.
        target = self.out_dir / Path(artifact.name).name
        self.out_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(artifact.content, encoding="utf-8")
        self.written.append((artifact, target))
