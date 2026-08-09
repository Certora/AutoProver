"""The local-command runner behind the ``RunCommand`` effect.

A single choke point: materialize a set of files into a workdir, run a command
over them (as a child process, **never** a shell), and capture the result. The
trusted Python build steps (the Solana sBPF build / IDL step) route through here.

(A Rust backend's own ``compile``/``validate`` toolchain runs no longer go through
this: they spawn the ``run-confined`` launcher directly from the wheel via
``autoprover_sdk::run_confined`` — see ``docs/rust-backend-api.md``. This runner and the
launcher share the same :mod:`composer.sandbox.policy` seam, which is why it lives in
:mod:`composer.sandbox` rather than under ``rustapp``.)

Optional confinement is applied via a :class:`~composer.sandbox.policy.SandboxProvider`
+ :class:`~composer.sandbox.policy.SandboxPolicy` (``docs/command-sandbox.md``):
``None`` / the ``none`` provider is a passthrough; the ``launcher`` provider wraps
the argv in ``run-confined`` (Landlock + seccomp) and is fail-closed.

**Trust boundary** (``docs/command-sandbox.md`` §2): the *caller* — a trusted Rust
decider or a trusted Python build step — supplies ``program`` and ``args``; only
file *contents* may derive from LLM output. We enforce path confinement here
(no absolute paths, no ``..`` traversal) in addition to whatever the provider does.
"""

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TypedDict

from composer.sandbox.policy import (
    NoneProvider,
    SandboxPolicy,
    SandboxProvider,
    ensure_available,
)

_log = logging.getLogger(__name__)

# Generous default; individual callers (a fuzz run vs a quick dry-run) pass their own.
DEFAULT_TIMEOUT_S = 600

# Exit code we synthesize when the binary isn't on PATH (mirrors shells' 127).
NOT_FOUND_EXIT = 127


class UnsafePath(ValueError):
    """A requested file path is absolute or escapes the workdir."""


class CommandResultObservation(TypedDict):
    """The ``Observation::CommandResult`` payload the IoC loop feeds back to Rust."""

    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str

    def as_observation(self) -> CommandResultObservation:
        """The ``Observation::CommandResult`` payload the IoC loop feeds back to Rust."""
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


def _confined_target(workdir: Path, rel: str) -> Path:
    """Resolve ``rel`` under ``workdir``, rejecting anything that escapes it.

    The resolved path must live under ``workdir`` — this rejects absolute paths
    (which ``workdir / p`` would adopt wholesale), ``..`` traversal, and symlinked
    components that would otherwise point outside.
    """
    target = workdir / PurePosixPath(rel)
    try:
        target.resolve().relative_to(workdir.resolve())
    except ValueError as e:
        raise UnsafePath(f"file path {rel!r} resolves outside the workdir") from e
    return target


async def run_local_command(
    program: str,
    args: list[str],
    files: dict[str, str],
    *,
    workdir: Path,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    sem: asyncio.Semaphore | None = None,
    provider: SandboxProvider | None = None,
    policy: SandboxPolicy | None = None,
    env_overlay: dict[str, str] | None = None,
) -> CommandResult:
    """Write ``files`` into ``workdir``, then run ``program args`` there and capture output.

    ``workdir`` persists across calls. 
    
    Concurrency is bounded by ``sem`` when given.

    ``provider`` selects the sandbox mechanism (``docs/command-sandbox.md``); with
    the default (``None`` → the ``none`` passthrough) the command runs exactly as
    before. A real provider maps ``policy`` → a confined launch and is **fail-closed**
    (raises :class:`~composer.sandbox.policy.SandboxUnavailable` if it can't confine,
    rather than running unsandboxed). The untrusted ``files`` are materialized by
    trusted Python (path-confined via ``_confined_target``) and then the command runs
    — as one unit under ``sem`` when given, so that when callers share a workdir the
    file-write and the run don't interleave (a concurrent caller can't overwrite these
    files between our write and our run). Path confinement complements the sandbox.

    ``env_overlay`` sets extra env vars on the child on top of what it would otherwise
    inherit — used by the *unsandboxed* prep steps (e.g. `cargo fetch` with a per-run
    ``CARGO_HOME``); the sandboxed path's env is fully governed by the provider/policy.
    """
    prov: SandboxProvider = provider if provider is not None else NoneProvider()
    await ensure_available(prov)  # fail-closed: raises before running if it can't confine
    spec = prov.wrap(policy if policy is not None else SandboxPolicy(), program, list(args))
    child_env = dict(spec.env) if spec.env is not None else None
    if env_overlay:
        # Overlay onto the effective env (the inherited parent env when the provider
        # didn't set one — i.e. the `none`/unsandboxed path).
        child_env = {**(child_env if child_env is not None else os.environ), **env_overlay}

    async def _run() -> CommandResult:
        # Materialize files + launch as one unit so a shared workdir stays consistent
        # for this command's duration (see `sem`).
        workdir.mkdir(parents=True, exist_ok=True)
        for rel, contents in files.items():
            target = _confined_target(workdir, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents)
        try:
            proc = await asyncio.create_subprocess_exec(
                *spec.argv,
                cwd=str(workdir),
                env=child_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return CommandResult(NOT_FOUND_EXIT, "", f"{spec.argv[0]}: not found on PATH")
        try:
            out_b, err_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_s
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return CommandResult(-1, "", f"command timed out after {timeout_s}s")
        rc = proc.returncode if proc.returncode is not None else -1
        return CommandResult(
            rc, out_b.decode(errors="replace"), err_b.decode(errors="replace")
        )

    async with (sem if sem is not None else contextlib.nullcontext()):
        return await _run()
