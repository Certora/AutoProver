"""The local-command runner behind the ``RunCommand`` effect.

A single choke point: materialize a set of files into a workdir, run a command
over them (as a child process, **never** a shell), and capture the result. Both
the IoC ``RunCommand`` effect (:meth:`composer.rustapp.adapter.RealEffects.run_command`)
and the Solana build/IDL step route through here — and any Python backend may too,
which is why this lives in :mod:`composer.sandbox` rather than under ``rustapp``.
The command sandbox (``docs/command-sandbox.md``) wraps exactly this one function.

**Trust boundary** (``docs/command-sandbox.md`` §2): the *caller* — a trusted Rust
decider or a trusted Python build step — supplies ``program`` and ``args``; only
file *contents* may derive from LLM output. We enforce two things here: the command
runs via ``exec`` (argv, no shell), and every written path is confined to the
workdir (no absolute paths, no ``..`` traversal).

.. note::
   This does **not** yet apply the sandbox (network-off, clean env, resource caps):
   the :mod:`composer.sandbox.policy` seam exists, but wiring a ``SandboxProvider``
   in *here* — behind the same signature — is the next step (``docs/command-sandbox.md``
   §9 step 3). Until then, run only on trusted input in a trusted environment.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_log = logging.getLogger(__name__)

# Generous default; individual callers (a fuzz run vs a quick dry-run) pass their own.
DEFAULT_TIMEOUT_S = 600

# Exit code we synthesize when the binary isn't on PATH (mirrors shells' 127).
NOT_FOUND_EXIT = 127


class UnsafePath(ValueError):
    """A requested file path is absolute or escapes the workdir."""


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str

    def as_observation(self) -> dict:
        """The ``Observation::CommandResult`` payload the IoC loop feeds back to Rust."""
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


def _confined_target(workdir: Path, rel: str) -> Path:
    """Resolve ``rel`` under ``workdir``, rejecting absolute paths / ``..`` escapes."""
    p = PurePosixPath(rel)
    if p.is_absolute() or ".." in p.parts:
        raise UnsafePath(
            f"file path {rel!r} is absolute or traverses outside the workdir"
        )
    target = workdir / p
    # Belt-and-suspenders: the resolved path must still live under the workdir.
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
) -> CommandResult:
    """Write ``files`` into ``workdir``, then run ``program args`` there and capture output.

    ``workdir`` persists across calls (a session materializes its crate once and
    runs several commands against it). Concurrency is bounded by ``sem`` when
    given — important because fuzzers are resource-hungry.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    for rel, contents in files.items():
        target = _confined_target(workdir, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)

    async def _run() -> CommandResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                program,
                *args,
                cwd=str(workdir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return CommandResult(NOT_FOUND_EXIT, "", f"{program}: not found on PATH")
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

    if sem is not None:
        async with sem:
            return await _run()
    return await _run()
