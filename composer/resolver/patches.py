"""The agent's products as patches against the fetched run.

The lemma agent edits sources through a VFS overlay relative to the run
directory and edits the spec in its own buffer. Both become unified diffs
against the run as fetched, so a caller applies them to a checkout and the
resolver never edits one.
"""
from dataclasses import dataclass
from pathlib import Path

from composer.spec.source.munge.vfs_diff import diff_against_baseline, file_diff

from .workspace import RunWorkspace


@dataclass(frozen=True)
class Patches:
    #: Unified diff over the source files the agent changed; empty if none.
    source: str
    #: Unified diff of the verified spec; empty if unchanged.
    spec: str

    @property
    def is_empty(self) -> bool:
        return not self.source and not self.spec


def patches_for(workspace: RunWorkspace, *, vfs: dict[str, str], final_spec: str) -> Patches:
    """``vfs`` is the agent's overlay (paths relative to ``run_dir``); ``final_spec``
    is its spec buffer at delivery."""
    return Patches(
        source=diff_against_baseline(vfs, workspace.run_dir),
        spec=file_diff(workspace.spec_path.as_posix(), workspace.spec_text(), final_spec),
    )


def write_patches(patches: Patches, out_dir: Path) -> list[Path]:
    """Write the non-empty patches as ``source.patch`` and ``spec.patch``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, text in (("source.patch", patches.source), ("spec.patch", patches.spec)):
        if not text:
            continue
        target = out_dir / name
        target.write_text(text, encoding="utf-8")
        written.append(target)
    return written
