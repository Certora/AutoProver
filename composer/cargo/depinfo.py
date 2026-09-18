"""Source files a cargo build compiled, from rustc's dep-info.

Same role as the EVM backend's ``EditsNotCompiled``: an edit in a file the
build never reads changes nothing, and the run then claims a verdict about code
it did not verify. On Solana that usually means an attribute in a file the
``certora`` feature gates out.

rustc writes a Makefile-style ``.d`` next to each artifact. Several feature
variants of one crate share a ``target/``, each with its own ``.d``, and the
filename does not say which feature set produced it. The caller names a file
this build is known to have compiled (the unit's harness module). The dep-info
that mentions it belongs to this build. If the caller cannot name such a file,
this returns ``None``.
"""

import logging
import re
from pathlib import Path

_log = logging.getLogger(__name__)

#: rustc escapes a space in a path as ``\ ``; every other byte is literal.
_UNESCAPED_SPACE = re.compile(r"(?<!\\)\s+")


def _targets(text: str) -> list[list[str]]:
    """Dependency lists in one ``.d``, one per rule.

    A dep-info file has a rule per emitted artifact, then an empty rule per
    source. Empty right-hand sides are dropped, which removes those trailing
    stanzas.
    """
    lists: list[list[str]] = []
    for line in text.splitlines():
        _, sep, rhs = line.partition(": ")
        if not sep or not rhs.strip():
            continue
        lists.append([p.replace("\\ ", " ") for p in _UNESCAPED_SPACE.split(rhs.strip()) if p])
    return lists


def _resolve(raw: str, roots: tuple[Path, ...]) -> Path | None:
    """A dep-info path as an absolute path, or ``None`` if it does not exist here.

    Paths are relative to the directory cargo invoked rustc from: the workspace
    root for a workspace, the package root for a single crate. Those are often
    the same directory, so both are tried.
    """
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate.resolve() if candidate.exists() else None
    for root in roots:
        resolved = (root / candidate).resolve()
        if resolved.exists():
            return resolved
    return None


def compiled_sources(
    workdir: Path, package: str, *, marker: Path, package_root: Path | None = None
) -> frozenset[Path] | None:
    """Absolute paths of every source read by the build that compiled ``marker``.

    ``package`` is the cargo package name; dep-info files use ``_`` for ``-``.
    ``None`` means no dep-info naming ``marker`` was found.
    """
    crate = package.replace("-", "_")
    roots = tuple(dict.fromkeys(r for r in (workdir, package_root) if r is not None))
    wanted = marker.resolve()
    found: set[Path] = set()
    seen_marker = False
    for dep_file in sorted((workdir / "target").glob(f"*/deps/{crate}-*.d")) + sorted(
        (workdir / "target").glob(f"*/*/deps/{crate}-*.d")
    ):
        try:
            text = dep_file.read_text()
        except OSError:
            continue
        for sources in _targets(text):
            resolved = {p for raw in sources if (p := _resolve(raw, roots)) is not None}
            if wanted in resolved:
                seen_marker = True
                found |= resolved
    if not seen_marker:
        _log.info("cargo: no dep-info for %s mentions %s", crate, marker)
        return None
    return frozenset(found)
