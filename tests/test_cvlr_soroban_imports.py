"""Which workspace contracts a Soroban contract imports, and in what order they are built. No cargo:
the workspace is built directly, the way Phoenix lays one out.
"""

from pathlib import Path

from composer.cargo.metadata import CratePackage, LibTarget, Workspace
from composer.spec.cvlr.soroban_imports import ContractImport, contract_imports

_TARGET = "../../target/wasm32-unknown-unknown/release"


def _member(root: Path, name: str, stem: str, sources: dict[str, str]) -> CratePackage:
    crate = root / "contracts" / name
    for rel, text in {"lib.rs": "", **sources}.items():
        path = crate / "src" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (crate / "Cargo.toml").write_text(f'[package]\nname = "{name}"\n')
    return CratePackage(
        name=name, version="0.1.0", manifest_path=crate / "Cargo.toml",
        lib=LibTarget(name=stem, src_path=crate / "src" / "lib.rs", crate_types=("cdylib",)),
        features=(), source=None,
    )


def _import(stem: str) -> str:
    return f'soroban_sdk::contractimport!(\n    file = "{_TARGET}/{stem}.wasm"\n);\n'


def _workspace(root: Path, *members: CratePackage) -> Workspace:
    return Workspace(
        root=root, target_directory=root / "target", members=members, packages=members
    )


def test_imports_are_built_each_after_what_it_imports(tmp_path):
    token = _member(tmp_path, "token", "soroban_token_contract", {})
    stake = _member(tmp_path, "stake", "phoenix_stake", {"lib.rs": _import("soroban_token_contract")})
    pool = _member(
        tmp_path, "pool", "phoenix_pool",
        {"lib.rs": _import("phoenix_stake") + _import("soroban_token_contract")},
    )
    imports = contract_imports(_workspace(tmp_path, token, stake, pool), pool)
    assert [i.artifact_stem for i in imports] == ["soroban_token_contract", "phoenix_stake"]
    assert imports[0] == ContractImport(
        member_dir=Path("contracts/token"),
        artifact_stem="soroban_token_contract",
        expected=Path("target/wasm32-unknown-unknown/release/soroban_token_contract.wasm"),
    )


def test_an_import_in_a_test_source_is_not_built(tmp_path):
    token = _member(tmp_path, "token", "soroban_token_contract", {})
    pool = _member(
        tmp_path, "pool", "phoenix_pool",
        {"tests/setup.rs": _import("soroban_token_contract"), "test.rs": _import("soroban_token_contract")},
    )
    assert contract_imports(_workspace(tmp_path, token, pool), pool) == ()


def test_a_wasm_no_member_builds_is_left_alone(tmp_path):
    pool = _member(
        tmp_path, "pool", "phoenix_pool",
        {"lib.rs": 'soroban_sdk::contractimport!(file = "../../.wasm_binaries_mainnet/live_pool.wasm");'},
    )
    assert contract_imports(_workspace(tmp_path, pool), pool) == ()


def test_a_cycle_does_not_recurse_forever(tmp_path):
    a = _member(tmp_path, "a", "a_contract", {"lib.rs": _import("b_contract")})
    b = _member(tmp_path, "b", "b_contract", {"lib.rs": _import("a_contract")})
    imports = contract_imports(_workspace(tmp_path, a, b), a)
    assert [i.artifact_stem for i in imports] == ["b_contract"]
