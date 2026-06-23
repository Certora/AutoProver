"""Cache filesystem abstraction.

Provides a module-level fsspec filesystem singleton so cache/state I/O
transparently targets local FS or S3 depending on the configured root.

    # Local mode (default):
    init_cache_fs()                          # root = cwd

    # S3 mode (SaaS containers):
    init_cache_fs("s3://bucket/prefix")      # root = s3://bucket/prefix

    # Auto-detect SaaS mode from PreAudit container env vars:
    # PREAUDIT_S3_BUCKET + PREAUDIT_REPO_CACHE_PREFIX → s3 root
    # otherwise local. Callers in the SaaS path do not have to construct
    # the URL themselves — they can just call init_cache_fs().
    init_cache_fs()

    # Usage:
    fs = get_fs()
    p = cache_path(DIR_CERTORA_INTERNAL, DIR_LLM_CACHE, f"{key}.json")
    with fs.open(p, "w") as f:
        json.dump(data, f)
"""

import os
from pathlib import Path

import fsspec

from certora_autosetup.utils.logger import logger

_fs: fsspec.AbstractFileSystem | None = None
_root: str = ""

# PreAudit's SaaS container env vars. When both are set, init_cache_fs()
# called with no args auto-picks S3 against `s3://{bucket}/{prefix}` —
# the SaaS entrypoint doesn't have to read the env or build the URL
# itself, and the runner's later init_cache_fs() call doesn't accidentally
# reset the singleton to local.
_BUCKET_ENV = "PREAUDIT_S3_BUCKET"
_PREFIX_ENV = "PREAUDIT_REPO_CACHE_PREFIX"


def _resolve_default_root() -> str:
    """If SaaS env vars are set, return the s3://… URL; otherwise local cwd."""
    bucket = os.environ.get(_BUCKET_ENV, "").strip("/")
    prefix = os.environ.get(_PREFIX_ENV, "").strip("/")
    if bucket and prefix:
        return f"s3://{bucket}/{prefix}"
    return "."


def init_cache_fs(root: str | Path | None = None, *, force: bool = False) -> None:
    """Initialize the cache filesystem singleton.

    When ``root`` is None, the function reads PREAUDIT_S3_BUCKET and
    PREAUDIT_REPO_CACHE_PREFIX from the environment — when both are
    set, the singleton is pointed at ``s3://{bucket}/{prefix}``;
    otherwise it falls back to a local FS rooted at cwd. Callers in
    a SaaS container don't have to remember to construct the URL.

    Idempotency: a second call with ``force=False`` (the default)
    will NOT silently overwrite a previously-set S3 root with a local
    one. Local→local and S3→S3 re-inits still apply — the runner
    relies on re-initing after ``os.chdir(project_dir)`` to pick up
    the new cwd in local CLI mode. Pass ``force=True`` to override
    the guard (test resets, explicit re-init).
    """
    global _fs, _root
    target = str(root) if root is not None else _resolve_default_root()
    new_is_s3 = target.startswith("s3://")
    old_is_s3 = _root.startswith("s3://")

    if old_is_s3 and not new_is_s3 and not force:
        # Exactly the SaaS-init-then-local-re-init bug — refuse to silently
        # drop the S3 root. Caller probably forgot to thread the SaaS env
        # through, or this is the runner re-initing inside _run_preaudit
        # (intended no-op).
        logger.info(
            f"init_cache_fs: keeping existing S3 root {_root} "
            f"(refused re-init to local '{target}'); pass force=True to override"
        )
        return

    if new_is_s3:
        _fs = fsspec.filesystem("s3")
        _root = target.rstrip("/")
    else:
        _fs = fsspec.filesystem("file")
        _root = str(Path(target).resolve())


def get_fs() -> fsspec.AbstractFileSystem:
    """Return the configured filesystem. Lazily defaults to local."""
    global _fs
    if _fs is None:
        init_cache_fs()
    assert _fs is not None
    return _fs


def cache_path(*parts: str) -> str:
    """Build a full cache path from the root and path components.

    Example:
        cache_path(".certora_internal", "content_cache", "autosetup", "abc.json")
        → "/home/user/project/.certora_internal/content_cache/autosetup/abc.json"  (local)
        → "s3://bucket/prefix/.certora_internal/content_cache/autosetup/abc.json"   (S3)
    """
    if _root == "":
        init_cache_fs()
    if _root.startswith("s3://"):
        return _root + "/" + "/".join(parts)
    return str(Path(_root).joinpath(*parts))
