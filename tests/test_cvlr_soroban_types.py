"""Arbitrary values of a Soroban contract's own types: what is parsed, what is implemented, what is
skipped, and how the scaffold writes it. No cargo: the real-build check lives in the probe runs the
commit records.
"""

import tomllib
from pathlib import Path

import pytest

from composer.cargo.metadata import CratePackage, LibTarget, Workspace
from composer.spec.cvlr.reference import SOROBAN_V22, SOROBAN_V25
from composer.spec.cvlr.scaffold import SOROBAN_SCAFFOLD, AppendSection, apply, plan_scaffold
from composer.spec.cvlr.soroban_types import (
    NONDET_MODULE,
    RustType,
    has_nondet_module,
    implementable,
    parse_contract_types,
    parse_type,
    render_nondet_module,
)

TYPES = """
use soroban_sdk::{contracttype, contracterror, Address, BytesN, Map, Vec};

/// A config.
#[contracttype]
#[derive(Clone, Debug)]
pub struct Config {
    pub admin: Address,
    pub(crate) fee_bps: u32,
    cap: Option<i128>,
    owners: Vec<Address>,
    limits: Map<Address, i128>,
    salt: BytesN<32>,
    pair: (Address, u64),
    kind: Kind,
}

#[contracttype]
#[derive(Clone)]
pub enum Kind { Plain, Capped(i128), Split(Address, u32) }

#[contracterror]
#[derive(Copy, Clone)]
#[repr(u32)]
pub enum Error { NotInitialized = 1, AlreadyInitialized = 2 }

#[contracttype]
pub struct Wrapper(pub Address, u64);

#[contracttype]
pub struct Marker;

#[contracttype]
pub struct Node { pub children: Vec<Node> }

#[contracttype]
pub struct HoldsNode { pub node: Node }

#[contracttype]
pub struct Generic<T> { pub v: T }

#[contracttype]
pub struct Foreign { pub x: other::Thing }

#[contracttype]
pub struct Muxed { pub to: MuxedAddress }

#[cfg(test)]
mod test {
    #[contracttype]
    pub struct TestOnly { pub a: u32 }
}
"""


def _names(types) -> list[str]:
    return [t.name for t in types]


def test_every_contract_type_outside_tests_is_found():
    assert _names(parse_contract_types(TYPES)) == [
        "Config", "Kind", "Error", "Wrapper", "Marker", "Node", "HoldsNode", "Generic",
        "Foreign", "Muxed",
    ]


def test_types_parse_to_their_last_path_segment_with_parameters():
    assert parse_type("soroban_sdk::Map<Address, Vec<u64>>") == RustType(
        "Map", (RustType("Address"), RustType("Vec", (RustType("u64"),)))
    )
    assert parse_type("(Address, u64)") == RustType("()", (RustType("Address"), RustType("u64")))


def test_only_types_every_field_of_which_can_be_built_are_implemented():
    built = _names(implementable(parse_contract_types(TYPES), muxed=False))
    assert built == ["Config", "Kind", "Error", "Wrapper", "Marker"]


@pytest.mark.parametrize(
    ("name", "why"),
    [
        ("Node", "reaches itself, so its nondet() would not terminate"),
        ("HoldsNode", "needs a type that is skipped"),
        ("Generic", "has type parameters"),
        ("Foreign", "names a type nothing here builds"),
        ("Muxed", "names MuxedAddress on an SDK without it"),
    ],
)
def test_a_type_is_skipped_when(name, why):
    assert name not in _names(implementable(parse_contract_types(TYPES), muxed=False)), why


def test_a_muxed_address_is_built_where_the_sdk_has_one():
    assert "Muxed" in _names(implementable(parse_contract_types(TYPES), muxed=True))


def _rendered(muxed: bool = False) -> str:
    types = implementable(parse_contract_types(TYPES), muxed=muxed)
    return render_nondet_module(
        types, frozenset(_names(types)), muxed=muxed, feature="certora"
    )


def test_the_module_is_gated_and_reads_the_files_own_names():
    text = _rendered()
    assert '#[cfg(feature = "certora")]' in text
    assert f"mod {NONDET_MODULE}" in text and has_nondet_module(text)
    assert "use super::*;" in text
    assert "use crate::certora::soroban_nondet::*;" in text


def test_fields_are_built_from_the_helpers_and_an_option_can_be_none():
    text = " ".join(_rendered().split())
    assert "admin: nondet_address()" in text
    assert "fee_bps: cvlr::nondet::nondet::<u32>()" in text
    assert "cap: if nondet_bool() { Some(cvlr::nondet::nondet::<i128>()) } else { None }" in text
    assert "owners: nondet_vec_internal(|| nondet_address())" in text
    assert "salt: nondet_bytesn_32()" in text
    assert "kind: Kind::nondet()" in text
    assert "Wrapper(nondet_address(), cvlr::nondet::nondet::<u64>())" in text
    assert "fn nondet() -> Self { Marker }" in text


def test_every_enum_variant_is_reachable():
    text = " ".join(_rendered().split())
    for variant in ("Kind::Plain", "Kind::Capped(", "Kind::Split(", "Error::NotInitialized",
                    "Error::AlreadyInitialized"):
        assert variant in text, variant


# ---------------------------------------------------------------------------------------------
# The scaffold


MANIFEST = """\
[package]
name = "token"
version = "0.1.0"
edition = "2021"

[lib]
crate-type = ["cdylib"]

[dependencies]
soroban-sdk = "{sdk}"
"""

LIB = """\
#![no_std]
use soroban_sdk::{contract, contractimpl, contracttype, Address};

mod types;

#[contracttype]
pub enum DataKey { Admin, Balance(Address) }

#[contract]
pub struct Token;
"""


def _project(root: Path, sdk: str = "22.0.8") -> tuple[Workspace, CratePackage]:
    (root / "src").mkdir(parents=True, exist_ok=True)
    for path, text in (
        (root / "Cargo.toml", MANIFEST.format(sdk=sdk)),
        (root / "src" / "lib.rs", LIB),
        (root / "src" / "types.rs", TYPES),
    ):
        if not path.exists():
            path.write_text(text)
    package = CratePackage(
        name="token", version="0.1.0", manifest_path=root / "Cargo.toml",
        lib=LibTarget(name="token", src_path=root / "src" / "lib.rs", crate_types=("cdylib",)),
        features=(), source=None,
    )
    sdk_package = CratePackage(
        name="soroban-sdk", version=sdk, manifest_path=root / "vendor" / "Cargo.toml", lib=None,
        features=(), source="registry+https://github.com/rust-lang/crates.io-index",
    )
    workspace = Workspace(
        root=root, target_directory=root / "target", members=(package,),
        packages=(package, sdk_package), links={package: (sdk_package,)},
    )
    return workspace, package


def _appends(plan) -> dict[str, str]:
    """Each file's appended text, every append to it joined in plan order."""
    out: dict[str, str] = {}
    for c in plan.changes:
        if isinstance(c, AppendSection):
            out[c.path.as_posix()] = out.get(c.path.as_posix(), "") + c.contents
    return out


def test_the_scaffold_appends_each_files_implementations_to_that_file(tmp_path):
    workspace, package = _project(tmp_path)
    appends = _appends(plan_scaffold(workspace, package, SOROBAN_V22, SOROBAN_SCAFFOLD))
    assert "impl Nondet for Config" in appends["src/types.rs"]
    assert "impl Nondet for Config" not in appends["src/lib.rs"]
    # lib.rs gets both the harness declaration and its own type's implementation.
    assert "mod certora;" in appends["src/lib.rs"]
    assert "impl Nondet for DataKey" in appends["src/lib.rs"]


def test_the_harness_is_not_scanned_for_contract_types(tmp_path):
    workspace, package = _project(tmp_path)
    apply(plan_scaffold(workspace, package, SOROBAN_V22, SOROBAN_SCAFFOLD), tmp_path)
    again = plan_scaffold(*_project(tmp_path), SOROBAN_V22, SOROBAN_SCAFFOLD)
    assert again.changes == ()
    assert any("already has its contract types" in note for note in again.satisfied)


def test_a_name_defined_in_two_files_is_left_out(tmp_path):
    workspace, package = _project(tmp_path)
    (tmp_path / "src" / "other.rs").write_text(
        "#[contracttype]\npub struct Wrapper(pub u64);\n"
    )
    appends = _appends(plan_scaffold(workspace, package, SOROBAN_V22, SOROBAN_SCAFFOLD))
    assert "impl Nondet for Wrapper" not in "".join(appends.values())
    assert "src/other.rs" not in appends


def test_the_sdk_25_line_builds_muxed_addresses(tmp_path):
    workspace, package = _project(tmp_path, sdk="25.1.1")
    appends = _appends(plan_scaffold(workspace, package, SOROBAN_V25, SOROBAN_SCAFFOLD))
    assert "impl Nondet for Muxed" in appends["src/types.rs"]


def test_the_manifest_is_untouched_by_the_generated_implementations(tmp_path):
    workspace, package = _project(tmp_path)
    apply(plan_scaffold(workspace, package, SOROBAN_V22, SOROBAN_SCAFFOLD), tmp_path)
    parsed = tomllib.loads((tmp_path / "Cargo.toml").read_text())
    assert "certora_nondet" not in str(parsed)
