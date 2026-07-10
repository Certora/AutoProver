"""Phase-6 gate — Part B: the legitimate path works UNDER the launcher sandbox.

Part A (the escape suite proving confinement holds — every exfil vector denied) is
[tests/test_sandbox_escape.py], runnable without the Solana toolchain. This file is
Part B: the *real* toolchain runs confined + offline.

`test_solana_vault_builds_under_launcher` builds the real `solana_vault` program with
`cargo-build-sbf` inside the `run-confined` launcher (network off, `CARGO_NET_OFFLINE`)
and asserts the `.so` is produced — proving the policy grants exactly the toolchain a
real sBPF build needs, and that the offline build resolves against the warm cache. No
LLM.

The FULL vertical (shared fixture + per-instruction harness build + fuzz, all confined)
is the existing e2e gate run with the launcher enabled — `run_crucible_pipeline` already
honors `$COMPOSER_SANDBOX_PROVIDER` via `_crucible_sandbox`, so no separate test is
needed:

    CRUCIBLE_REPO=/path/to/crucible COMPOSER_SANDBOX_PROVIDER=launcher \
      .venv/bin/python -m pytest tests/test_crucible_e2e_gate.py -m expensive -q -s

Prereqs: `cargo-build-sbf` + `crucible` on PATH, a built `run-confined`, `CRUCIBLE_REPO`.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from composer.sandbox.config import SandboxConfig
from composer.sandbox.launcher import LauncherProvider
from composer.spec.solana.build import build_program

pytestmark = [pytest.mark.expensive, pytest.mark.asyncio]

_SCENARIO = Path(__file__).parent.parent / "test_scenarios" / "solana_vault"


def _require(cond: bool, why: str) -> None:
    if not cond:
        pytest.skip(why)


async def test_solana_vault_builds_under_launcher():
    _require(_SCENARIO.is_dir(), f"scenario missing: {_SCENARIO}")
    _require(shutil.which("cargo-build-sbf") is not None, "cargo-build-sbf not on PATH")
    _require(LauncherProvider().available().ok, "run-confined unbuilt or kernel lacks Landlock")

    # Warm the dep cache with an ordinary (unsandboxed) build first, so the confined
    # offline build has everything it needs — mirrors the §5 fetch-outside / build-inside
    # split on a fresh machine.
    await build_program(_SCENARIO, "vault", timeout_s=480)

    cargo_home = Path(os.environ.get("CARGO_HOME", Path.home() / ".cargo"))
    extra_ro: list[Path] = [Path.home() / ".cargo" / "bin"]
    if (crucible := os.environ.get("CRUCIBLE_REPO")):
        extra_ro.append(Path(crucible))
    cfg = SandboxConfig(
        provider="launcher",
        extra_ro=tuple(extra_ro),
        extra_rw=(cargo_home,),
    )
    built = await build_program(_SCENARIO, "vault", sandbox=cfg, timeout_s=480)
    assert built.so_path.is_file(), "cargo-build-sbf under the launcher did not produce the .so"
