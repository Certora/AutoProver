"""Putting CVLR's own source in front of the agents.

``docs/cvlr-backend-plan.md`` §5.5 is the argument: CVL's implementation lives inside the Prover and
cannot be read, so a curated manual plus recall is genuinely all an agent has. CVLR is the opposite —
it is a **cargo dependency of the project under verification**, so every macro definition and every
helper signature is on disk in the exact version the build resolves. We have less prose than EVM and
more ground truth, and this is the module that spends it.

Four decisions, each load-bearing:

**Its own tool set, not a layer of the project VFS.** Layering would put ``src/lib.rs`` from the
crate and from the project in one flat namespace, and the materializer dumps everything not globally
excluded, so crate sources would land in the tree the prover and the audit DB see. The combination
actually wanted from a layer — agent-visible, materializer-excluded — is not expressible today
(``forbidden_read`` hides from the agent only, ``global_exclude`` hides from both). A separate tool
set sidesteps all of it, and the tool's own name is the affordance that says what it is for.

**Every path carries its version.** A file is addressed as ``cvlr-0.6.1/src/lib.rs``, never
``cvlr/src/lib.rs``. Reading a different version's source than the build compiles is worse than
reading none — it is confidently wrong — and ``RUST_FORBIDDEN_READ`` hides ``Cargo.lock``, so an
agent has no other way to see which one it is holding. Stamping the version into every path makes it
unmissable, and makes a stale answer quotable back at the run that produced it.

**The whole family, not the facade.** ``cvlr`` re-exports; the definition of ``cvlr_assert!`` is in
``cvlr-asserts``, ``clog!`` in ``cvlr-log``, ``nondet()`` in ``cvlr-nondet``. An agent handed only
the crate the manifest names would find a re-export and stop.

**Read-only and small.** Only ``*.rs``, ``Cargo.toml`` and ``*.md`` are listed — a registry checkout
also carries a lockfile and cargo bookkeeping that answer no question anyone will ask. The listing
for a full CVLR family is a few dozen files, which is what keeps ``cvlr_source_files`` a cheap first
move rather than a context-burning one.
"""

import dataclasses
import logging
from collections.abc import Iterator
from pathlib import Path

from langchain_core.tools import BaseTool
from pydantic import Field

from composer.cargo.metadata import CratePackage
from composer.spec.cvlr.crates import CvlrSources
from graphcore.tools.schemas import WithAsyncDependencies

_log = logging.getLogger(__name__)

#: What is worth showing an agent in a crate checkout. Everything else a published crate carries —
#: ``Cargo.lock``, ``.cargo_vcs_info.json``, ``Cargo.toml.orig``, ``.cargo-ok`` — is packaging
#: bookkeeping that answers no question about what a macro does.
READABLE_NAMES = frozenset({"Cargo.toml"})
READABLE_SUFFIXES = frozenset({".rs", ".md"})

#: A search that matches this many lines is a search for the wrong thing; the cap turns a useless
#: answer into a short one that says so.
MAX_MATCHES = 80

#: Files are small (the largest CVLR module is a few hundred lines), but a cap keeps one bad read
#: from evicting the rest of an agent's context.
MAX_FILE_CHARS = 60_000


def _readable(path: Path) -> bool:
    return path.name in READABLE_NAMES or path.suffix in READABLE_SUFFIXES


@dataclasses.dataclass(frozen=True)
class MountedCrates:
    """Several crate source trees under one flat, version-stamped namespace.

    Constructed from what the *build* resolved, never from a version anyone chose: see
    :func:`mount`.
    """

    crates: tuple[CratePackage, ...]

    @property
    def roots(self) -> dict[str, Path]:
        """``<name>-<version>`` → the crate directory."""
        return {f"{c.name}-{c.version}": c.root for c in self.crates}

    def _resolve(self, path: str) -> Path | None:
        """The on-disk file for a namespaced path, or ``None`` if it escapes or does not exist."""
        head, _, rest = path.strip("/").partition("/")
        root = self.roots.get(head)
        if root is None or not rest:
            return None
        target = root / rest
        try:
            resolved = target.resolve()
        except OSError:
            return None
        # A crate checkout is trusted content, but the path is not: it comes from the agent, and
        # `..` in it would read whatever sits beside the registry cache.
        if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
            return None
        return resolved if _readable(resolved) else None

    def paths(self) -> Iterator[str]:
        for prefix, root in self.roots.items():
            for file in sorted(root.rglob("*")):
                if file.is_file() and _readable(file):
                    yield f"{prefix}/{file.relative_to(root)}"

    def read(self, path: str) -> str | None:
        resolved = self._resolve(path)
        if resolved is None:
            return None
        try:
            text = resolved.read_text(errors="replace")
        except OSError as exc:
            _log.info("could not read %s: %r", resolved, exc)
            return None
        return text if len(text) <= MAX_FILE_CHARS else text[:MAX_FILE_CHARS] + "\n… (truncated)"

    def search(self, needle: str) -> tuple[list[str], bool]:
        """``path:line: text`` for every line containing ``needle``, and whether the cap was hit."""
        hits: list[str] = []
        for path in self.paths():
            content = self.read(path)
            if content is None:
                continue
            for number, line in enumerate(content.splitlines(), start=1):
                if needle in line:
                    hits.append(f"{path}:{number}: {line.strip()}")
                    if len(hits) >= MAX_MATCHES:
                        return hits, True
        return hits, False

    def tools(self) -> tuple[BaseTool, ...]:
        """The read-only tool set over these crates.

        Named ``cvlr_source_*`` rather than reusing the project's ``get_file`` / ``list_files``: the
        two surfaces answer different questions over different trees, and a name that says which is
        what keeps an agent from asking one of them about the other.

        Paired with :meth:`statement` on purpose — binding the tools without saying they exist and
        are authoritative gets the expensive half of this and none of the benefit. Both hang off the
        mount so a caller reaches them from one ``None`` check rather than two."""
        bound = (CvlrSourceFiles.bind(self), CvlrSourceRead.bind(self), CvlrSourceSearch.bind(self))
        names = ("cvlr_source_files", "cvlr_source_read", "cvlr_source_search")
        return tuple(builder.as_tool(name) for builder, name in zip(bound, names))

    def statement(self) -> str:
        """What the prompt says about this mount — the versions, spelled out.

        The host resolved them; the agent is told rather than asked. This sentence is the whole
        defence against the failure mode that a plausible answer from the wrong version is
        indistinguishable from a right one."""
        listed = ", ".join(f"{c.name} {c.version}" for c in self.crates)
        return (
            f"This project's build resolves {listed}. Their complete source is readable through the "
            f"`cvlr_source_*` tools, under paths that begin with the crate's name and version. That "
            f"source is authoritative: it outranks anything recalled from the knowledge base and "
            f"anything you remember about CVLR."
        )


def mount(sources: CvlrSources) -> MountedCrates | None:
    """The mount for a resolved CVLR family, or ``None`` when the target depends on no CVLR.

    ``None`` rather than an empty mount, because the two ask for different behaviour: with no CVLR
    there are no tools to bind and nothing to say in the prompt, and an empty mount would advertise
    a source of truth that answers every question with "not found"."""
    present = tuple(c for c in sources.crates if c.root.is_dir())
    if len(present) != len(sources.crates):
        missing = [c.name for c in sources.crates if not c.root.is_dir()]
        # A resolved crate whose source is absent means the cache was pruned between the metadata
        # read and now. Mount what is there and say what is not, rather than failing an optional aid.
        _log.warning("CVLR crate sources missing from the cargo cache: %s", ", ".join(missing))
    return MountedCrates(present) if present else None


class CvlrSourceFiles(WithAsyncDependencies[str, MountedCrates]):
    """
    List every file of the CVLR crates this project builds against.

    Paths begin with the crate's name and version (`cvlr-0.6.1/src/lib.rs`), so the version you are
    reading is visible in every path — you never have to guess which CVLR this project uses. Read
    one with `cvlr_source_read`.
    """

    async def run(self) -> str:
        with self.tool_deps() as crates:
            return "\n".join(crates.paths()) or "(no CVLR sources are mounted)"


class CvlrSourceRead(WithAsyncDependencies[str, MountedCrates]):
    """
    Read a file from the CVLR crate sources.

    This is the definition, not a description of it: what a macro expands to, what a helper's
    signature is, which trait a derive implements. It is authoritative — when it disagrees with the
    knowledge base or with what you remember about CVLR, it is right and they are stale.
    """

    path: str = Field(description=(
        "A path from `cvlr_source_files` or `cvlr_source_search`, beginning with the crate's name "
        "and version — for example 'cvlr-asserts-0.6.1/src/lib.rs'."
    ))

    async def run(self) -> str:
        with self.tool_deps() as crates:
            content = crates.read(self.path)
            if content is None:
                return (
                    f"No such file: {self.path!r}. Paths begin with a crate name and version; call "
                    f"`cvlr_source_files` to see them."
                )
            return content


class CvlrSourceSearch(WithAsyncDependencies[str, MountedCrates]):
    """
    Find where a name appears in the CVLR crate sources.

    Use this to locate a definition before reading it — `cvlr` re-exports most of its surface, so
    the macro or helper you want is usually defined in a sibling crate (`cvlr-asserts`, `cvlr-log`,
    `cvlr-nondet`) rather than in `cvlr` itself.
    """

    name: str = Field(description=(
        "The exact identifier to look for — a macro (`cvlr_assert`, `clog`), a function "
        "(`nondet`), a trait or derive (`Nondet`, `CvlrLog`), or a type."
    ))

    async def run(self) -> str:
        with self.tool_deps() as crates:
            hits, capped = crates.search(self.name)
            if not hits:
                return (
                    f"{self.name!r} does not appear in the CVLR sources this project builds "
                    f"against. If you expected it to exist, it belongs to a different CVLR version "
                    f"or to a crate this project does not depend on — do not use it."
                )
            body = "\n".join(hits)
            return f"{body}\n… (more matches than shown; search a longer name)" if capped else body
