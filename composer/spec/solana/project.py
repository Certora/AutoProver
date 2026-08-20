"""Solana's half of the project seam — the host side of ``rust/autoprover-solana``.

:mod:`composer.rustapp.toolchain` declares one seam for "a project whose build system the host does
not understand" and carries everything crossing it as an opaque JSON object, because a Cargo package
name and an Anchor IDL are one ecosystem's vocabulary rather than the framework's. This module is the
other end of that: the three payload shapes, and the :class:`SolanaToolchain` that produces them.

The shapes are hand-written mirrors of the Rust structs in ``rust/autoprover-solana`` — the crate
every Solana wheel reads them through — so keep the field names in lockstep. Unlike
:mod:`composer.rustapp.wire`, this pair is not a *framework* ABI: nothing here is generic, and a
second ecosystem gets its own module rather than a field on any of these.

The build capability itself (``cargo-build-sbf``, ``anchor idl build``, the program-id resolution an
IDL needs) stays in :mod:`composer.spec.solana.build`. This module is the seam; that one is the
toolchain.
"""

import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from composer.rustapp.adapter import confined_target
from composer.rustapp.wire import AuthorInput, WorkspacePrep
from composer.sandbox.config import SandboxConfig
from composer.sandbox.recipes import sandbox_cargo_home
from composer.spec.cargo import ProgramCrate, resolve_program_crate
from composer.spec.context import SourceFields
from composer.spec.solana.build import (
    build_program,
    idl_with_program_id,
    warm_cargo_cache,
)

_log = logging.getLogger(__name__)


class _Mirror(BaseModel):
    """Base for the three payloads. Rejects unknown fields, matching the ``deny_unknown_fields`` on
    the Rust side: a key one half sends and the other doesn't declare means the wheel and this
    toolchain disagree about the chain, which should stop the run naming the key."""

    model_config = ConfigDict(extra="forbid")


class SolanaSourceUnit(_Mirror):
    """Where the analyzed code lives as a Cargo compilation unit, as the wheel receives it.

    The lossy copy of :class:`composer.spec.cargo.ProgramCrate`: an unknown Anchor requirement is
    ``None`` there and ``""`` here, because the wire form has no third state. Anything on this side
    that needs the distinction (filling in an IDL's program id) works with the resolved type, and the
    flattening happens once, on the way out."""

    dir: str = ""
    package: str = ""
    lib: str = ""
    anchor: str = ""

    @classmethod
    def of(cls, crate: ProgramCrate) -> "SolanaSourceUnit":
        return cls(dir=crate.dir, package=crate.package, lib=crate.lib, anchor=crate.anchor or "")


class SolanaPrep(_Mirror):
    """What a wheel asks this toolchain to do beyond writing the plan's files."""

    #: Project-relative manifest dirs to ``cargo fetch`` (unconfined, network) so a later confined +
    #: offline build finds every dep warm.
    warm_dirs: list[str] = Field(default_factory=list)
    #: The build artifact to produce — the crate's *lib* target, not the analysis identifier.
    build_program: str | None = None
    #: Where the wheel wants the program's IDL, workdir-relative.
    idl_dest: str | None = None


class SolanaPrepFacts(_Mirror):
    """What the prep established, as the wheel reads it back."""

    #: Where the IDL was placed, project-root-relative. Set means the file *is in place*.
    idl: str | None = None

    def as_facts(self) -> dict[str, Any]:
        """The wire form. Unset fields are dropped rather than sent as ``null``, because an empty
        object is the seam's spelling of "the prep established nothing" — a key present and empty
        would read as something being in place at an empty path."""
        return self.model_dump(exclude_none=True)


class SolanaToolchain:
    """Solana's :class:`~composer.rustapp.toolchain.ProjectToolchain`.

    Everything here is knowledge about the *analyzed* program's toolchain, which is why it lives with
    the ecosystem rather than with the framework — and why one entry serves every wheel targeting the
    chain (Crucible's fuzz harness and a future CVLR backend read the same manifest and build the same
    program)."""

    def source_unit(self, source: SourceFields) -> dict[str, Any]:
        """The Cargo crate whose manifest owns the main source file.

        Empty when the layout couldn't be read — the seam's spelling of "nothing resolved", and
        deliberately not an all-empty unit, which would claim a crate at the project root under the
        name ``""``. The wheel then applies its own convention
        (``SolanaSourceUnit::resolved``)."""
        crate = resolve_program_crate(source.project_root, source.relative_path)
        if crate is None:
            return {}
        return SolanaSourceUnit.of(crate).model_dump()

    async def prepare(
        self,
        plan: WorkspacePrep,
        input: AuthorInput,
        *,
        source: SourceFields,
        sandbox: SandboxConfig | None,
        timeout_s: int,
    ) -> dict[str, Any]:
        """Execute the toolchain half of a wheel's ``workspace_prep`` plan with this chain's tools.

        Only reached when the plan asks for more than files, and only after the host has written them
        (the manifest a warm or build reads is usually one). In order: ``cargo fetch`` each
        ``warm_dirs`` — but only when a sandbox is enabled, since the point of warming is that the
        later confined+offline build finds its deps already there — then build ``build_program``,
        then place the program's IDL if the wheel asked for one.

        Network stays Python-owned and the posture is the one ``docs/command-sandbox.md`` §5
        describes: fetches run *unconfined* (a fetch executes no untrusted code), the code-executing
        build runs *confined + offline* (:func:`~composer.spec.solana.build.build_program` handles
        both). The wheel supplies only which dirs/program — never a command line — and every path it
        names goes through :func:`~composer.rustapp.adapter.confined_target`, as the host's own
        writes do.

        The crate is resolved here rather than read off ``input.source_unit``: that is the lossy wire
        copy (unknown Anchor spelled ``""``), and filling in an IDL's program id needs to know whether
        anything was resolved at all. Same function and same inputs as :meth:`source_unit`, which
        produced the copy, so the two agree.
        """
        request = SolanaPrep.model_validate(plan.toolchain_request)
        workdir = Path(source.project_root)
        crate = resolve_program_crate(source.project_root, source.relative_path)
        idl_dest = request.idl_dest

        if request.warm_dirs and sandbox is not None and sandbox.enabled:
            # Warm into the SAME private CARGO_HOME the confined offline build will read.
            cargo_home = sandbox_cargo_home(str(workdir))
            for d in request.warm_dirs:
                await warm_cargo_cache(
                    confined_target(workdir, d), cargo_home=cargo_home, timeout_s=timeout_s
                )

        # An operator-supplied IDL wins over building one — for a program whose own toolchain isn't
        # installed (the usual reason the wheel wants an IDL at all), `anchor idl build` can't run.
        supplied = input.args.get("program_idl") or None
        idl_src = Path(supplied) if (idl_dest and supplied) else None
        if idl_src is not None and not idl_src.is_file():
            raise RuntimeError(f"--program-idl: no such file: {idl_src}")

        if request.build_program:
            built = await build_program(
                str(workdir), request.build_program, with_idl=bool(idl_dest) and idl_src is None,
                timeout_s=timeout_s, sandbox=sandbox,
            )
            if idl_dest and idl_src is None:
                idl_src = built.idl_path

        if not idl_dest:
            return SolanaPrepFacts().as_facts()
        if idl_src is None:
            raise RuntimeError(
                "the harness must generate the program's types from its IDL (it cannot link the "
                "program's crate directly), but no IDL could be produced: `anchor idl build` did "
                "not emit one, which usually means the program's own anchor CLI version isn't "
                "installed. Supply one with --program-idl <file> — any Anchor IDL format, including "
                "the pre-0.30 layout."
            )
        dest = confined_target(workdir, idl_dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Normalized on the way in: an IDL must name the program's address, and the one `anchor idl
        # build` emits for a pre-0.30 program doesn't (see ``idl_with_program_id``).
        dest.write_text(idl_with_program_id(idl_src.read_text(), project_root=workdir, crate=crate))
        _log.info("harness IDL: %s -> %s", idl_src, idl_dest)
        return SolanaPrepFacts(idl=idl_dest).as_facts()
