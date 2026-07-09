"""The ``run-confined`` launcher provider — the first real :class:`SandboxProvider`.

Maps a tool-agnostic :class:`SandboxPolicy` to an invocation of the ``run-confined``
trusted Rust binary (``rust/run-confined``), which applies Landlock + seccomp +
rlimits + a scrubbed env to itself and then ``execve``s the command
(``docs/command-sandbox.md`` §6). This module is deliberately *separate* from the
:mod:`composer.sandbox.policy` seam: importing it registers the ``"launcher"``
provider, so the seam never imports a concrete mechanism.

``wrap`` is pure argv construction (unit-testable, no subprocess); ``available``
shells out to ``run-confined --probe`` once to confirm the kernel supports Landlock
(fail-closed otherwise).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from composer.sandbox.policy import (
    Availability,
    LaunchSpec,
    SandboxPolicy,
    register_provider,
)

_BIN_NAME = "run-confined"
_PROBE_TIMEOUT_S = 10


def _resolve_binary() -> str | None:
    """Locate the ``run-confined`` binary: ``$RUN_CONFINED_BIN`` → ``PATH`` → the
    dev build under ``rust/target/release`` (repo-relative). ``None`` if unbuilt."""
    override = os.environ.get("RUN_CONFINED_BIN")
    if override and Path(override).is_file():
        return override
    on_path = shutil.which(_BIN_NAME)
    if on_path:
        return on_path
    # Dev fallback: composer/sandbox/launcher.py → repo root is parents[2].
    repo_root = Path(__file__).resolve().parents[2]
    cand = repo_root / "rust" / "target" / "release" / _BIN_NAME
    return str(cand) if cand.is_file() else None


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

    def available(self) -> Availability:
        if self._binary is None:
            return Availability(
                ok=False,
                reason=(
                    f"{_BIN_NAME} binary not found; build rust/run-confined "
                    f"(cargo build -p run-confined --release) or set RUN_CONFINED_BIN"
                ),
            )
        try:
            proc = subprocess.run(
                [self._binary, "--probe"],
                capture_output=True,
                text=True,
                timeout=_PROBE_TIMEOUT_S,
            )
        except OSError as e:
            return Availability(ok=False, reason=f"{_BIN_NAME} --probe could not run: {e}")
        if proc.returncode != 0:
            reason = proc.stderr.strip() or f"{_BIN_NAME} --probe reported no Landlock support"
            return Availability(ok=False, reason=reason)
        return Availability(ok=True)

    def wrap(self, policy: SandboxPolicy, program: str, args: list[str]) -> LaunchSpec:
        """Build the ``run-confined … -- program args`` argv from ``policy``.

        Emits ``--allow-env NAME=VALUE`` (explicit values — the allowlist holds only
        benign build vars, never secrets). ``env`` stays ``None``: the launcher
        inherits AutoProver's env but scrubs it to the allowlist for the child, so
        the child's environment is fully determined by the flags."""
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
        argv += ["--", program, *args]
        return LaunchSpec(argv=tuple(argv), env=None)


# Registering on import keeps the `composer.sandbox.policy` seam free of any
# concrete-mechanism import. Consumers (RealEffects, tests) `import` this module to
# make ``get_provider("launcher")`` resolvable.
register_provider("launcher", LauncherProvider)
