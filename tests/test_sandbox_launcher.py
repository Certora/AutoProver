"""Tests for the ``run-confined`` launcher provider (Phase 6 step 2).

The ``wrap`` tests are pure argv construction (no binary, no subprocess) and pin
the exact flag mapping. The ``available`` / ``--probe`` tests exercise the real
binary when it has been built (``cargo build -p run-confined --release``) and skip
otherwise, so the suite stays green on a machine without the Rust build.
"""

from pathlib import Path

import pytest

from composer.sandbox.launcher import LauncherProvider, _resolve_binary
from composer.sandbox.config import SandboxConfig
from composer.sandbox.policy import LaunchSpec, Reason, SandboxPolicy

_FAKE_BIN = "/opt/run-confined"


def _provider() -> LauncherProvider:
    return LauncherProvider(binary=_FAKE_BIN)


def test_wrap_minimal_policy():
    """A workdir-only policy maps to the workdir grant + the command after `--`."""
    policy = SandboxPolicy(rw_paths=(Path("/work"),))
    spec = _provider().wrap(policy, "cargo", ["build", "--offline"])
    assert spec == LaunchSpec(
        argv=(_FAKE_BIN, "--rw", "/work", "--", "cargo", "build", "--offline"),
        env=None,
    )


def test_wrap_full_policy_flag_order():
    """ro before rw, then env, network, then rlimits, then `-- program args`."""
    policy = SandboxPolicy(
        rw_paths=(Path("/work"), Path("/dev")),
        ro_paths=(Path("/usr"), Path("/lib")),
        env_allowlist={"PATH": "/usr/bin", "HOME": "/work"},
        network=False,
        mem_bytes=4 << 30,
        cpu_seconds=900,
        nproc=512,
        fsize_bytes=1 << 30,
    )
    spec = _provider().wrap(policy, "crucible", ["run", "vault", "c_deposit"])
    assert spec.argv == (
        _FAKE_BIN,
        "--ro", "/usr",
        "--ro", "/lib",
        "--rw", "/work",
        "--rw", "/dev",
        "--allow-env", "PATH=/usr/bin",
        "--allow-env", "HOME=/work",
        "--rlimit-as", str(4 << 30),
        "--rlimit-cpu", "900",
        "--rlimit-nproc", "512",
        "--rlimit-fsize", str(1 << 30),
        "--", "crucible", "run", "vault", "c_deposit",
    )
    assert spec.env is None


def test_argv_prefix_ends_with_separator_and_wrap_appends_command():
    """``argv_prefix`` is the confinement wrapper up to ``--``; ``wrap`` is exactly
    that prefix followed by ``program args`` (the contract a Rust backend relies on)."""
    policy = SandboxPolicy(rw_paths=(Path("/work"),), ro_paths=(Path("/usr"),))
    prov = _provider()
    prefix = prov.argv_prefix(policy)
    assert prefix == [_FAKE_BIN, "--ro", "/usr", "--rw", "/work", "--"]
    spec = prov.wrap(policy, "cargo", ["build"])
    assert list(spec.argv) == [*prefix, "cargo", "build"]


def test_wrap_network_flag():
    policy = SandboxPolicy(rw_paths=(Path("/work"),), network=True)
    spec = _provider().wrap(policy, "echo", [])
    assert "--allow-network" in spec.argv
    # no rlimit flags when caps are unset
    assert not any(a.startswith("--rlimit") for a in spec.argv)


def test_wrap_uses_binary_name_when_unresolved():
    """With no binary resolved, argv[0] falls back to the bare name (kept runnable
    if it is later placed on PATH); wrap never crashes on a missing binary."""
    prov = LauncherProvider(binary=None)
    prov._binary = None  # force the unresolved case regardless of the dev tree
    spec = prov.wrap(SandboxPolicy(rw_paths=(Path("/w"),)), "true", [])
    assert spec.argv[0] == "run-confined"


@pytest.mark.asyncio
async def test_available_reports_missing_binary():
    prov = LauncherProvider(binary=None)
    prov._binary = None
    avail = await prov.available()
    assert isinstance(avail, Reason)
    assert "run-confined" in avail.reason


def test_launcher_resolves_via_entry_point():
    """The launcher is discovered through its ``composer.sandbox_providers`` entry
    point — resolving it needs no explicit import of this module."""
    prov = SandboxConfig(provider="launcher").resolve_provider()
    assert isinstance(prov, LauncherProvider)
    assert prov.name == "launcher"


# --- tests that need the actual built binary (skip if unbuilt) ---

_REAL_BIN = _resolve_binary()
_needs_bin = pytest.mark.skipif(_REAL_BIN is None, reason="run-confined not built")


@_needs_bin
@pytest.mark.asyncio
async def test_probe_reports_available_on_this_host():
    """On a Landlock-capable host the real binary's --probe → available()."""
    avail = await LauncherProvider().available()
    assert avail == "ok", avail
