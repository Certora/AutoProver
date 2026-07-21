"""File utilities for safe file operations."""

import json
import os
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import ijson


def stream_ast_files(ast_path: Path) -> Iterator[tuple[str, Any]]:
    """Yield ``(relative_path, path_data)`` pairs from a ``.asts.json``.

    The file is streamed one top-level entry at a time, so only a single source
    file's ASTs are held in memory. ``.asts.json`` is sometimes many GB.
    Structure: ``dict[relative_path: dict[absolute_path: dict[node_id: node_data]]]``.
    """
    with open(ast_path, "rb") as f:
        yield from ijson.kvitems(f, "")


def atomic_write_json(file_path: Path, data: Any, indent: int = 2) -> None:
    """
    Write JSON data to a file atomically to prevent corruption.

    Writes to a temporary file first, flushes to disk, then atomically
    renames to the target path. This prevents readers from seeing
    incomplete/corrupted JSON during writes or after crashes.

    Uses unique temp file names (PID + thread ID + UUID) to prevent conflicts
    when multiple processes/threads write to the same file simultaneously.

    Args:
        file_path: Target file path
        data: Data to serialize as JSON
        indent: JSON indentation (default: 2)

    Raises:
        Exception: If write or rename fails
    """
    # Ensure file_path is a Path object
    file_path = Path(file_path)

    # Create unique temp file name using PID, thread ID, and UUID
    # This prevents conflicts when multiple processes/threads write simultaneously
    pid = os.getpid()
    tid = threading.get_ident()
    unique_id = uuid.uuid4().hex[:8]
    temp_file = file_path.with_suffix(f'.tmp.{pid}.{tid}.{unique_id}')

    try:
        with open(temp_file, 'w') as f:
            json.dump(data, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())  # Ensure data is on disk before rename

        # Atomic rename (overwrites existing file if present)
        temp_file.replace(file_path)
    finally:
        # Clean up temp file if it still exists (e.g., if rename failed)
        if temp_file.exists():
            temp_file.unlink()


def atomic_write_json_fsspec(path: str, data: Any, indent: int = 2) -> None:
    """Write JSON to a fsspec path atomically (works for local FS and S3).

    Mirrors ``atomic_write_json`` but through the cache filesystem singleton, so
    callers writing under the SaaS cache prefix (``s3://…``) get the same
    write-temp-then-rename safety as local writes. The temp object carries a unique
    suffix (PID + thread id + UUID) so concurrent writers to the same key don't
    clobber each other; ``fs.mv`` finalizes the destination in one step (atomic
    rename on local FS; copy-then-delete on S3, where the destination object only
    appears once the copy completes).

    Imported lazily so file_utils stays free of a hard cache_fs dependency.
    """
    from certora_autosetup.cache.cache_fs import get_fs

    fs = get_fs()
    pid = os.getpid()
    tid = threading.get_ident()
    unique_id = uuid.uuid4().hex[:8]
    temp_path = f"{path}.tmp.{pid}.{tid}.{unique_id}"

    parent = path.rsplit("/", 1)[0] if "/" in path else "."
    fs.makedirs(parent, exist_ok=True)  # no-op-safe on S3; needed for local FS

    try:
        with fs.open(temp_path, "w") as f:
            json.dump(data, f, indent=indent)
        fs.mv(temp_path, path)
    finally:
        if fs.exists(temp_path):
            fs.rm(temp_path)
