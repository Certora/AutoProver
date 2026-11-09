"""The ``certora-cvlr`` commands the Solana skill runs.

The tests marked ``skill_interface`` hold the code to :mod:`composer.spec.cvlr.skill_interface`,
the list of what the skill names. One that fails here means either the change breaks the skill, or
the interface module and the skill's text both need the new name.
"""

import shutil
import tomllib
from pathlib import Path

import pytest

from composer.cli.certora_cvlr import build_parser, main
from composer.spec.cvlr import skill_interface as interface
from composer.spec.cvlr.conf import DEFAULT_FEATURE
from composer.spec.cvlr.harness import SHARED_INVARIANTS, HarnessModule, declare_modules
from composer.spec.cvlr.scaffold import SPECS_DIR
from composer.spec.cvlr.skill import (
    REFERENCE_DIR,
    ComponentNameRejected,
    component_modules,
    reference_documents,
)

_REPO = Path(__file__).resolve().parent.parent


def _subcommand_flags() -> dict[str, set[str]]:
    parser = build_parser()
    (commands,) = [a for a in parser._actions if a.dest == "command"]
    assert isinstance(commands.choices, dict)
    return {
        name: {flag for action in sub._actions for flag in action.option_strings}
        for name, sub in commands.choices.items()
    }


@pytest.mark.skill_interface
def test_the_console_script_the_skill_runs_is_installed():
    scripts = tomllib.loads((_REPO / "pyproject.toml").read_text())["project"]["scripts"]
    assert scripts[interface.COMMAND] == "composer.cli.certora_cvlr:main"


@pytest.mark.skill_interface
def test_every_subcommand_and_flag_the_skill_passes_exists():
    flags = _subcommand_flags()
    for command, wanted in interface.SUBCOMMANDS.items():
        assert command in flags, f"no subcommand {command!r}"
        assert set(wanted) <= flags[command], set(wanted) - flags[command]


@pytest.mark.skill_interface
def test_the_reference_holds_every_file_and_section_the_skill_reads():
    docs = reference_documents(package="vault", cvlr_versions="cvlr 0.6.2", caveats=())
    assert str(REFERENCE_DIR) == interface.REFERENCE_DIR
    assert {Path(f) for f in interface.REFERENCE_FILES} <= set(docs)
    author = docs[Path("author.md")].splitlines()
    assert [h for h in interface.AUTHOR_SECTIONS if h not in author] == []


@pytest.mark.skill_interface
def test_the_harness_layout_is_where_the_skill_says():
    assert str(SPECS_DIR) == interface.SPECS_DIR
    assert str(SPECS_DIR / f"{SHARED_INVARIANTS}.rs") == interface.SHARED_INVARIANTS_FILE
    assert interface.UNIT_FEATURE.format(module="deposits") == HarnessModule("deposits").feature
    assert interface.HARNESS_FEATURE == DEFAULT_FEATURE


def test_every_recipe_the_index_names_is_a_file_under_its_id():
    docs = reference_documents(package="vault", cvlr_versions="cvlr 0.6.2", caveats=())
    index = docs[Path("recipes") / "index.md"]
    ids = [p.stem for p in docs if p.parent == Path("recipes") and p.name != "index.md"]
    assert ids
    assert [i for i in ids if f"Retrieval ID: {i}\n" not in index] == []


def test_a_shared_module_is_compiled_with_every_unit(tmp_path: Path):
    mod_rs = declare_modules(
        tmp_path, [HarnessModule("withdraw"), HarnessModule("deposit")], shared=("invariants",)
    )
    declarations = [line for line in mod_rs.read_text().splitlines() if not line.startswith("//")]
    assert declarations == [
        "",
        "pub mod invariants;",
        '#[cfg(feature = "certora_unit_deposit")]',
        "pub mod deposit;",
        '#[cfg(feature = "certora_unit_withdraw")]',
        "pub mod withdraw;",
    ]


def test_a_unit_may_not_take_a_shared_module_s_name(tmp_path: Path):
    with pytest.raises(ValueError, match="invariants"):
        declare_modules(tmp_path, [HarnessModule("invariants")], shared=("invariants",))


@pytest.mark.parametrize(
    ("names", "complaint"),
    [
        (["Deposits"], "use 'deposits'"),
        (["admin-config"], "use 'admin_config'"),
        (["invariants"], "shared invariants module"),
        (["deposits", "deposits"], "more than once"),
    ],
)
def test_a_component_name_the_agent_could_not_find_as_a_file_is_refused(names, complaint):
    with pytest.raises(ComponentNameRejected, match=complaint):
        component_modules(names)


def test_a_refused_component_name_exits_1_before_anything_is_written(tmp_path: Path, capsys):
    assert main(["setup", str(tmp_path), "--component", "Deposits"]) == 1
    assert "use 'deposits'" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


_MANIFEST = """\
[package]
name = "vault"
version = "0.1.0"
edition = "2021"

[lib]
crate-type = ["cdylib", "lib"]

[dependencies]
solana-program = "2"
"""


@pytest.mark.expensive
@pytest.mark.skill_interface
def test_setup_scaffolds_declares_and_compiles_and_a_second_setup_keeps_the_rules(
    tmp_path: Path, capsys
):
    """Against real cargo, off the network. The second setup names a component the first did not,
    and must keep what was written in the meantime."""
    if shutil.which("cargo") is None:
        pytest.skip("cargo is not on PATH")
    (tmp_path / "Cargo.toml").write_text(_MANIFEST)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("pub fn deposit(amount: u64) -> u64 { amount }\n")

    assert main(["setup", str(tmp_path), "--component", "deposits"]) == 0, capsys.readouterr()
    specs = tmp_path / SPECS_DIR
    authored = "//! The rules for deposits.\n// an author's work\n"
    (specs / "deposits.rs").write_text(authored)
    invariants = specs / "invariants.rs"
    invariants.write_text(invariants.read_text() + "// an invariant\n")

    assert main(
        ["setup", str(tmp_path), "--component", "deposits", "--component", "withdrawals"]
    ) == 0, capsys.readouterr()
    out = capsys.readouterr().out
    assert "compiled with --features certora: ok" in out
    assert (specs / "deposits.rs").read_text() == authored
    assert invariants.read_text().endswith("// an invariant\n")
    assert (specs / "withdrawals.rs").is_file()
    mod_rs = (specs / "mod.rs").read_text()
    assert "pub mod invariants;" in mod_rs and "pub mod withdrawals;" in mod_rs
    manifest = (tmp_path / "Cargo.toml").read_text()
    assert "certora_unit_deposits = []" in manifest and "certora_unit_withdrawals = []" in manifest
    reference = tmp_path / interface.REFERENCE_DIR
    assert all((reference / f).is_file() for f in interface.REFERENCE_FILES)
