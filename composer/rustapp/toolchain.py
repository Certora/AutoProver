"""The seam for **the analyzed project's build system** — the two things the generic host wants to
know about a target it does not itself understand: where its code lives as a unit of that build
system, and how to prepare/build its workspace.

Both are knowledge about the *ecosystem under analysis*, not about implementing a backend in Rust. An
application is written in Rust; the project it analyzes need not be — a Cargo crate's package and lib
names, an Anchor IDL, a Move package's named addresses are all one ecosystem's vocabulary. So the
framework declares this seam and the application that needs it registers an implementation per chain.
Like the ecosystem registry and :mod:`composer.tools.rag_env`, it is a declarative tag → one concrete
implementation, not an application fork: a chain's entry is shared by every wheel targeting it
(Crucible's fuzz harness and a future CVLR backend read a Cargo manifest and build a Solana program
the same way).

**What crosses the seam is chain-shaped and opaque to everything here.** Both methods speak
``dict[str, Any]`` (Rust: ``autoprover_sdk::chain::ChainData``), typed at each end and nowhere in
between: the implementation registered here and the wheels targeting that chain share those types
through the chain's own support crate (``rust/autoprover-solana``), while the host only transports
them. Which type is inside follows from the wheel's declared ``ecosystem``, not from inspecting keys.
That is what makes a new ecosystem a registration rather than an edit to
:mod:`composer.rustapp.wire`.

**No chain has an entry yet.** The two methods are therefore reached in deliberately different ways:

* :func:`source_unit` **degrades**. An empty answer is already a documented state — it is what a
  language with no such unit yields, and what an unreadable layout yields — and the wheel fills the
  gaps from its own convention (``SolanaSourceUnit::resolved``). So "no toolchain" is
  indistinguishable from "nothing to resolve", which is the honest answer.
* :func:`project_toolchain` **raises**. A plan that only places files never asks for it (see
  ``WorkspacePrep.needs_toolchain``), so reaching it means the wheel asked for work nothing can
  perform; silently skipping it would surface much later as a mystifying compile error in the first
  authored draft. Same treatment an unknown ecosystem or an unregistered RAG corpus gets, for the same
  reason.

The wheel never supplies a command line either way — only *what* to prepare (file contents, plus a
request its chain's toolchain understands) — so the network posture stays Python-owned.
"""

from typing import Any, Protocol

from composer.pipeline.ecosystem import ChainTag
from composer.rustapp.wire import AuthorInput, WorkspacePrep
from composer.sandbox.config import SandboxConfig
from composer.spec.context import SourceFields


class ProjectToolchain(Protocol):
    """Everything the host needs of a project whose build system it does not understand.

    One object per chain rather than two registries, because both questions are the same knowledge:
    the thing that can read a Cargo manifest is the thing that can drive Cargo."""

    def source_unit(self, source: SourceFields) -> dict[str, Any]:
        """Where ``source``'s code lives as a unit of this chain's build system — for Cargo, the crate
        whose manifest owns the main source file, and what that crate is called.

        Returns the chain-shaped facts the wheel receives as ``AuthorInput.source_unit``; empty means
        "nothing resolved, apply your own convention". Never raises: a layout it cannot read is that
        same empty answer, not a failure."""
        ...

    async def prepare(
        self,
        plan: WorkspacePrep,
        input: AuthorInput,
        *,
        source: SourceFields,
        sandbox: SandboxConfig | None,
        timeout_s: int,
    ) -> dict[str, Any]:
        """Execute the toolchain half of a ``workspace_prep`` plan.

        Called only when :attr:`~composer.rustapp.wire.WorkspacePrep.needs_toolchain`, and only after
        the plan's ``files`` are in place — the manifest a warm or build reads is usually one of them.

        * ``plan`` — the wheel's parsed plan. :attr:`~composer.rustapp.wire.WorkspacePrep.toolchain_request`
          is the field addressed to this call, in the shape this chain defines.
        * ``input`` — the ``AuthorInput`` the plan was produced from, so the implementation can read
          its own declared args out of ``args`` (Crucible passes ``program_idl`` there) without the
          generic host having to know which keys mean anything.
        * ``source`` — what is being analyzed. ``project_root`` is the workdir the host just wrote the
          plan's ``files`` under and the root every build runs in; every path the implementation writes
          below it must go through :func:`composer.rustapp.adapter.confined_target`, as the host's own
          writes do. The rest of the fields are there so an implementation can resolve its own project
          facts (the crate owning ``relative_path``, say) rather than being handed a shape the
          framework would have to understand.
        * ``sandbox`` — the run's confinement, or ``None`` when unsandboxed. Fetches run *unconfined*
          (a fetch executes no untrusted code); anything that compiles runs confined + offline
          (``docs/command-sandbox.md`` §5).

        Returns what the prep established, chain-shaped, for the host to report back to the wheel as
        ``AuthorInput.prep_facts`` — empty when the plan asked for nothing it had to establish."""
        ...


#: Registered implementations, by chain. An entry is added together with the module it binds — a tag
#: whose implementation doesn't exist would pass every check here and then fail at the first plan that
#: needs it. Empty is a working state: see the module docstring for what each method does then.
PROJECT_TOOLCHAINS: dict[ChainTag, ProjectToolchain] = {}


def source_unit(chain: ChainTag, source: SourceFields) -> dict[str, Any]:
    """Where ``source``'s code lives as a build-system unit, per ``chain``'s toolchain — empty when
    the chain has none, which the wheel reads as "apply your own convention"."""
    toolchain = PROJECT_TOOLCHAINS.get(chain)
    return toolchain.source_unit(source) if toolchain is not None else {}


def project_toolchain(chain: ChainTag) -> ProjectToolchain:
    """The implementation for ``chain``. Raises when there is none: the wheel asked for preparation
    nothing here can perform, and every alternative to failing now (skip it, establish nothing)
    defers the same failure to a place where it reads as the authoring agent's fault."""
    toolchain = PROJECT_TOOLCHAINS.get(chain)
    if toolchain is None:
        known = sorted(PROJECT_TOOLCHAINS)
        raise ValueError(
            f"the wheel's workspace_prep asks to prepare the {chain} project, but no project "
            f"toolchain is registered for that chain "
            f"({f'registered: {known}' if known else 'none is registered yet'}). Register one in "
            "composer.rustapp.toolchain.PROJECT_TOOLCHAINS, or have the plan place files only."
        )
    return toolchain
