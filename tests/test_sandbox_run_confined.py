"""Integration test: `run_local_command` actually confines via the launcher provider.

This proves the *wiring* (step 3) end-to-end — the runner routes a command through
a real `SandboxProvider` and the confinement takes effect — as opposed to the pure
argv/golden tests elsewhere. Skipped unless the `run-confined` binary is built and
the kernel supports Landlock (so CI without the Rust build stays green); the full
escape gate on the real Crucible build is step 5.
"""

import asyncio
import os
from pathlib import Path

import pytest

from composer.sandbox.command import run_local_command
from composer.sandbox.launcher import LauncherProvider
from composer.sandbox.policy import SandboxPolicy, SandboxUnavailable

pytestmark = pytest.mark.asyncio

_PROVIDER = LauncherProvider()
# Evaluated at collection time (no running loop), so drive the async probe with asyncio.run.
_needs_sandbox = pytest.mark.skipif(
    asyncio.run(_PROVIDER.available()) != "ok",
    reason="run-confined unbuilt or kernel lacks Landlock",
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
async def test_confined_command_denies_io_uring(tmp_path):
    """io_uring_setup is a known seccomp socket() bypass — must be denied.

    Use /usr/bin/python3 (not a venv shim): the sandbox does not grant the
    project .venv, so a PATH-resolved venv python fails before the probe runs.
    """
    res = await run_local_command(
        "/usr/bin/python3",
        [
            "-c",
            "import ctypes; c=ctypes.CDLL('libc.so.6',use_errno=True); "
            "p=(ctypes.c_char*256)(); "
            "fd=c.syscall(425,8,ctypes.byref(p)); "
            "print('LEAK' if fd>=0 else 'denied')",
        ],
        {},
        workdir=tmp_path,
        provider=_PROVIDER,
        policy=_system_policy(tmp_path),
    )
    assert res.exit_code == 0, res.stderr
    assert "LEAK" not in res.stdout
    assert "denied" in res.stdout


@_needs_sandbox
async def test_confined_command_denies_netlink_and_vsock(tmp_path):
    res = await run_local_command(
        "/usr/bin/python3",
        [
            "-c",
            "import socket; "
            "out=[]\n"
            "for fam,name in ((socket.AF_NETLINK,'nl'), (getattr(socket,'AF_VSOCK',40),'vs')):\n"
            "  try:\n"
            "    socket.socket(fam, socket.SOCK_STREAM if name=='vs' else socket.SOCK_RAW)\n"
            "    out.append(name+':LEAK')\n"
            "  except OSError:\n"
            "    out.append(name+':denied')\n"
            "print(' '.join(out))",
        ],
        {},
        workdir=tmp_path,
        provider=_PROVIDER,
        policy=_system_policy(tmp_path),
    )
    assert res.exit_code == 0, res.stderr
    assert "LEAK" not in res.stdout
    assert "nl:denied" in res.stdout
    assert "vs:denied" in res.stdout


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

        async def available(self):
            from composer.sandbox.policy import Reason

            return Reason("nope")

        def wrap(self, policy, program, args):  # pragma: no cover - must not be called
            raise AssertionError("wrap reached despite unavailable")

    with pytest.raises(SandboxUnavailable):
        await run_local_command(
            "true", [], {}, workdir=tmp_path, provider=_Unavailable(), policy=SandboxPolicy()
        )
