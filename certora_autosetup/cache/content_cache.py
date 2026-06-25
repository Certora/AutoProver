"""
Content-hash-based cache keyed by file contents.

Provides a generic caching mechanism where cache keys are derived from the actual
content of files of interest, plus optional extra key parts. This ensures cache
invalidation when any relevant file changes.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

from certora_autosetup.cache.cache_fs import cache_path, get_fs
from certora_autosetup.utils.constants import DIR_CERTORA_INTERNAL, DIR_CONTENT_CACHE


class ContentCache:
    """Content-hash-based cache for arbitrary data keyed by file contents.

    Usage:
        cache = ContentCache("autosetup")
        key = cache.compute_cache_key(sol_files, extra_key_parts=["solc:0.8.20"])
        cached = cache.get(key)
        if cached is None:
            result = expensive_computation()
            cache.put(key, result)
    """

    def __init__(self, namespace: str, cache_dir: Path | None = None):
        """Initialize a content cache.

        Args:
            namespace: Namespace for this cache instance (e.g., "autosetup", "extcall_Token").
                       Creates a subdirectory under the cache base.
            cache_dir: Deprecated. Ignored — paths are computed from cache_fs.
        """
        self._namespace = namespace
        self._base = cache_path(DIR_CERTORA_INTERNAL, DIR_CONTENT_CACHE, namespace)

    def compute_cache_key(
        self,
        files_of_interest: list[Path],
        extra_key_parts: list[str] | None = None,
    ) -> str:
        """Compute a cache key from file contents and extra parts.

        The key is a SHA-256 hash of:
        - Sorted (relative_path, file_content_hash) pairs for all files
        - Any extra key parts (e.g., config flags, compiler versions)

        Files that don't exist are included with a "MISSING" content hash,
        so that creating or deleting a file invalidates the cache.

        Args:
            files_of_interest: Local paths to files whose contents determine the cache key.
            extra_key_parts: Additional strings to include in the key (e.g., config flags).

        Returns:
            A hex string cache key.
        """
        parts: list[str] = []

        for file_path in sorted(files_of_interest, key=lambda p: str(p)):
            if file_path.exists() and file_path.is_file():
                content_hash = self._hash_file(file_path)
            else:
                content_hash = "MISSING"
            parts.append(f"{file_path}:{content_hash}")

        if extra_key_parts:
            for part in extra_key_parts:
                parts.append(f"extra:{part}")

        combined = "\n".join(parts)
        return hashlib.sha256(combined.encode()).hexdigest()[:32]

    def get(self, cache_key: str) -> dict[str, Any] | None:
        """Retrieve cached data for the given key.

        Args:
            cache_key: Key returned by compute_cache_key().

        Returns:
            The cached data dict, or None if not found or corrupted.
        """
        fs = get_fs()
        p = self._cache_file(cache_key)
        if not fs.exists(p):
            return None

        try:
            with fs.open(p, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    def put(self, cache_key: str, data: dict[str, Any]) -> None:
        """Store data in the cache.

        Args:
            cache_key: Key returned by compute_cache_key().
            data: JSON-serializable dict to cache.
        """
        fs = get_fs()
        fs.mkdirs(self._base, exist_ok=True)
        p = self._cache_file(cache_key)
        with fs.open(p, "w") as f:
            json.dump(data, f, indent=2)

    def invalidate(self, cache_key: str) -> None:
        """Remove a specific cache entry.

        Args:
            cache_key: Key to invalidate.
        """
        fs = get_fs()
        p = self._cache_file(cache_key)
        if fs.exists(p):
            fs.rm(p)

    def clear(self) -> None:
        """Remove all entries in this cache namespace."""
        fs = get_fs()
        if fs.exists(self._base):
            fs.rm(self._base, recursive=True)

    def _cache_file(self, cache_key: str) -> str:
        """Get the path for a cache entry."""
        return self._base + f"/{cache_key}.json"

    @staticmethod
    def _hash_file(file_path: Path) -> str:
        """Compute SHA-256 hash of a local file's contents (first 16 hex chars)."""
        hasher = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                hasher.update(chunk)
        return hasher.hexdigest()[:16]
