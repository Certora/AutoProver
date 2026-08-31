"""Putting CVLR's own source in front of the agents.

``docs/cvlr-backend-plan.md`` §5.5 is the argument: CVL's implementation lives inside the Prover and
cannot be read, so a curated manual plus recall is genuinely all an agent has. CVLR is the opposite —
it is a **cargo dependency of the project under verification**, so every macro definition and every
helper signature is on disk in the exact version the build resolves. We have less prose than EVM and
more ground truth, and this is the module that spends it.

**Its own tool set, not a layer of the project VFS.** Layering would put ``src/lib.rs`` from the
crate and from the project in one flat namespace, and the materializer dumps everything not globally
excluded, so crate sources would land in the tree the prover and the audit DB see. The combination
actually wanted from a layer — agent-visible, materializer-excluded — is not expressible today
(``forbidden_read`` hides from the agent only, ``global_exclude`` hides from both). A separate tool
set sidesteps all of it, and the tool's own name is the affordance that says what it is for.

The tree those tools read is :mod:`composer.spec.cvlr.crate_mount`, which is deliberately free of
this module's dependencies so that a plain script can reuse it.
"""

from langchain_core.tools import BaseTool
from pydantic import Field

from composer.spec.cvlr.crate_mount import MAX_MATCHES, MountedCrates, mount
from graphcore.tools.schemas import WithAsyncDependencies

#: Re-exported so a consumer of the agent-facing tools needs one import, not two.
__all__ = ["MAX_MATCHES", "MountedCrates", "mount", "cvlr_source_tools"]


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


def cvlr_source_tools(crates: MountedCrates) -> tuple[BaseTool, ...]:
    """The read-only tool set over these crates.

    Named ``cvlr_source_*`` rather than reusing the project's ``get_file`` / ``list_files``: the
    two surfaces answer different questions over different trees, and a name that says which is
    what keeps an agent from asking one of them about the other.

    Always bind these together with :meth:`~composer.spec.cvlr.crate_mount.MountedCrates.statement`.
    Tools with no statement leave an agent unaware that authoritative source is reachable, and a
    statement with no tools invites it to fabricate the reads — which is why both take the same
    ``MountedCrates`` and why a caller reaches them from one ``None`` check on :func:`mount`."""
    bound = (CvlrSourceFiles.bind(crates), CvlrSourceRead.bind(crates), CvlrSourceSearch.bind(crates))
    names = ("cvlr_source_files", "cvlr_source_read", "cvlr_source_search")
    return tuple(builder.as_tool(name) for builder, name in zip(bound, names))
