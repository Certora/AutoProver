"""Tests for the Solana build step's offline warming (Phase 6 step 4).

Pin the §5 split: the registry is warmed with network *outside* the sandbox
(`warm_cargo_cache` → `cargo fetch`, no provider) only when a sandbox is enabled,
and the actual build is then handed the provider (so it runs confined + offline).
Uses fakes — no real cargo/toolchain.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import composer.spec.solana.build as buildmod
from composer.sandbox.command import CommandResult
from composer.sandbox.config import SandboxConfig

pytestmark = pytest.mark.asyncio


async def test_warm_cargo_cache_runs_unsandboxed_fetch(tmp_path, monkeypatch):
    seen = {}

    async def fake_run(program, args, files, *, workdir, timeout_s=600, sem=None,
                       provider=None, policy=None, env_overlay=None):
        seen.update(program=program, args=args, workdir=Path(workdir),
                    provider=provider, env_overlay=env_overlay)
        return CommandResult(0, "", "")

    monkeypatch.setattr(buildmod, "run_local_command", fake_run)
    res = await buildmod.warm_cargo_cache(tmp_path, cargo_home=tmp_path / ".sandbox_cargo")
    assert res.exit_code == 0
    assert seen["program"] == "cargo" and seen["args"] == ["fetch"]
    assert seen["workdir"] == tmp_path
    assert seen["provider"] is None  # warming must NOT be sandboxed (it needs network)
    # fetch targets the private per-run CARGO_HOME (same one the offline build reads)
    assert seen["env_overlay"] == {"CARGO_HOME": str(tmp_path / ".sandbox_cargo")}


async def _fake_build_run_factory(calls):
    async def fake_run(program, args, files, *, workdir, timeout_s=600, sem=None,
                       provider=None, policy=None, env_overlay=None):
        calls.append((program, provider is not None))
        so = Path(workdir) / "target" / "deploy" / "vault.so"
        so.parent.mkdir(parents=True, exist_ok=True)
        so.write_text("")  # simulate a produced .so so build_program succeeds
        return CommandResult(0, "", "")

    return fake_run


async def test_build_program_skips_warm_when_unsandboxed(tmp_path, monkeypatch):
    calls: list = []
    warm_count = {"n": 0}

    async def fake_warm(*a, **k):
        warm_count["n"] += 1
        return CommandResult(0, "", "")

    monkeypatch.setattr(buildmod, "warm_cargo_cache", fake_warm)
    monkeypatch.setattr(buildmod, "run_local_command", await _fake_build_run_factory(calls))

    await buildmod.build_program(tmp_path, "vault")  # no sandbox → no warm, no provider
    assert warm_count["n"] == 0
    assert calls and calls[0][1] is False


async def test_build_program_warms_and_sandboxes_when_enabled(tmp_path, monkeypatch):
    calls: list = []
    warm_count = {"n": 0}

    async def fake_warm(*a, **k):
        warm_count["n"] += 1
        return CommandResult(0, "", "")

    monkeypatch.setattr(buildmod, "warm_cargo_cache", fake_warm)
    monkeypatch.setattr(buildmod, "run_local_command", await _fake_build_run_factory(calls))

    await buildmod.build_program(tmp_path, "vault", sandbox=SandboxConfig(provider="launcher"))
    assert warm_count["n"] == 1  # warmed once, outside the sandbox
    assert calls[0][1] is True  # the build command received the provider
