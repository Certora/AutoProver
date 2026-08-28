"""Solana's entry in the project-toolchain registry.

:mod:`composer.rustapp.toolchain` declares the seam and, until now, had no entries — its docstring
says so, and says why the two methods behave differently when nothing is registered. This is the
first registrant, and ``docs/cvlr-backend-plan.md`` §4.3 is the argument for putting it here rather
than inside the CVLR backend: the natural sharing axis of "read a Cargo manifest and build a Solana
program" is the *chain*, across products, and one of those products is not CVLR at all. Crucible's
fuzz harness wants the same two answers.

The dependency runs one way, and the wheel's wire types are therefore imported for typing only: Cargo
knowledge does not know what a wheel is, and ``composer.rustapp``'s package ``__init__`` imports the
registry that imports this module, so a runtime import back into it closes a cycle.

The two halves land in different states, deliberately, and the difference is not tidiness:

* :meth:`SolanaToolchain.source_unit` is **complete**. It replaces an empty answer — the state the
  seam documents as "apply your own convention" — with the real crate, read from ``cargo metadata``.
  Every consumer strictly gains.
* :meth:`SolanaToolchain.prepare` handles the two requests that are Cargo work, warming and building,
  and **refuses the IDL step up front**. Refusing up front is the whole point: a plan that asks for
  an IDL gets the same immediate failure it gets today from an unregistered chain, rather than a
  failure after a multi-minute build. Placing an Anchor IDL is Crucible's own knowledge — which file
  the operator supplied, how a program address is normalized into it — and inventing it here to fill
  a table cell would be worse than saying it is missing.
"""

import dataclasses
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from composer.cargo.metadata import read_workspace, read_workspace_sync
from composer.cargo.sbf import Built, sbf_build
from composer.cargo.session import CargoSession, WarmFailed
from composer.sandbox.config import SandboxConfig
from composer.spec.context import SourceFields

if TYPE_CHECKING:
    from composer.rustapp.wire import AuthorInput, WorkspacePrep

_log = logging.getLogger(__name__)

#: The keys a Solana ``toolchain_request`` may carry (Crucible's ``SolanaPrep``). Named here so an
#: unknown key is reported as such instead of silently ignored — a request nothing acts on is a plan
#: that believes work happened.
_REQUEST_KEYS = frozenset({"warm_dirs", "build_program", "idl_dest"})


class ToolchainRequestUnsupported(ValueError):
    """The plan asked this toolchain for preparation it does not perform.

    Raised before any work, so it costs a plan nothing to find out. Same treatment, and the same
    reason, as :func:`composer.rustapp.toolchain.project_toolchain` raising for an unregistered
    chain: every alternative defers the failure to a place where it reads as the agent's fault."""


@dataclasses.dataclass(frozen=True)
class SolanaToolchain:
    """Cargo, for a project whose verification artifact is an sBPF program."""

    def source_unit(self, source: SourceFields) -> dict[str, Any]:
        """The crate owning ``source``'s main file: ``{dir, package, lib}``.

        ``dir`` is relative to the project root, because that is the vocabulary every path crossing
        this seam uses. Never raises, and returns ``{}`` for anything it cannot read — a project that
        is not a Cargo workspace, a main file outside every member, a crate with no library target.
        Each of those is a real state the wheel already handles by applying its own convention, and
        none of them is improved by an exception.
        """
        root = Path(source.project_root)
        workspace = read_workspace_sync(root)
        if workspace is None:
            return {}
        owner = workspace.owning(root / source.relative_path)
        if owner is None or owner.lib_target is None:
            return {}
        try:
            relative = owner.root.resolve().relative_to(root.resolve())
        except ValueError:
            # The owning crate lives outside the project root — a path dependency reached from a
            # workspace above it. Its directory has no project-relative spelling, so there is nothing
            # honest to report.
            return {}
        return {
            "dir": str(relative),
            "package": owner.name,
            "lib": owner.lib_target,
        }

    async def prepare(
        self,
        plan: "WorkspacePrep",
        input: "AuthorInput",
        *,
        source: SourceFields,
        sandbox: SandboxConfig | None,
        timeout_s: int,
    ) -> dict[str, Any]:
        """Warm the dependency graph, then build the program, per ``plan.toolchain_request``.

        The trust split is :mod:`composer.cargo.session`'s: the warm runs unconfined and online
        because a fetch executes nothing, and the build runs confined and offline because it executes
        the analyzed project's ``build.rs``. ``sandbox`` being ``None`` means the caller asked for an
        unconfined run, and the session says so loudly.
        """
        request = dict(plan.toolchain_request)
        if unknown := sorted(set(request) - _REQUEST_KEYS):
            raise ToolchainRequestUnsupported(
                f"the Solana toolchain does not understand {', '.join(unknown)} "
                f"(it handles {', '.join(sorted(_REQUEST_KEYS))})"
            )
        if request.get("idl_dest"):
            raise ToolchainRequestUnsupported(
                "the Solana toolchain cannot place a program IDL yet: which file the operator "
                "supplied and how a program address is normalized into it is Anchor knowledge this "
                "registration does not carry. Supply the IDL through the wheel's own args, or "
                "implement it in composer.cargo.toolchain."
            )

        workdir = Path(source.project_root)
        session = CargoSession(
            workdir=workdir, sandbox=sandbox if sandbox is not None else SandboxConfig()
        )
        warm_dirs = tuple(Path(d) for d in request.get("warm_dirs") or ())
        warmed = await session.warm(manifest_dirs=warm_dirs, timeout_s=timeout_s)
        if isinstance(warmed, WarmFailed):
            # Non-fatal by contract: a partially warm cache still compiles what it covers, and the
            # build below reports the crate it could not find, which the fetch cannot name.
            _log.warning("cargo fetch did not complete: %s", warmed.diagnostics)

        program = request.get("build_program")
        if not program:
            return {}
        workspace = await read_workspace(workdir, offline=True)
        package = workspace.member(str(program)) if workspace is not None else None
        if package is None:
            raise ToolchainRequestUnsupported(
                f"the plan asks to build {program!r}, which is not a member of the Cargo workspace "
                f"at {workdir}"
            )
        built = await sbf_build(
            session, manifest_path=package.manifest_path, timeout_s=timeout_s
        )
        if not isinstance(built.verdict, Built):
            raise ToolchainRequestUnsupported(
                f"building {program} failed:\n{built.verdict.diagnostics}"
            )
        return {"executable": str(built.verdict.manifest.executables)}
