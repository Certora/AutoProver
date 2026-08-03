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

# Matches at any depth, for path components that occur nested as readily as they do
# at the project root: a package's own dependency tree, a submodule's ``.git``, a
# sub-project's ``.certora_internal``.
_ANY_DEPTH = r"(?:.*/)?"

# Paths the agent's source tools (``list_files`` / ``get_file`` / ``grep_files``) may
# not read, as a single ``re.fullmatch`` alternation over project-root-relative POSIX
# paths. Each alternative therefore has to match a whole path, not a prefix of one.
FS_FORBIDDEN_READ = "|".join([
    # A dependency tree's Solidity stays readable: the conf's ``packages`` remappings
    # resolve into it, so it is part of the verification target's source. The rest of
    # the tree (its JS, docs, lockfiles) is not.
    rf"{_ANY_DEPTH}node_modules/.*(?<!\.sol)",
    # Deliberately root-only, unlike the rule above. A dependency keeps its own
    # transitive Solidity under nested ``lib/`` and ``test/`` directories, and the
    # conf remaps into those, so matching these at any depth would hide source the
    # agent has to be able to read.
    r"lib/.*",
    r"test/.*",
    r"emv-.*",
    rf"{_ANY_DEPTH}\.certora_internal.*",
    rf"{_ANY_DEPTH}\.git.*",
    # Machine-generated output. Beyond being unreadable, a minified bundle or packed
    # data blob holds its content on very few very long lines — a single line can span
    # megabytes — and a content grep reports whole matching lines.
    r".*\.json",
    rf"{_ANY_DEPTH}dist/.*",
    r".*[.\-_](?:min|bundle)\.js",
    r".*\.map",
    r".*\.dat",
])

def uniq_thread_id(prefix: str) -> str:
    suff = uuid.uuid4().hex[:16]
    return f"{prefix}-{suff}"
