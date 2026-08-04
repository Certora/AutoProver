"""The escape suite — Part A of the Phase-6 gate (docs/command-sandbox.md §10).

A *malicious* program (standing in for a harness `setup()` / a program's `build.rs`)
is compiled with `rustc`, then run through the **real** `run-confined` launcher via
`run_local_command` under a Crucible-representative policy (`rust_build_policy`). It
attempts every escape and writes each result into the workdir (allowed); the test
reads them back and asserts *denied* for all. A no-sandbox control runs the same
binary unconfined and confirms the leaks would otherwise happen — proving it is the
sandbox doing the blocking.

Vectors covered:
  env scrub, /proc/<ppid>/environ, host file outside workdir, TCP (socket),
  IMDS, io_uring socket setup (seccomp bypass), AF_NETLINK/AF_VSOCK, signal to
  parent (Landlock scope), abstract Unix socket to outside listener (scope),
  cargo credentials.toml under the shared cargo home (narrow RO grant).

Runnable without the full Crucible stack (std-only program, no crates, no network
needed to compile). Skipped unless `rustc` and a working launcher are present. The
*legitimate* half (a real `solana_vault` build+fuzz under the launcher) is the
expensive Part B in `tests/test_crucible_sandbox_gate.py`.
"""

import asyncio
import os
import re
import shutil
import socket
import subprocess
import threading
from pathlib import Path

import pytest

from composer.sandbox.command import run_local_command
from composer.sandbox.launcher import LauncherProvider
from composer.sandbox.recipes import rust_build_policy


def _kernel_at_least(major: int, minor: int) -> bool:
    """Best-effort parse of ``uname -r`` (e.g. ``6.1.119-...``, ``7.0.11-...``)."""
    m = re.match(r"(\d+)\.(\d+)", os.uname().release)
    if not m:
        return False
    return (int(m.group(1)), int(m.group(2))) >= (major, minor)


# Landlock scopes (Signal + AbstractUnixSocket) need ABI v6 ≈ Linux 6.12.
_SCOPES_AVAILABLE = _kernel_at_least(6, 12)

pytestmark = pytest.mark.asyncio

_PROVIDER = LauncherProvider()
# Evaluated at collection time (no running loop), so drive the async probe with asyncio.run.
_needs = pytest.mark.skipif(
    shutil.which("rustc") is None or asyncio.run(_PROVIDER.available()) != "ok",
    reason="needs rustc + a working run-confined launcher (Linux/Landlock)",
)

_ENV_CANARY = "ENVCANARY-a1b2c3"
_HOSTFILE_CANARY = "HOSTFILECANARY-d4e5f6"
_CREDS_CANARY = "crates-io-TOKEN-CANARY-xyz"
_ABSTRACT_PAYLOAD = b"ABSTRACT-UNIX-LEAK-OK"

# Standing in for hostile code in setup()/build.rs. std-only so it compiles offline.
# Uses libc via extern for io_uring / multi-domain socket probes.
_MALICIOUS_RS = r"""
use std::fs;
use std::io::Read;
use std::net::{SocketAddr, TcpStream};
use std::os::linux::net::SocketAddrExt;
use std::os::unix::net::{SocketAddr as UnixSocketAddr, UnixStream};
use std::time::Duration;

extern "C" {
    fn socket(domain: i32, typ: i32, protocol: i32) -> i32;
    fn close(fd: i32) -> i32;
    fn syscall(n: i64, ...) -> i64;
    fn kill(pid: i32, sig: i32) -> i32;
}

const AF_UNIX: i32 = 1;
const AF_INET: i32 = 2;
const AF_NETLINK: i32 = 16;
const AF_VSOCK: i32 = 40;
const SOCK_STREAM: i32 = 1;
const SOCK_RAW: i32 = 3;
// io_uring_setup is 425 on both x86_64 and aarch64.
const SYS_IO_URING_SETUP: i64 = 425;

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

fn sock_domain(domain: i32, typ: i32) -> String {
    unsafe {
        let fd = socket(domain, typ, 0);
        if fd >= 0 {
            close(fd);
            "LEAK:socket-ok".to_string()
        } else {
            "denied".to_string()
        }
    }
}

// x32-ABI bypass (x86_64): invoke socket(2) via the x32 calling convention — same
// AUDIT_ARCH_X86_64, but the syscall number OR'd with __X32_SYSCALL_BIT (0x4000_0000).
// glibc's syscall() just loads the number into rax, so this issues a genuine x32 call.
// Records the errno so the test can distinguish "seccomp denied it" (EPERM) from "the
// kernel has no x32 support" (ENOSYS) — without the deny-mirror the call reaches the
// kernel (ENOSYS here, a live fd on an x32-enabled kernel); with it, seccomp returns EPERM.
fn sock_inet_x32() -> String {
    const X32_BIT: i64 = 0x4000_0000;
    const SYS_SOCKET_X86_64: i64 = 41;
    unsafe {
        let fd = syscall(SYS_SOCKET_X86_64 | X32_BIT, AF_INET as i64, SOCK_STREAM as i64, 0i64);
        if fd >= 0 {
            close(fd as i32);
            "LEAK:socket-ok".to_string()
        } else {
            let e = std::io::Error::last_os_error().raw_os_error().unwrap_or(0);
            format!("denied:errno={}", e)
        }
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let outside = args.get(1).cloned().unwrap_or_default();
    let parent_pid: i32 = args.get(2).and_then(|s| s.parse().ok()).unwrap_or(0);
    let creds_path = args.get(3).cloned().unwrap_or_default();

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

    // Classic seccomp bypass: create sockets via io_uring without socket(2).
    probe("iouring", &{
        let mut params = [0u8; 256];
        let fd = unsafe { syscall(SYS_IO_URING_SETUP, 8i32, params.as_mut_ptr()) };
        if fd >= 0 {
            unsafe { close(fd as i32) };
            "LEAK:io-uring-setup-ok".to_string()
        } else {
            "denied".to_string()
        }
    });

    probe("netlink", &sock_domain(AF_NETLINK, SOCK_RAW));
    probe("vsock", &sock_domain(AF_VSOCK, SOCK_STREAM));
    // AF_UNIX must still work (cargo jobserver); record that separately for the control.
    probe("unix_sock", &sock_domain(AF_UNIX, SOCK_STREAM));
    probe("inet_sock", &sock_domain(AF_INET, SOCK_STREAM));
    probe("inet_sock_x32", &sock_inet_x32());

    // Same-uid signal: kill(pid, 0) checks permission without delivering a fatal signal.
    probe("signal", &{
        let rc = unsafe { kill(parent_pid, 0) };
        if rc == 0 {
            "LEAK:signal-ok".to_string()
        } else {
            "denied".to_string()
        }
    });

    // Abstract-namespace Unix socket — not covered by Landlock FS rules (needs scope).
    // Name is arg 4 without a leading NUL; use from_abstract_name (path connect truncates at \\0).
    let abstract_name = args.get(4).cloned().unwrap_or_default();
    probe("abstract_unix", &{
        match UnixSocketAddr::from_abstract_name(abstract_name.as_bytes())
            .and_then(|addr| UnixStream::connect_addr(&addr))
        {
            Ok(mut s) => {
                let mut buf = [0u8; 64];
                match s.read(&mut buf) {
                    Ok(n) if n > 0 => format!("LEAK:{}", String::from_utf8_lossy(&buf[..n])),
                    Ok(_) => "LEAK:connected-empty".to_string(),
                    Err(_) => "LEAK:connected".to_string(),
                }
            }
            Err(_) => "denied".to_string(),
        }
    });

    probe("cargo_creds", &match fs::read_to_string(&creds_path) {
        Ok(s) if s.contains("TOKEN-CANARY") => "LEAK:creds".to_string(),
        Ok(_) => "LEAK:creds-readable".to_string(),
        Err(_) => "denied".to_string(),
    });
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

    # Shared cargo home with credentials at the root + a bin/ shim dir. Policy must
    # grant bin/ only — never the root (so credentials.toml stays unreadable).
    fake_cargo = tmp_path / "fake_cargo"
    (fake_cargo / "bin").mkdir(parents=True)
    creds = fake_cargo / "credentials.toml"
    creds.write_text(f'token = "{_CREDS_CANARY}"\n')
    monkeypatch.setenv("CARGO_HOME", str(fake_cargo))

    # Plant the secret in *this* process's env; run-confined must scrub it, and the
    # /proc/<ppid>/environ read (ppid = this pytest process) must be denied.
    monkeypatch.setenv("ANTHROPIC_API_KEY", _ENV_CANARY)
    return workdir, outside, creds


def _start_abstract_listener(name: str):
    """Serve one connection on abstract UDS ``\\0{name}`` (unique per test)."""
    ready = threading.Event()
    result = {"status": "not-started"}
    abstract = b"\0" + name.encode()

    def _run():
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.bind(abstract)
            s.listen(1)
            s.settimeout(8)
            ready.set()
            try:
                c, _ = s.accept()
                c.sendall(_ABSTRACT_PAYLOAD)
                c.close()
                result["status"] = "accepted"
            except OSError as e:
                result["status"] = f"fail:{e}"
        finally:
            s.close()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    assert ready.wait(2), "abstract listener failed to bind"
    return t, result


@_needs
async def test_all_escapes_denied(scenario):
    workdir, outside, creds = scenario

    # Policy must not grant the shared cargo root (credentials live there).
    policy = rust_build_policy(workdir)
    assert creds.parent.resolve() not in policy.ro_paths
    assert (creds.parent / "bin").resolve() in policy.ro_paths

    abs_name = f"autoprover-escape-{os.getpid()}-confined"
    _listener, _ = _start_abstract_listener(abs_name)
    res = await run_local_command(
        "./malicious",
        [str(outside), str(os.getpid()), str(creds), abs_name],
        {},
        workdir=workdir,
        provider=_PROVIDER,
        policy=policy,
    )
    assert res.exit_code == 0, res.stderr

    def probe(name: str) -> str:
        return (workdir / f"probe_{name}.txt").read_text().strip()

    # Always enforced (seccomp + Landlock FS + env scrub + narrow cargo grant).
    denied = (
        "env",
        "procenv",
        "hostfile",
        "net_ext",
        "imds",
        "iouring",
        "netlink",
        "vsock",
        "inet_sock",
        "cargo_creds",
    )
    for name in denied:
        assert probe(name) == "denied", f"{name}: {probe(name)}"
        assert "LEAK" not in probe(name), f"{name}: {probe(name)}"

    # AF_UNIX remains allowed (toolchain jobserver / local IPC on path sockets).
    assert probe("unix_sock") == "LEAK:socket-ok"

    # x32-ABI bypass (x86_64 only): the deny-mirror must make seccomp catch the
    # x32-tagged socket() *itself*. Asserting the errno is EPERM (seccomp) — not
    # ENOSYS (the kernel, reached only because the filter let the call through) —
    # is what makes this a real regression test even on an x32-disabled kernel like
    # CI's: without the mirror this reads `denied:errno=38`, with it `denied:errno=1`
    # (and on an x32-*enabled* kernel, without the mirror it would be a live socket).
    import errno as _errno

    if os.uname().machine == "x86_64":
        assert probe("inet_sock_x32") == f"denied:errno={_errno.EPERM}", probe(
            "inet_sock_x32"
        )

    # Landlock scopes (ABI ≥6 / Linux ≥6.12): signal + abstract UDS. On older
    # kernels BestEffort drops them — residual same-uid risk (command-sandbox.md §6).
    if _SCOPES_AVAILABLE:
        assert probe("signal") == "denied", probe("signal")
        assert probe("abstract_unix") == "denied", probe("abstract_unix")
    else:
        # Still record the probes so a future kernel bump is visible in artifacts.
        assert (workdir / "probe_signal.txt").is_file()
        assert (workdir / "probe_abstract_unix.txt").is_file()


@_needs
async def test_control_unconfined_would_leak(scenario):
    """Without the sandbox the same binary reads the secret env + the host file —
    confirming the assertions above are enforced by the sandbox, not by accident."""
    workdir, outside, creds = scenario
    abs_name = f"autoprover-escape-{os.getpid()}-control"
    _listener, _ = _start_abstract_listener(abs_name)
    res = await run_local_command(
        "./malicious",
        [str(outside), str(os.getpid()), str(creds), abs_name],
        {},
        workdir=workdir,  # provider=None → unconfined passthrough
    )
    assert res.exit_code == 0, res.stderr
    assert (workdir / "probe_env.txt").read_text().strip() == f"LEAK:{_ENV_CANARY}"
    assert _HOSTFILE_CANARY in (workdir / "probe_hostfile.txt").read_text()
    assert "LEAK" in (workdir / "probe_cargo_creds.txt").read_text()
    # io_uring and abstract unix should also work unconfined (control for those fixes).
    assert "LEAK" in (workdir / "probe_iouring.txt").read_text()
    assert "LEAK" in (workdir / "probe_abstract_unix.txt").read_text()
    assert "LEAK" in (workdir / "probe_signal.txt").read_text()
