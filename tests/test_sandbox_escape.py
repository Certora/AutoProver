"""The escape suite — Part A of the Phase-6 gate (docs/command-sandbox.md §10).

A *malicious* program (standing in for a harness `setup()` / a program's `build.rs`)
is compiled with `rustc`, then run through the **real** `run-confined` launcher via
`run_local_command` under a Crucible-representative policy (`rust_build_policy`). It
attempts every escape and writes each result into the workdir (allowed); the test
reads them back and asserts *denied* for all. A no-sandbox control runs the same
binary unconfined and confirms the leaks would otherwise happen — proving it is the
sandbox doing the blocking.

Runnable without the full Crucible stack (std-only program, no crates, no network
needed to compile). Skipped unless `rustc` and a working launcher are present. The
*legitimate* half (a real `solana_vault` build+fuzz under the launcher) is the
expensive Part B in `tests/test_crucible_sandbox_gate.py`.
"""

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from composer.sandbox.command import run_local_command
from composer.sandbox.launcher import LauncherProvider
from composer.sandbox.recipes import rust_build_policy

pytestmark = pytest.mark.asyncio

_PROVIDER = LauncherProvider()
_needs = pytest.mark.skipif(
    shutil.which("rustc") is None or not _PROVIDER.available().ok,
    reason="needs rustc + a working run-confined launcher (Linux/Landlock)",
)

_ENV_CANARY = "ENVCANARY-a1b2c3"
_HOSTFILE_CANARY = "HOSTFILECANARY-d4e5f6"

# Standing in for hostile code in setup()/build.rs. std-only so it compiles offline.
_MALICIOUS_RS = """
use std::fs;
use std::net::{SocketAddr, TcpStream};
use std::time::Duration;

fn probe(name: &str, result: &str) {
    let _ = fs::write(format!("probe_{}.txt", name), result);
}

fn net(addr: &str) -> String {
    let sa: SocketAddr = addr.parse().unwrap();
    match TcpStream::connect_timeout(&sa, Duration::from_secs(2)) {
        Ok(_) => "LEAK:connected".to_string(),
        Err(_) => "denied".to_string(),
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let outside = args.get(1).cloned().unwrap_or_default();
    let parent_pid = args.get(2).cloned().unwrap_or_default();

    probe("env", &match std::env::var("ANTHROPIC_API_KEY") {
        Ok(v) => format!("LEAK:{}", v),
        Err(_) => "denied".to_string(),
    });

    probe("procenv", &match fs::read_to_string(format!("/proc/{}/environ", parent_pid)) {
        Ok(s) if s.contains("ENVCANARY") => "LEAK:found-canary".to_string(),
        Ok(_) => "LEAK:proc-readable".to_string(),
        Err(_) => "denied".to_string(),
    });

    probe("hostfile", &match fs::read_to_string(&outside) {
        Ok(s) => format!("LEAK:{}", s.trim()),
        Err(_) => "denied".to_string(),
    });

    probe("net_ext", &net("1.1.1.1:80"));
    probe("imds", &net("169.254.169.254:80"));
}
"""


def _compile(tmp_path: Path, workdir: Path) -> None:
    src = tmp_path / "malicious.rs"
    src.write_text(_MALICIOUS_RS)
    # Compiled UNSANDBOXED (we're testing runtime confinement, not the build here).
    subprocess.run(
        ["rustc", "-O", str(src), "-o", str(workdir / "malicious")],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    workdir = tmp_path / "work"
    workdir.mkdir()
    _compile(tmp_path, workdir)
    outside = tmp_path / "host_secret.txt"  # OUTSIDE the granted workdir
    outside.write_text(_HOSTFILE_CANARY)
    # Plant the secret in *this* process's env; run-confined must scrub it, and the
    # /proc/<ppid>/environ read (ppid = this pytest process) must be denied.
    monkeypatch.setenv("ANTHROPIC_API_KEY", _ENV_CANARY)
    return workdir, outside


@_needs
async def test_all_escapes_denied(scenario):
    workdir, outside = scenario
    policy = rust_build_policy(workdir)  # grants workdir + toolchains; NOT /proc, NOT `outside`
    res = await run_local_command(
        "./malicious", [str(outside), str(os.getpid())], {},
        workdir=workdir, provider=_PROVIDER, policy=policy,
    )
    assert res.exit_code == 0, res.stderr

    def probe(name: str) -> str:
        return (workdir / f"probe_{name}.txt").read_text().strip()

    # every vector denied — and specifically no canary leaked
    assert probe("env") == "denied"
    assert probe("procenv") == "denied"
    assert probe("hostfile") == "denied"
    assert probe("net_ext") == "denied"
    assert probe("imds") == "denied"
    for name in ("env", "procenv", "hostfile", "net_ext", "imds"):
        assert "LEAK" not in probe(name)


@_needs
async def test_control_unconfined_would_leak(scenario):
    """Without the sandbox the same binary reads the secret env + the host file —
    confirming the assertions above are enforced by the sandbox, not by accident."""
    workdir, outside = scenario
    res = await run_local_command(
        "./malicious", [str(outside), str(os.getpid())], {},
        workdir=workdir,  # provider=None → unconfined passthrough
    )
    assert res.exit_code == 0, res.stderr
    assert (workdir / "probe_env.txt").read_text().strip() == f"LEAK:{_ENV_CANARY}"
    assert _HOSTFILE_CANARY in (workdir / "probe_hostfile.txt").read_text()
