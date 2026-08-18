"""The ``run-confined`` launcher provider — the first real :class:`SandboxProvider`.

Maps a tool-agnostic :class:`SandboxPolicy` to an invocation of the ``run-confined``
trusted Rust binary (``rust/run-confined``), which applies Landlock + seccomp +
rlimits + a scrubbed env to itself and then ``execve``s the command
(``docs/command-sandbox.md`` §6). This module is deliberately *separate* from the
:mod:`composer.sandbox.policy` seam: it is wired in as the ``launcher``
``composer.sandbox_providers`` entry point (pyproject.toml) and loaded only when that
provider is actually constructed, so the seam never imports a concrete mechanism.

``wrap`` is pure argv construction (unit-testable, no subprocess); ``available``
shells out to ``run-confined --probe`` once to confirm the kernel supports Landlock
(fail-closed otherwise) — asynchronously, so probing never blocks the event loop.
"""

import asyncio
import os
import shutil
import sysconfig
from pathlib import Path

from composer.sandbox.policy import (
    Availability,
    LaunchSpec,
    Reason,
    SandboxPolicy,
)

_BIN_NAME = "run-confined"
_PROBE_TIMEOUT_S = 10


def _resolve_binary() -> str | None:
    """Locate the ``run-confined`` binary: ``$RUN_CONFINED_BIN`` → ``PATH`` → this
    interpreter's scripts dir. ``None`` if unbuilt.

    The env var is for deployments that mount the binary elsewhere (the sandbox compose
    overlay). The two probes both target the normal development install:
    ``rust/run-confined`` builds as a maturin *bin* wheel that ``uv sync`` puts in
    ``.venv/bin`` (see the ``apps`` dependency group) — PATH finds it under `uv run` or
    an activated venv, and ``sysconfig`` finds it when the venv's interpreter is invoked
    by path without activation."""
    override = os.environ.get("RUN_CONFINED_BIN")
    if override and Path(override).is_file():
        return override
    on_path = shutil.which(_BIN_NAME)
    if on_path:
        return on_path
    in_venv = Path(sysconfig.get_path("scripts")) / _BIN_NAME
    return str(in_venv) if in_venv.is_file() else None


class LauncherProvider:
    """Confines commands via the ``run-confined`` launcher (Landlock + seccomp)."""

    name = "launcher"

    def __init__(self, binary: str | None = None):
        # Resolve at construction so `available()` and `wrap()` agree on the path;
        # tests pass an explicit binary to keep `wrap()` golden-testable offline.
        self._binary = binary if binary is not None else _resolve_binary()

    @property
    def binary(self) -> str | None:
        return self._binary

    async def available(self) -> Availability:
        if self._binary is None:
            return Reason(
                f"{_BIN_NAME} binary not found; run `uv sync` (builds it from "
                f"rust/run-confined into .venv/bin) or set RUN_CONFINED_BIN"
            )
        try:
            proc = await asyncio.create_subprocess_exec(
                self._binary,
                "--probe",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            return Reason(f"{_BIN_NAME} --probe could not run: {e}")
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_PROBE_TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        if proc.returncode != 0:
            reason = stderr.decode().strip() or f"{_BIN_NAME} --probe reported no Landlock support"
            return Reason(reason)
        return "ok"

    def argv_prefix(self, policy: SandboxPolicy) -> list[str]:
        """The ``run-confined …  --`` argv that confines ``policy``, up to and
        including the ``--`` separator: a caller launches the command as
        ``[*argv_prefix(policy), program, *args]``.

        Emits ``--allow-env NAME=VALUE`` (explicit values — the allowlist holds only
        benign build vars, never secrets)."""
        argv: list[str] = [self._binary or _BIN_NAME]
        for p in policy.ro_paths:
            argv += ["--ro", str(p)]
        for p in policy.rw_paths:
            argv += ["--rw", str(p)]
        for name, value in policy.env_allowlist.items():
            argv += ["--allow-env", f"{name}={value}"]
        if policy.network:
            argv.append("--allow-network")
        if policy.mem_bytes is not None:
            argv += ["--rlimit-as", str(policy.mem_bytes)]
        if policy.cpu_seconds is not None:
            argv += ["--rlimit-cpu", str(policy.cpu_seconds)]
        if policy.nproc is not None:
            argv += ["--rlimit-nproc", str(policy.nproc)]
        if policy.fsize_bytes is not None:
            argv += ["--rlimit-fsize", str(policy.fsize_bytes)]
        argv.append("--")
        return argv

    def wrap(self, policy: SandboxPolicy, program: str, args: list[str]) -> LaunchSpec:
        """Build the ``run-confined … -- program args`` argv from ``policy``.

        ``env`` stays ``None``: the launcher inherits AutoProver's env but scrubs it
        to the allowlist for the child, so the child's environment is fully determined
        by the flags in :meth:`argv_prefix`."""
        return LaunchSpec(argv=(*self.argv_prefix(policy), program, *args), env=None)
