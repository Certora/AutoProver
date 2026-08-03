"""Tests for resolving a Solana program's on-chain address (``composer.spec.solana.build``).

An IDL must name the program it describes — a harness generated from it derives the address it sends
instructions to. `anchor idl build` under Anchor 0.29 writes none (only the deploy flow filled it in),
so the host fills it from the project: ``Anchor.toml`` first, then a lone ``declare_id!``.
"""

import json

import pytest

import composer.spec.solana.build as buildmod
from composer.spec.cargo import ProgramCrate

ADDR = "LendvUkXRmuDKxGCCFJra9uxWMdMooPEmJk3qp7Tg1Z"
STAGING = "StgXLspartwQUuGWdydvyicgYwZxH8gX43iy3xtBWJs"
CRATE = ProgramCrate(dir="programs/lend", package="example_lending", lib="example_lending")


def _crate_src(root, *sources: str) -> None:
    src = root / "programs" / "lend" / "src"
    src.mkdir(parents=True, exist_ok=True)
    for i, body in enumerate(sources):
        (src / f"m{i}.rs").write_text(body)


@pytest.mark.parametrize(
    "table",
    [
        # Anchor's key is the *program* name, which projects spell as the crate directory (this one's
        # own Anchor.toml), the package, or the lib.
        f'[programs.localnet]\nlend = "{ADDR}"\n',
        f'[programs.mainnet]\nexample_lending = "{ADDR}"\n',
        f'[programs.localnet]\nexample-lending = "{ADDR}"\n',
        # The value may be a table rather than a bare address.
        f'[programs.localnet.lend]\naddress = "{ADDR}"\nidl = "x.json"\n',
        # A local-validator entry is preferred over a deployed one when both exist.
        f'[programs.mainnet]\nlend = "{STAGING}"\n\n[programs.localnet]\nlend = "{ADDR}"\n',
    ],
)
def test_program_id_comes_from_anchor_toml(tmp_path, table):
    (tmp_path / "Anchor.toml").write_text(table)
    assert buildmod.resolve_program_id(tmp_path, CRATE) == ADDR


def test_a_lone_declare_id_is_the_fallback_and_ambiguity_is_not_guessed(tmp_path):
    _crate_src(tmp_path, f'declare_id!("{ADDR}");\n')
    assert buildmod.resolve_program_id(tmp_path, CRATE) == ADDR
    # A real program's shape: a feature-gated staging id beside the real one. Picking one would point the
    # harness at a program that isn't loaded, so the caller is told instead.
    _crate_src(tmp_path, f'declare_id!("{ADDR}");\n', f'declare_id!("{STAGING}");\n')
    assert buildmod.resolve_program_id(tmp_path, CRATE) is None


def test_idl_with_program_id_fills_only_what_is_missing(tmp_path):
    (tmp_path / "Anchor.toml").write_text(f'[programs.localnet]\nlend = "{ADDR}"\n')
    fill = lambda text: buildmod.idl_with_program_id(text, project_root=tmp_path, crate=CRATE)

    # The pre-0.30 layout keeps the address under `metadata`; docs/other metadata are preserved.
    got = json.loads(fill('{"name": "example_lending", "metadata": {"origin": "shank"}}'))
    assert got["metadata"] == {"origin": "shank", "address": ADDR}
    assert got["name"] == "example_lending"

    # An IDL that already names its program is returned untouched, in either spec.
    for already in [f'{{"address": "{STAGING}"}}', f'{{"metadata": {{"address": "{STAGING}"}}}}']:
        assert fill(already) == already


def test_idl_with_program_id_refuses_to_guess(tmp_path):
    # No Anchor.toml and no source: raise rather than emit an IDL that would fuzz a wrong address.
    with pytest.raises(ValueError, match="address"):
        buildmod.idl_with_program_id("{}", project_root=tmp_path, crate=CRATE)
    with pytest.raises(ValueError, match="not valid JSON"):
        buildmod.idl_with_program_id("{oops", project_root=tmp_path, crate=CRATE)


def test_an_unresolved_crate_is_not_a_crate_named_dot(tmp_path):
    # The resolver yields None when the layout has no crate; the program-id lookup used to receive a
    # dict and paper that over with `crate.get("dir", ".")`, i.e. scan the root as if it were the
    # crate and match against a set of empty names. `None` says it plainly, and the fallback (scan
    # the root's own sources) is stated once, here.
    (tmp_path / "Anchor.toml").write_text(f'[programs.localnet]\nlend = "{ADDR}"\n')
    # No names to match, so the Anchor.toml entry is not claimed…
    assert buildmod.resolve_program_id(tmp_path, None) is None
    # …but a lone declare_id! at the root is still found.
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(f'declare_id!("{STAGING}");\n')
    assert buildmod.resolve_program_id(tmp_path, None) == STAGING
