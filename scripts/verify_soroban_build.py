"""Step 4 of scripts/verify-soroban-image.sh: a confined Soroban scaffold, gate and wasm build, with
no LLM and no Prover. Run inside the autoprover-soroban container.

Writes a one-function contract into the directory it is given, scaffolds it as a run's preflight
does, checks it as the gate does, and builds it for ``wasm32v1-none`` as a submission does. Every
cargo command but the dependency fetch runs under the image's sandbox, and a step that ran
unconfined is a failure: that is the part of the image a host run cannot check. With
``COMPOSER_SANDBOX_PROVIDER=none`` (``verify-soroban-image.sh --unconfined``) every step runs
unconfined, and only the toolchain is checked.

    python verify_soroban_build.py <empty directory>
"""

import asyncio
import sys
from pathlib import Path

from composer.cargo.wasm import WasmBuild, WasmIdentity, wasm_artifact, wasm_build
from composer.spec.cvlr.conf import DEFAULT_FEATURE
from composer.spec.cvlr.entry import build_confinement
from composer.spec.cvlr.preflight import (
    chain_session,
    gate_workspace,
    prepare_workspace,
    select_package,
)
from composer.spec.cvlr.reference import SOROBAN, generations

#: The newest line's SDK, at the lowest patch its cvlr-soroban branch accepts.
_MANIFEST = f"""[package]
name = "probe"
version = "0.1.0"
edition = "2021"

[lib]
crate-type = ["cdylib"]

[dependencies]
soroban-sdk = "{SOROBAN.platform.minimum}"
"""

_LIB = """#![no_std]
use soroban_sdk::{contract, contractimpl, Env};

#[contract]
pub struct Probe;

#[contractimpl]
impl Probe {
    pub fn add(_env: Env, a: u32, b: u32) -> u32 {
        a.wrapping_add(b)
    }
}
"""


def _fail(message: str) -> None:
    raise SystemExit(f"FAIL: {message}")


async def main(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text(_MANIFEST)
    (root / "src" / "lib.rs").write_text(_LIB)

    sandbox = build_confinement("soroban")
    confined = sandbox.enabled
    how = "confined" if confined else "UNCONFINED"

    selected = await select_package(
        root, None, candidates=generations("soroban"), main_source=root / "src" / "lib.rs"
    )
    print(f"package {selected.name}, line {selected.reference.line()}")
    pre = await prepare_workspace(
        root, reference=selected.reference, sandbox=sandbox, package=selected.name, chain="soroban"
    )
    print(pre.describe())
    await gate_workspace(pre, sandbox=sandbox, chain="soroban")
    print(f"gate: cargo check passed, {how}")

    session = await chain_session(root, sandbox=sandbox, chain="soroban")
    identity = WasmIdentity(root, pre.package_dir, pre.target_directory, pre.artifact_stem)
    run = await wasm_build(
        session,
        WasmBuild(manifest_path=root / "Cargo.toml", identity=identity, features=(DEFAULT_FEATURE,)),
    )
    if not run.ok:
        _fail(f"the wasm build failed:\n{getattr(run.verdict, 'diagnostics', run.verdict)}")
    if confined and not run.confined:
        _fail("the wasm build ran unconfined")
    artifact = wasm_artifact(pre.target_directory, pre.artifact_stem)
    if not artifact.is_file():
        _fail(f"the wasm build exited 0 and left no {artifact}")
    print(f"wasm build: {artifact} ({artifact.stat().st_size} bytes) in {run.duration_ms} ms, {how}")


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]).resolve()))
