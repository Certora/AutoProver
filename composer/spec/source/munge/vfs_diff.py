"""Unified-diff core shared between the migration oracle and change summaries.

The diff is always computed the same way: an ``old`` resolver that answers "what
was at this path in the baseline view" plus the ``new`` overlay whose keys are the
edited files. Iterating the overlay is sufficient because VFS overlays only
accumulate — there is no delete-file tool — so a path missing from ``old`` is an
addition, never a deletion.
"""

import difflib
from typing import Callable

from graphcore.tools.vfs import VFSState, VFSAccessor


def file_diff(path: str, old: str | None, new: str) -> str:
    """Unified diff for a single file. ``old`` is ``None`` when the path isn't in
    the baseline view (an addition); identical content yields the empty string."""
    if old == new:
        return ""
    old_lines = (old or "").splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    from_label = f"a/{path}" if old is not None else "/dev/null"
    return "".join(
        difflib.unified_diff(old_lines, new_lines, fromfile=from_label, tofile=f"b/{path}")
    )


def compute_diff(old: Callable[[str], str | None], new: dict[str, str]) -> str:
    """Diff the ``old`` resolver against every path in the ``new`` overlay,
    concatenating the per-file unified diffs and dropping the unchanged files."""
    chunks = (file_diff(path, old(path), content) for path, content in new.items())
    return "".join(c for c in chunks if c)


def _decode(raw: bytes | None) -> str | None:
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def summarize_changes(
    state: VFSState,
    accessor: VFSAccessor[VFSState],
    original: dict[str, str],
) -> str:
    """Summarize the edits in ``state`` relative to ``original`` as a unified diff.

    ``original`` is a baseline VFS overlay; the ``accessor`` resolves any path it
    doesn't carry through to the base fs_layer, so the comparison is against the
    original *view* (overlay over base), not just the overlay. Only the files
    edited in ``state`` (its overlay keys) are diffed."""
    baseline: VFSState = {"vfs": original}

    def old(path: str) -> str | None:
        return _decode(accessor.get(baseline, path))

    return compute_diff(old, state["vfs"])
