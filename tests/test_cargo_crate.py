"""Tests for Cargo crate resolution — the ``rust`` language facet's source→crate lookup.

A real workspace we hit is the motivating case: a workspace member whose *directory* (``programs/lend``),
*package* (``example_lending``) and analysis identifier all differ. A backend that path-depends on
the program under test (Crucible) must get the first two from the manifest, never from the third.
"""

import pytest

from composer.pipeline.ecosystem import EVM, SOLANA
from composer.rustapp.adapter import program_crate_of
from composer.rustapp.wire import ProgramCrate as WireCrate
from composer.spec.cargo import ProgramCrate, resolve_program_crate
from composer.spec.context import SourceFields
from composer.spec.system_model import SolidityIdentifier

WORKSPACE = '[workspace]\nmembers = ["programs/*"]\n'


def _member(root, dir_name, manifest: str) -> None:
    """Write a workspace with one member crate at ``programs/<dir_name>`` holding ``src/lib.rs``."""
    (root / "Cargo.toml").write_text(WORKSPACE)
    crate = root / "programs" / dir_name
    (crate / "src").mkdir(parents=True)
    (crate / "Cargo.toml").write_text(manifest)
    (crate / "src" / "lib.rs").write_text("// program\n")


def test_resolves_directory_and_names_independently_of_each_other(tmp_path):
    # Directory, package and lib name all differ (lend: programs/lend / example_lending).
    _member(tmp_path, "lend", '[package]\nname = "example-lending"\n\n[lib]\nname = "example_lending"\n')
    assert resolve_program_crate(tmp_path, "programs/lend/src/lib.rs") == ProgramCrate(
        dir="programs/lend", package="example-lending", lib="example_lending"
    )


def test_reads_the_anchor_requirement_through_workspace_inheritance(tmp_path):
    # A real workspace's shape: the member inherits the version from the root, which is where a
    # backend deciding "can I link this crate?" has to look.
    (tmp_path / "Cargo.toml").write_text(
        WORKSPACE + '\n[workspace.dependencies]\nanchor-lang = { version = "0.29.0" }\n'
    )
    crate = tmp_path / "programs" / "lend"
    (crate / "src").mkdir(parents=True)
    (crate / "src" / "lib.rs").write_text("// program\n")
    (crate / "Cargo.toml").write_text(
        '[package]\nname = "example_lending"\n\n'
        '[dependencies]\nanchor-lang = { workspace = true, features = ["event-cpi"] }\n'
    )
    got = resolve_program_crate(tmp_path, "programs/lend/src/lib.rs")
    assert got is not None and got.anchor == "0.29.0"


@pytest.mark.parametrize(
    "dependencies, expected",
    [
        ('anchor-lang = "1.0.1"', "1.0.1"),                                  # bare string
        ('anchor-lang = { version = "^0.31", features = ["x"] }', "^0.31"),   # table
        # Nothing to compare reads as None, never as a version-shaped "" — see `_dep_req`.
        ("", None),                                                          # not an Anchor program
        ('anchor-lang = { git = "https://x/anchor" }', None),                # no version to compare
        ("anchor-lang = { workspace = true }", None),                        # nothing to inherit
    ],
)
def test_anchor_requirement_spellings(tmp_path, dependencies, expected):
    _member(tmp_path, "vault", f'[package]\nname = "vault"\n\n[dependencies]\n{dependencies}\n')
    got = resolve_program_crate(tmp_path, "programs/vault/src/lib.rs")
    assert got is not None and got.anchor == expected


def test_lib_name_defaults_to_the_package_name_with_dashes_normalized(tmp_path):
    # No [lib] section → Cargo's default lib target name, which is the `use <id>::*` path.
    _member(tmp_path, "lend", '[package]\nname = "example-lending"\n')
    crate = resolve_program_crate(tmp_path, "programs/lend/src/lib.rs")
    assert crate is not None and crate.lib == "example_lending"


def test_walks_up_past_nested_modules_and_the_virtual_workspace_root(tmp_path):
    _member(tmp_path, "vault", '[package]\nname = "vault"\n')
    nested = tmp_path / "programs" / "vault" / "src" / "state"
    nested.mkdir(parents=True)
    (nested / "mod.rs").write_text("// state\n")
    # A file several dirs below the manifest still resolves to its crate…
    assert resolve_program_crate(tmp_path, "programs/vault/src/state/mod.rs") == ProgramCrate(
        dir="programs/vault", package="vault", lib="vault"
    )
    # …and the workspace root's [workspace]-only manifest is not mistaken for a crate.
    assert resolve_program_crate(tmp_path, "Cargo.toml") is None


def test_resolves_a_crate_that_is_itself_the_project_root(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("// program\n")
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "solo"\n')
    # "." keeps the dir non-empty, so consumers don't read it as "unresolved".
    assert resolve_program_crate(tmp_path, "src/lib.rs") == ProgramCrate(
        dir=".", package="solo", lib="solo"
    )


def test_unresolvable_layouts_return_none_rather_than_raising(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("// no manifest anywhere\n")
    assert resolve_program_crate(tmp_path, "src/lib.rs") is None
    # A name inherited from the workspace isn't a name we can use as a dependency key.
    (tmp_path / "Cargo.toml").write_text("[package]\nname.workspace = true\n")
    assert resolve_program_crate(tmp_path, "src/lib.rs") is None
    # Unparseable manifest, and a path outside the root: both soft failures.
    (tmp_path / "Cargo.toml").write_text("[package\nname =\n")
    assert resolve_program_crate(tmp_path, "src/lib.rs") is None
    assert resolve_program_crate(tmp_path, "../elsewhere/src/lib.rs") is None


def _source(root, relative_path: str) -> SourceFields:
    return SourceFields(
        project_root=str(root),
        contract_name=SolidityIdentifier("vault"),
        relative_path=relative_path,
        forbidden_read="",
    )


def test_the_author_input_field_comes_from_the_ecosystems_language_facet(tmp_path):
    _member(
        tmp_path, "lend", '[package]\nname = "example_lending"\n\n[dependencies]\nanchor-lang = "0.29.0"\n'
    )
    # Rust (Solana) resolves the crate; this is what every AuthorInput carries.
    assert program_crate_of(SOLANA, _source(tmp_path, "programs/lend/src/lib.rs")) == WireCrate(
        dir="programs/lend", package="example_lending", lib="example_lending", anchor="0.29.0"
    )
    # Solidity has no compilation unit to locate, and an unresolvable Rust layout is not fatal —
    # both yield an all-empty crate, which the wheel reads through its own ``resolved()`` fallback.
    # Empty strings rather than absent keys: the Rust struct defaults every field, so the two
    # deserialize identically.
    assert program_crate_of(EVM, _source(tmp_path, "programs/lend/src/lib.rs")) == WireCrate()
    assert program_crate_of(SOLANA, _source(tmp_path, "nowhere/src/lib.rs")) == WireCrate()
