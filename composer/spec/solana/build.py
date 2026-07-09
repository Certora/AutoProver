"""Build a Solana program to sBPF (and, optionally, its IDL).

The shared "Solana build capability" (``docs/crucible-application.md`` §5.1):
``source → .so [+ IDL]``. It is deliberately backend-agnostic — the Crucible
backend calls it in *no-munge* mode (build the program as-is), and a future
Certora-Prover/CVLR backend will call it in *munge-and-rebuild* mode (rewrite the
source first). Both route through the same :func:`run_local_command` choke point
the ``RunCommand`` effect uses, so phase-6 sandboxing (§7.4) wraps one path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from composer.sandbox.command import CommandResult, run_local_command
from composer.sandbox.config import SandboxConfig

_log = logging.getLogger(__name__)

DEFAULT_BUILD_TIMEOUT_S = 600


class BuildError(RuntimeError):
    """A build step (``cargo-build-sbf`` / ``anchor idl``) failed."""

    def __init__(self, step: str, result: CommandResult):
        self.step = step
        self.result = result
        # Keep the tail of stderr — the actionable part of a cargo error.
        super().__init__(f"{step} failed (exit {result.exit_code}):\n{result.stderr[-2000:]}")


@dataclass(frozen=True)
class BuiltProgram:
    """Where the build left its outputs."""

    program: str
    so_path: Path
    idl_path: Path | None


async def build_program(
    project_root: str | Path,
    program: str,
    *,
    build_binary: str = "cargo-build-sbf",
    anchor_binary: str = "anchor",
    with_idl: bool = False,
    timeout_s: int = DEFAULT_BUILD_TIMEOUT_S,
    sandbox: SandboxConfig | None = None,
) -> BuiltProgram:
    """Compile ``program`` in the workspace at ``project_root`` to
    ``target/deploy/<program>.so``. If ``with_idl``, also try ``anchor idl build``
    (best-effort — not every project has an ``Anchor.toml``; the same-version
    harness path depends on the program crate directly and needs no IDL).

    ``cargo-build-sbf`` runs the user program's ``build.rs`` natively, so it is
    confined by ``sandbox`` when one is supplied (``docs/command-sandbox.md``);
    ``None`` runs it unsandboxed (trusted input only).

    Raises :class:`BuildError` if the ``.so`` is not produced.
    """
    root = Path(project_root)
    provider = policy = None
    if sandbox is not None and sandbox.enabled:
        provider = sandbox.resolve_provider()
        policy = sandbox.build_policy(root)

    res = await run_local_command(
        build_binary, [], {}, workdir=root, timeout_s=timeout_s, provider=provider, policy=policy
    )
    so = root / "target" / "deploy" / f"{program}.so"
    if res.exit_code != 0 or not so.is_file():
        raise BuildError("cargo-build-sbf", res)

    idl_path: Path | None = None
    if with_idl:
        out_rel = f"target/idl/{program}.json"
        idl_res = await run_local_command(
            anchor_binary, ["idl", "build", "-o", out_rel], {}, workdir=root,
            timeout_s=timeout_s, provider=provider, policy=policy,
        )
        candidate = root / out_rel
        if idl_res.exit_code == 0 and candidate.is_file():
            idl_path = candidate
        else:
            _log.warning(
                "IDL build did not produce %s (exit %s); continuing without an IDL "
                "(same-version harnesses depend on the program crate directly). stderr tail:\n%s",
                candidate,
                idl_res.exit_code,
                idl_res.stderr[-500:],
            )

    return BuiltProgram(program=program, so_path=so, idl_path=idl_path)
