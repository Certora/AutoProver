"""Unit tests for the sandbox config + the Rust-build policy recipe (step 3).

Pure: no subprocess, no Rust binary. They pin provider selection (default ``none``,
``$COMPOSER_SANDBOX_PROVIDER`` override) and that the recipe grants the workdir
read-write, discoverable toolchain dirs read-only, and a scrubbed env with the
network off.
"""

from pathlib import Path

import pytest

from composer.sandbox.config import SandboxConfig
from composer.sandbox.launcher import LauncherProvider
from composer.sandbox.policy import NoneProvider
from composer.sandbox.recipes import rust_build_policy, shared_cargo_ro_paths


def test_config_default_is_none_and_disabled():
    cfg = SandboxConfig()
    assert cfg.provider == "none"
    assert cfg.enabled is False
    assert isinstance(cfg.resolve_provider(), NoneProvider)


def test_config_from_env_default(monkeypatch):
    monkeypatch.delenv("COMPOSER_SANDBOX_PROVIDER", raising=False)
    assert SandboxConfig.from_env().provider == "none"


def test_config_from_env_launcher(monkeypatch):
    monkeypatch.setenv("COMPOSER_SANDBOX_PROVIDER", "launcher")
    cfg = SandboxConfig.from_env(extra_ro=(Path("/usr"),))
    assert cfg.provider == "launcher"
    assert cfg.enabled is True
    assert isinstance(cfg.resolve_provider(), LauncherProvider)
    assert cfg.extra_ro == (Path("/usr"),)


def test_resolve_provider_unknown_is_value_error():
    cfg = SandboxConfig(provider="bogus")
    with pytest.raises(ValueError, match="unknown sandbox provider 'bogus'"):
        cfg.resolve_provider()


def test_config_none_build_policy_is_none():
    """A passthrough config has no confinement to describe — build_policy returns
    None rather than a misleading empty policy."""
    assert SandboxConfig().build_policy("/work") is None


def test_config_enabled_build_policy_grants_workdir(tmp_path):
    cfg = SandboxConfig(provider="launcher", mem_bytes=1 << 30)
    pol = cfg.build_policy(tmp_path)
    assert tmp_path in pol.rw_paths
    assert pol.network is False
    assert pol.mem_bytes == (1 << 30)


@pytest.mark.asyncio
async def test_backend_spec_none_is_passthrough():
    """A passthrough config yields an empty ``argv_prefix`` — the backend runs the
    command directly, with no confinement wrapper to prepend."""
    assert await SandboxConfig().backend_spec("/work", timeout_s=42) == {
        "argv_prefix": [],
        "timeout_s": 42,
    }


@pytest.mark.asyncio
async def test_backend_spec_enabled_ships_provider_argv_prefix(tmp_path, monkeypatch):
    """An enabled config ships the resolved provider's ``argv_prefix`` verbatim, so a
    Rust backend launches ``[*argv_prefix, program, *args]`` with no mechanism knowledge.
    The provider/availability check is stubbed to stay a pure unit test (no real binary)."""
    import composer.sandbox.config as config_mod

    async def _ok(prov):
        return None

    provider = LauncherProvider(binary="/opt/run-confined")
    monkeypatch.setattr(SandboxConfig, "resolve_provider", lambda self: provider)
    monkeypatch.setattr(config_mod, "ensure_available", _ok)

    cfg = SandboxConfig(provider="launcher", mem_bytes=1 << 30)
    spec = await cfg.backend_spec(tmp_path, timeout_s=900)

    policy = cfg.build_policy(tmp_path)
    assert policy is not None
    assert spec["timeout_s"] == 900
    assert spec["argv_prefix"] == provider.argv_prefix(policy)
    assert spec["argv_prefix"][0] == "/opt/run-confined"
    assert spec["argv_prefix"][-1] == "--"


def test_rust_build_policy_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("MY_SECRET", "do-not-pass")
    extra_ro_dir = tmp_path / "toolchain"
    extra_ro_dir.mkdir()
    extra_rw_dir = tmp_path / "scratch"
    extra_rw_dir.mkdir()

    pol = rust_build_policy(
        tmp_path,
        extra_ro=(extra_ro_dir, tmp_path / "does-not-exist"),
        extra_rw=(extra_rw_dir,),
        cpu_seconds=900,
    )

    # workdir + existing extra_rw are writable
    assert tmp_path in pol.rw_paths
    assert extra_rw_dir in pol.rw_paths
    # existing extra_ro granted; non-existent dropped
    assert extra_ro_dir in pol.ro_paths
    assert (tmp_path / "does-not-exist") not in pol.ro_paths
    # env: only allowlisted names pass through; secrets do not
    assert pol.env_allowlist.get("PATH") == "/usr/bin:/bin"
    assert "MY_SECRET" not in pol.env_allowlist
    # network off, caps threaded
    assert pol.network is False
    assert pol.cpu_seconds == 900


def test_rust_build_policy_offline_sets_cargo_net_offline(tmp_path):
    """Default (offline) forces every cargo — incl. the one `crucible run` spawns —
    offline via CARGO_NET_OFFLINE; opting out drops it."""
    on = rust_build_policy(tmp_path)
    assert on.env_allowlist.get("CARGO_NET_OFFLINE") == "1"
    off = rust_build_policy(tmp_path, offline=False)
    assert "CARGO_NET_OFFLINE" not in off.env_allowlist


def test_config_enabled_policy_is_offline_by_default(tmp_path):
    pol = SandboxConfig(provider="launcher").build_policy(tmp_path)
    assert pol.env_allowlist.get("CARGO_NET_OFFLINE") == "1"
    pol_net = SandboxConfig(provider="launcher", offline=False).build_policy(tmp_path)
    assert "CARGO_NET_OFFLINE" not in pol_net.env_allowlist


def test_rust_build_policy_includes_system_and_dev_when_present():
    pol = rust_build_policy("/tmp")
    if Path("/usr").exists():
        assert Path("/usr") in pol.ro_paths
    if Path("/dev/null").exists():
        assert Path("/dev/null") in pol.rw_paths


def test_rust_build_policy_grants_cargo_bin_not_home_root(tmp_path, monkeypatch):
    """Shared CARGO_HOME root must not be RO-granted (credentials.toml lives there)."""
    cargo = tmp_path / "cargo_home"
    (cargo / "bin").mkdir(parents=True)
    (cargo / "credentials.toml").write_text('token = "secret"\n')
    monkeypatch.setenv("CARGO_HOME", str(cargo))
    # Isolate RUSTUP_HOME so a real ~/.rustup does not pollute path assertions.
    rustup = tmp_path / "rustup"
    rustup.mkdir()
    monkeypatch.setenv("RUSTUP_HOME", str(rustup))

    pol = rust_build_policy(tmp_path / "work")
    assert (cargo / "bin").resolve() in pol.ro_paths
    assert cargo.resolve() not in pol.ro_paths


def test_shared_cargo_ro_paths_excludes_credentials(tmp_path):
    """Unit-level: grant bin/ only, never the home root that holds credentials."""
    cargo = tmp_path / "cargo_home"
    (cargo / "bin").mkdir(parents=True)
    (cargo / "credentials.toml").write_text('token = "secret"\n')
    (cargo / "registry").mkdir()
    paths = shared_cargo_ro_paths(cargo)
    assert paths == (cargo / "bin",)
    assert cargo not in paths
