import contextlib
import hashlib
import os
import re
import uuid
from pathlib import Path
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

# Solidity is never withheld from the project source surface. Any .sol in the tree can
# turn out to be part of the verification target: the conf's ``packages`` remappings
# resolve into vendored dependency trees, and in a stock Foundry layout ``lib/`` and
# ``test/`` hold real contracts.
_KEEP_SOLIDITY = r"(?!.*\.sol\Z)"

# Matches at any depth, for path components that occur nested as readily as they do at
# the project root: a package's own dependency tree, a sub-project's build output.
_ANY_DEPTH = r"(?:.*/)?"

# Trees that carry source alongside content of no use to a reader. Only the
# non-Solidity part is withheld.
_NON_SOLIDITY_ONLY = [
    rf"{_ANY_DEPTH}node_modules/.*",
    rf"{_ANY_DEPTH}lib/.*",
    rf"{_ANY_DEPTH}test/.*",
    # Machine-generated output. Beyond being unreadable, a minified bundle or a packed
    # data blob holds its content on very few very long lines — a single line can span
    # megabytes — and a content grep reports whole matching lines.
    rf"{_ANY_DEPTH}dist/.*",
    r".*\.json",
    r".*[.\-_](?:min|bundle)\.js",
    r".*\.map",
    r".*\.dat",
]

# Prover working directories and VCS internals, withheld whole. The Solidity carve-out
# would be actively harmful here: their .sol content is a verbatim copy of a contract
# already reachable at its canonical path, since each certoraRun invocation
# materializes its own ``inputs/.certora_sources/**``. Analysis that wants a specific
# report's copy reaches it through a VFS scoped to that report, not through this surface.
_WITHHELD_WHOLE = [
    rf"{_ANY_DEPTH}\.certora_internal.*",
    rf"{_ANY_DEPTH}\.git.*",
    r"emv-.*",
]

# Paths the agent's source tools (``list_files`` / ``get_file`` / ``grep_files``) may
# not read, as a single ``re.fullmatch`` pattern over project-root-relative POSIX
# paths. Each alternative therefore has to match a whole path, not a prefix of one.
FS_FORBIDDEN_READ = "|".join([
    _KEEP_SOLIDITY + "(?:" + "|".join(_NON_SOLIDITY_ONLY) + ")",
    *_WITHHELD_WHOLE,
])

def uniq_thread_id(prefix: str) -> str:
    suff = uuid.uuid4().hex[:16]
    return f"{prefix}-{suff}"
