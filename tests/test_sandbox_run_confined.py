"""Integration test: `run_local_command` actually confines via the launcher provider.

This proves the *wiring* (step 3) end-to-end — the runner routes a command through
a real `SandboxProvider` and the confinement takes effect — as opposed to the pure
argv/golden tests elsewhere. Skipped unless the `run-confined` binary is built and
the kernel supports Landlock (so CI without the Rust build stays green); the full
escape gate on the real Crucible build is step 5.
"""

import os
from pathlib import Path

import pytest

from composer.sandbox.command import run_local_command
from composer.sandbox.launcher import LauncherProvider
from composer.sandbox.policy import SandboxPolicy, SandboxUnavailable

pytestmark = pytest.mark.asyncio

_PROVIDER = LauncherProvider()
_needs_sandbox = pytest.mark.skipif(
    not _PROVIDER.available().ok, reason="run-confined unbuilt or kernel lacks Landlock"
)


def _system_policy(workdir: Path) -> SandboxPolicy:
    """A minimal policy: workdir + the dev nodes rw, the system dirs ro. Deliberately
    does NOT grant /etc, so reading a host file outside the workdir is denied."""
    ro = tuple(p for p in (Path("/usr"), Path("/lib"), Path("/lib64"), Path("/bin")) if p.exists())
    rw = (workdir, *(Path(d) for d in ("/dev/null", "/dev/urandom") if Path(d).exists()))
    return SandboxPolicy(rw_paths=rw, ro_paths=ro, env_allowlist={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})


@_needs_sandbox
async def test_confined_command_can_write_workdir(tmp_path):
    res = await run_local_command(
        "bash", ["-c", "echo hi > w.txt"], {}, workdir=tmp_path,
        provider=_PROVIDER, policy=_system_policy(tmp_path),
    )
    assert res.exit_code == 0, res.stderr
    assert (tmp_path / "w.txt").read_text().strip() == "hi"


@_needs_sandbox
async def test_confined_command_cannot_read_outside_workdir(tmp_path):
    outside = tmp_path.parent / f"secret-{tmp_path.name}.txt"
    outside.write_text("TOPSECRET")
    try:
        res = await run_local_command(
            "bash", ["-c", f"cat {outside} && echo LEAK || echo denied"], {}, workdir=tmp_path,
            provider=_PROVIDER, policy=_system_policy(tmp_path),
        )
    finally:
        outside.unlink(missing_ok=True)
    assert "TOPSECRET" not in res.stdout
    assert "LEAK" not in res.stdout
    assert "denied" in res.stdout


@_needs_sandbox
async def test_confined_command_has_no_network(tmp_path):
    res = await run_local_command(
        "python3",
        ["-c", "import socket; socket.socket(socket.AF_INET, socket.SOCK_STREAM); print('LEAK')"],
        {}, workdir=tmp_path, provider=_PROVIDER, policy=_system_policy(tmp_path),
    )
    assert res.exit_code != 0
    assert "LEAK" not in res.stdout


@_needs_sandbox
async def test_none_provider_is_not_confined(tmp_path):
    """Control: without a provider the same outside-read succeeds — proving it is the
    sandbox, not something else, doing the blocking above."""
    outside = tmp_path.parent / f"plain-{tmp_path.name}.txt"
    outside.write_text("readable")
    try:
        res = await run_local_command(
            "bash", ["-c", f"cat {outside}"], {}, workdir=tmp_path,  # provider=None (passthrough)
        )
    finally:
        outside.unlink(missing_ok=True)
    assert res.exit_code == 0
    assert "readable" in res.stdout


async def test_unavailable_provider_fails_closed(tmp_path):
    """A provider that reports unavailable must raise, never run unconfined."""

    class _Unavailable:
        name = "x"

        def available(self):
            from composer.sandbox.policy import Availability

            return Availability(ok=False, reason="nope")

        def wrap(self, policy, program, args):  # pragma: no cover - must not be called
            raise AssertionError("wrap reached despite unavailable")

    with pytest.raises(SandboxUnavailable):
        await run_local_command(
            "true", [], {}, workdir=tmp_path, provider=_Unavailable(), policy=SandboxPolicy()
        )
