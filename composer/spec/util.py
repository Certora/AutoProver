import contextlib
import hashlib
import os
import re
import uuid
from pathlib import Path, PurePath
from typing import Iterator

from composer.spec.gen_types import CERTORA_DIR


def string_hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def slugify_filename(name: str) -> str:
    # Collapse any run of filesystem-unsafe characters into a single underscore so the
    # result is safe to use as a filename component; falls back to "unnamed" if empty.
    # Example: "transfer(address,uint256)" -> "transfer_address_uint256"
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")
    return slug or "unnamed"


def ensure_dir(path: Path) -> Path:
    """``mkdir -p`` *path* (no-op if it already exists) and return it, so it can be
    used inline, e.g. ``ensure_dir(certora_dir / "specs") / spec_name``."""
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextlib.contextmanager
def temp_certora_file(
    *,
    root: str,
    ext: str,
    content: str,
    prefix: str = "generated",
    name: str | None = None,
    dest_dir: Path = CERTORA_DIR,
) -> Iterator[str]:
    """Write a temp file under ``<root>/<dest_dir>``, yield its path **relative to
    the project root**, and clean it up.

    *dest_dir* is itself project-root-relative (default ``certora``). The yielded
    path uses the same project-root-relative convention as the persisted artifacts,
    so callers use it verbatim (no ``certora/`` prefixing). Materializing a spec in
    the same directory it will ultimately be dumped to (e.g. ``certora/specs``)
    makes the prover resolve the spec's CVL ``import`` statements identically at
    verify-time and after persistence.

    *name* (without extension) names the file ``<name>.<ext>`` verbatim instead of a
    unique ``<prefix>_<uid>.<ext>``. Since it is then not unique, callers passing
    *name* must serialize same-name use (the file is unlinked on exit).
    """
    tmp_name = f"{name}.{ext}" if name is not None else f"{prefix}_{uuid.uuid1().hex[:16]}.{ext}"
    target_dir = ensure_dir(Path(root) / dest_dir)
    tgt = target_dir / tmp_name
    tgt.write_text(content)
    try:
        yield (dest_dir / tmp_name).as_posix()
    finally:
        os.unlink(tgt)

# Prover working directories and VCS internals. Withheld whole, Solidity included:
# their .sol is a verbatim copy of a contract already readable at its canonical path,
# since each certoraRun invocation materializes its own ``inputs/.certora_sources/**``.
# Analysis that wants a specific report's copy reaches it through a VFS scoped to that
# report, not through the project source surface.
_WITHHELD_WHOLE_DIRS = frozenset({".git", ".certora_internal"})

# Prover report directories, named ``emv-<n>-<verdict>-<contract>``. Root-only: these
# are created where certoraRun was invoked, which for this pipeline is the project root.
_REPORT_DIR_PREFIX = "emv-"

# Directories that carry source alongside content of no use to a reader — vendored
# dependencies, scaffolding, build output. Matched at any depth, since a package's own
# dependency tree nests as readily as the project's own sits at the root.
_NON_SOLIDITY_DIRS = frozenset({"node_modules", "lib", "test", "dist"})

# Machine-generated files. Beyond being unreadable, a minified bundle or a packed data
# blob holds its content on very few very long lines — a single line can span megabytes
# — and a content grep reports whole matching lines.
_GENERATED_SUFFIXES = frozenset({".json", ".map", ".dat"})

# Every separator/marker pairing a bundle name is written with in practice, so that
# ``str.endswith`` can take the whole tuple in one call: ``.min`` / ``-min`` / ``_min``
# and the same three for ``bundle``.
_BUNDLE_STEM_SUFFIXES = tuple(
    sep + marker for sep in (".", "-", "_") for marker in ("min", "bundle")
)


def _is_generated_bundle(path: PurePath) -> bool:
    """``vendor.min.js`` / ``app-bundle.js`` / ``app_bundle.js``, but not a hand-written
    ``bundle.js``: the marker has to be a suffix of the name, not the whole of it."""
    return path.suffix == ".js" and path.stem.endswith(_BUNDLE_STEM_SUFFIXES)


def fs_forbidden_read(path: PurePath) -> bool:
    """True to withhold *path* from the agent's source tools (``list_files`` /
    ``get_file`` / ``grep_files``). Paths are project-root-relative.

    Solidity is never withheld from the project source surface: any .sol can turn out
    to be part of the verification target, since the conf's ``packages`` remappings
    resolve into vendored dependency trees and a stock Foundry layout keeps real
    contracts in ``lib/`` and ``test/``. The one exception is the whole-withheld
    directories, which is why they are tested before the carve-out.
    """
    parts = path.parts
    if _WITHHELD_WHOLE_DIRS.intersection(parts):
        return True
    if parts and parts[0].startswith(_REPORT_DIR_PREFIX):
        return True
    if path.suffix == ".sol":
        return False
    return (
        bool(_NON_SOLIDITY_DIRS.intersection(parts))
        or path.suffix in _GENERATED_SUFFIXES
        or _is_generated_bundle(path)
    )

def uniq_thread_id(prefix: str) -> str:
    suff = uuid.uuid4().hex[:16]
    return f"{prefix}-{suff}"
