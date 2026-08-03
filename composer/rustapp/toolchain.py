"""The seams for **the analyzed project's build system** — the two things the generic host wants to
know about a target it does not itself understand: where its code lives as a compilation unit, and
how to prepare/build its workspace.

Both are knowledge about the *ecosystem under analysis*, not about implementing a backend in Rust.
A Rust backend need not depend on the analyzed crate, build anything, or use the sandbox at all — so
the framework declares these seams and the application that needs them registers an implementation
per chain. Like the ecosystem registry and :mod:`composer.tools.rag_env`, each is a declarative
tag → one concrete implementation, not an application fork: a chain's entry is shared by every wheel
targeting it (Crucible's fuzz harness and a future CVLR backend read a Cargo manifest and build a
Solana program the same way).

**Neither map has an entry yet**, and the two are unregistered in deliberately different ways:

* :func:`source_crate` **degrades**. An all-empty
  :class:`~composer.rustapp.wire.ProgramCrate` is already a documented state — it is what Solidity
  yields, and what an unresolvable Rust layout yields — and the SDK's ``ProgramCrate::resolved``
  fills the gaps from the wheel's own convention. So "no resolver" is indistinguishable from
  "nothing to resolve", which is the honest answer.
* :func:`workspace_toolchain` **raises**. A plan that only places files never asks for it (see
  ``WorkspacePrep.needs_toolchain``), so reaching it means the wheel asked for a warm/build/IDL that
  nothing can perform; silently skipping it would surface much later as a mystifying compile error
  in the first authored draft. Same treatment an unknown ecosystem or an unregistered RAG corpus
  gets, for the same reason.

The wheel never supplies a command line either way — only *what* to prepare (file contents, which
dirs, which program) — so the network posture stays Python-owned.
"""

from typing import Callable, Protocol

from composer.pipeline.ecosystem import ChainTag
from composer.rustapp.wire import AuthorInput, ProgramCrate, WorkspacePrep
from composer.sandbox.config import SandboxConfig
from composer.spec.context import SourceFields


#: Resolves the analyzed source's compilation unit — for Cargo, the crate whose manifest owns the
#: main source file, and what that crate is called. Returns the wire shape the wheel receives as
#: ``AuthorInput.program_crate``; an all-empty one means "nothing resolved, apply your own
#: convention". Never raises: a layout it cannot read is that same empty answer, not a failure.
type SourceCrateResolver = Callable[[SourceFields], ProgramCrate]


#: Registered resolvers, by chain. Empty is a working state — see the module docstring.
SOURCE_CRATES: dict[ChainTag, SourceCrateResolver] = {}


def source_crate(chain: ChainTag, source: SourceFields) -> ProgramCrate:
    """Where ``source``'s code lives as a compilation unit, per ``chain``'s resolver — all-empty
    when the chain has none, which the wheel reads as "apply your own convention"."""
    resolver = SOURCE_CRATES.get(chain)
    return resolver(source) if resolver is not None else ProgramCrate()


class WorkspaceToolchain(Protocol):
    """Executes the toolchain half of a ``workspace_prep`` plan against one chain's tooling.

    Called only when :attr:`~composer.rustapp.wire.WorkspacePrep.needs_toolchain`, and only after
    the plan's ``files`` are in place — the manifest a warm or build reads is usually one of them.

    * ``plan`` — the wheel's parsed plan; ``warm_dirs`` / ``build_program`` / ``idl_dest`` are the
      fields addressed to this call.
    * ``input`` — the ``AuthorInput`` the plan was produced from, so the implementation can read
      its own declared args out of ``context`` (Crucible passes ``program_idl`` there) without the
      generic host having to know which keys mean anything.
    * ``source`` — what is being analyzed. ``project_root`` is the workdir the host just wrote the
      plan's ``files`` under and the root every build runs in; every path the implementation writes
      below it must go through :func:`composer.rustapp.adapter.confined_target`, as the host's own
      writes do. The rest of the fields are there so an implementation can resolve its own project
      facts (the crate owning ``relative_path``, say) rather than being handed a shape the framework
      would have to understand.
    * ``sandbox`` — the run's confinement, or ``None`` when unsandboxed. Fetches run *unconfined*
      (a fetch executes no untrusted code); anything that compiles runs confined + offline
      (``docs/command-sandbox.md`` §5).

    Returns where the program's IDL was placed, relative to the project root, for the host to report
    back to the wheel as the ``idl`` context key — or ``None`` when the plan asked for none.
    """

    async def __call__(
        self,
        plan: WorkspacePrep,
        input: AuthorInput,
        *,
        source: SourceFields,
        sandbox: SandboxConfig | None,
        timeout_s: int,
    ) -> str | None: ...


#: Registered implementations, by chain. An entry is added together with the module it lives in —
#: see the module docstring for why an empty map is the intended state on its own.
WORKSPACE_TOOLCHAINS: dict[ChainTag, WorkspaceToolchain] = {}


def workspace_toolchain(chain: ChainTag) -> WorkspaceToolchain:
    """The implementation for ``chain``. Raises when there is none: the wheel asked for a build
    that nothing here can perform, and every alternative to failing now (skip it, place no IDL)
    defers the same failure to a place where it reads as the authoring agent's fault."""
    toolchain = WORKSPACE_TOOLCHAINS.get(chain)
    if toolchain is None:
        known = sorted(WORKSPACE_TOOLCHAINS)
        raise ValueError(
            f"the wheel's workspace_prep asks to warm/build/IDL the {chain} project, but no "
            f"workspace toolchain is registered for that chain "
            f"({f'registered: {known}' if known else 'none is registered yet'}). Register one in "
            "composer.rustapp.toolchain.WORKSPACE_TOOLCHAINS, or have the plan place files only."
        )
    return toolchain
