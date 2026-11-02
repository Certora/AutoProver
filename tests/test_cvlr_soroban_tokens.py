"""Pointing a Soroban contract's token client at the in-process model: which shapes are found, what
the edits add, and what is left alone. Sources only; no cargo.
"""

from pathlib import Path

from composer.spec.cvlr.soroban_tokens import modelled, plan_token_edits

_OFF = '#[cfg(not(feature = "certora"))]'
_ON = '#[cfg(feature = "certora")]'
_TARGET = "../../target/wasm32-unknown-unknown/release"


def _package(root: Path, **sources: str) -> Path:
    for rel, text in sources.items():
        path = root / "src" / rel.replace("__", "/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def _plan(root: Path):
    return plan_token_edits(root, skip=root / "src" / "certora")


def _edited(root: Path, rel: str = "lib.rs") -> str:
    return _plan(root).edited[root / "src" / rel][1]


def _apply(root: Path) -> None:
    for path, (_, edited) in _plan(root).edited.items():
        path.write_text(edited)


def _lines(text: str) -> list[str]:
    return [line.split(" //")[0].strip() for line in text.splitlines()]


def _only_adds(original: str, edited: str) -> bool:
    """Whether ``edited`` is ``original`` with lines inserted."""
    it = iter(edited.splitlines())
    return all(line in it for line in original.splitlines())


def test_a_grouped_import_of_the_sdk_token_module_gets_a_twin_naming_the_model(tmp_path):
    src = (
        "#[allow(unused_imports)]\n"
        "use soroban_sdk::{contract, token, Address, Env};\n"
        "\n"
        "fn client(e: &Env, a: &Address) -> token::Client<'_> { token::Client::new(e, a) }\n"
    )
    root = _package(tmp_path, **{"lib.rs": src})
    [site] = _plan(root).sites
    assert (site.name, site.line, site.done) == ("token", 2, False)
    edited = _edited(root)
    assert _only_adds(src, edited)
    assert _lines(edited)[:9] == [
        "#[allow(unused_imports)]",
        _OFF,
        "use soroban_sdk::{contract, token, Address, Env};",
        "#[allow(unused_imports)]",
        _ON,
        "use soroban_sdk::{contract, Address, Env};",
        "#[allow(unused_imports)]",
        _ON,
        "use crate::certora::soroban_token_model as token;",
    ]


def test_each_spelling_of_the_sdk_token_import_names_the_model_under_the_same_name(tmp_path):
    cases = {
        "use soroban_sdk::token;": "use crate::certora::soroban_token_model as token;",
        "pub use soroban_sdk::token as tk;": "pub use crate::certora::soroban_token_model as tk;",
        "use soroban_sdk::token::Client as TokenClient;": (
            "use crate::certora::soroban_token_model::Client as TokenClient;"
        ),
        # `self` would bring the model in as `soroban_token_model`; the code says `token::`.
        "use soroban_sdk::token::{self, TokenClient};": (
            "use crate::certora::soroban_token_model::{self as token, TokenClient};"
        ),
        "use soroban_sdk::{token::{self, StellarAssetClient}, Env};": (
            "use crate::certora::soroban_token_model::{self as token, StellarAssetClient};"
        ),
    }
    for i, (use, twin) in enumerate(cases.items()):
        root = _package(tmp_path / str(i), **{"lib.rs": use + "\n"})
        assert twin in _lines(_edited(root)), use


def test_a_contractimport_module_of_a_token_is_compiled_out_and_its_name_reexported(tmp_path):
    src = (
        "pub mod token_contract {\n"
        "    // The import generates a client.\n"
        f'    soroban_sdk::contractimport!(\n        file = "{_TARGET}/soroban_token_contract.wasm"\n    );\n'
        "}\n"
        "\n"
        "fn pay(e: &soroban_sdk::Env, t: &soroban_sdk::Address) { token_contract::Client::new(e, t); }\n"
    )
    root = _package(tmp_path, **{"lib.rs": src})
    [site] = _plan(root).sites
    assert (site.name, site.line) == ("token_contract", 1)
    edited = _edited(root)
    assert _only_adds(src, edited)
    lines = _lines(edited)
    assert lines[:2] == [_OFF, "pub mod token_contract {"]
    assert lines[7:9] == [_ON, "pub use crate::certora::soroban_token_model as token_contract;"]


def test_an_import_module_the_package_names_more_from_is_left_calling_the_real_token(tmp_path):
    root = _package(
        tmp_path,
        **{
            "lib.rs": (
                f'mod token_contract {{ soroban_sdk::contractimport!(file = "{_TARGET}/token.wasm"); }}\n'
            ),
            "storage.rs": "use crate::token_contract::AllowanceValue;\n",
        },
    )
    edits = _plan(root)
    assert edits.sites == () and edits.edited == {}
    [note] = edits.notes
    assert note.line == 1 and "AllowanceValue" in note.text


def test_an_imported_contract_that_is_not_a_token_is_not_touched(tmp_path):
    root = _package(
        tmp_path,
        **{"lib.rs": f'mod pool {{ soroban_sdk::contractimport!(file = "{_TARGET}/phoenix_pool.wasm"); }}\n'},
    )
    edits = _plan(root)
    assert edits.sites == () and edits.edited == {} and edits.notes == ()


def test_a_token_client_spelled_in_full_is_noted_and_not_edited(tmp_path):
    root = _package(
        tmp_path,
        **{"lib.rs": "fn f(e: &Env, a: &Address) { soroban_sdk::token::Client::new(e, a); }\n"},
    )
    edits = _plan(root)
    assert edits.sites == () and edits.edited == {}
    [note] = edits.notes
    assert "soroban_sdk::token::Client" in note.text


def test_comments_strings_and_test_modules_are_not_sites(tmp_path):
    root = _package(
        tmp_path,
        **{
            "lib.rs": (
                "// use soroban_sdk::token;\n"
                "/* mod token_contract { soroban_sdk::contractimport!(file = \"t.wasm\"); } */\n"
                'const S: &str = "use soroban_sdk::token;";\n'
                "#[cfg(test)]\n"
                "mod test {\n"
                "    use soroban_sdk::token;\n"
                "}\n"
            ),
            "test.rs": "use soroban_sdk::token;\n",
        },
    )
    edits = _plan(root)
    assert edits.sites == () and edits.edited == {} and edits.notes == ()


def test_a_second_plan_finds_the_sites_done(tmp_path):
    root = _package(
        tmp_path,
        **{
            "lib.rs": (
                "use soroban_sdk::{contract, token, Env};\n"
                f'pub mod token_contract {{ soroban_sdk::contractimport!(file = "{_TARGET}/token.wasm"); }}\n'
            )
        },
    )
    _apply(root)
    again = _plan(root)
    assert again.edited == {}
    assert sorted((s.name, s.done) for s in again.sites) == [("token", True), ("token_contract", True)]


def test_the_prompt_reads_the_sites_back_only_once_the_model_is_written(tmp_path):
    root = _package(tmp_path, **{"lib.rs": "use soroban_sdk::{contract, token, Env};\n"})
    harness = root / "src" / "certora"
    _apply(root)
    assert modelled(root, harness) == ()
    harness.mkdir(parents=True)
    (harness / "soroban_token_model.rs").write_text("")
    [site] = modelled(root, harness)
    assert site.name == "token" and site.path == root / "src" / "lib.rs"
