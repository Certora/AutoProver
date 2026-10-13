"""rustdoc's doctest conventions, and the ``cargo check`` that holds examples to them.

Everything above the cargo section is pure. The cargo test at the bottom is ``expensive`` like
every other test here that runs cargo: it is offline and quick once the target directory is warm,
but a cold one compiles the CVLR family and ``solana-program`` first.
"""

import shutil
from pathlib import Path

import pytest

from composer.cargo.doctest import (
    Failed,
    FenceClass,
    Passed,
    attribute_errors,
    check_examples,
    classify,
    fences,
    reveal_hidden_lines,
    wrap,
)
from composer.spec.cvlr.reference import SOLANA_V3


@pytest.mark.parametrize(
    "info, expected",
    [
        ("", FenceClass.COMPILED),
        ("rust", FenceClass.COMPILED),
        ("rust,no_run", FenceClass.COMPILED),
        ("should_panic", FenceClass.COMPILED),
        ("edition2021", FenceClass.COMPILED),
        ("ignore", FenceClass.IGNORED),
        ("rust,ignore", FenceClass.IGNORED),
        ("ignore-wasm32", FenceClass.IGNORED),
        ("compile_fail,E0308", FenceClass.COMPILE_FAIL),
        ("text", FenceClass.NOT_RUST),
        ("toml", FenceClass.NOT_RUST),
        # rustdoc keeps a block Rust when `rust` is written next to an unknown word.
        ("rust,custom", FenceClass.COMPILED),
    ],
)
def test_a_fence_is_classified_as_rustdoc_classifies_it(info, expected):
    assert classify(info) is expected


def test_fences_are_found_with_their_extent():
    text = "a\n```rust,no_run\nlet x = 1;\n```\nb\n~~~~\n```\ninner\n~~~~\nc"
    found = fences(text)
    assert [(f.info, f.body) for f in found] == [
        ("rust,no_run", "let x = 1;\n"),
        ("", "```\ninner\n"),
    ]
    assert text[found[0].start : found[0].end] == "```rust,no_run\nlet x = 1;\n```\n"
    assert text[found[1].end :] == "c"


def test_an_unclosed_fence_runs_to_the_end_of_the_text():
    text = "a\n```\nlet x = 1;\n"
    (fence,) = fences(text)
    assert fence.body == "let x = 1;\n" and fence.end == len(text)


def test_inline_triple_backticks_in_prose_open_no_fence():
    assert fences("write ```foo``` inline\n") == []


def test_hidden_lines_are_compiled_and_escapes_unescaped():
    body = "# use cvlr::prelude::*;\n#\nlet x = 1;\n## not hidden\n#[derive(Clone)]\n"
    assert reveal_hidden_lines(body) == (
        "use cvlr::prelude::*;\n\nlet x = 1;\n# not hidden\n#[derive(Clone)]\n"
    )


def test_a_block_without_main_becomes_its_body():
    assert wrap("let x = 1;\n") == "fn main() {\nlet x = 1;\n}\n"


def test_a_block_with_main_is_taken_as_written():
    assert wrap("fn main() {}\n") == "fn main() {}\n"


def test_leading_inner_attributes_stay_outside_main():
    assert wrap("#![allow(unused)]\nlet x = 1;\n") == (
        "#![allow(unused)]\nfn main() {\nlet x = 1;\n}\n"
    )


def _error(file_name: str, *, expanded_from: str | None = None, text: str = "boom") -> dict:
    span: dict = {"file_name": file_name, "is_primary": True}
    if expanded_from is not None:
        span["expansion"] = {"span": {"file_name": expanded_from}}
    return {
        "reason": "compiler-message",
        "message": {"level": "error", "message": text, "rendered": text, "spans": [span]},
    }


def test_an_error_is_attributed_to_the_block_whose_file_it_is_in():
    modules = {"example_0000": 0, "example_0001": 1}
    per_block, stray = attribute_errors([_error("src/doc_examples/example_0001.rs")], modules)
    assert per_block == {1: ["boom"]} and stray == []


def test_an_error_inside_a_macro_lands_on_the_block_that_called_it():
    registry = "/home/u/.cargo/registry/src/cvlr-asserts-0.6.1/src/lib.rs"
    error = _error(registry, expanded_from="src/doc_examples/example_0000.rs")
    per_block, stray = attribute_errors([error], {"example_0000": 0})
    assert per_block == {0: ["boom"]} and stray == []


def test_an_error_no_block_accounts_for_is_reported_as_such():
    per_block, stray = attribute_errors([_error("src/lib.rs")], {"example_0000": 0})
    assert per_block == {} and stray == ["boom"]


def test_rustc_s_closing_summary_is_nobody_s_error():
    summary = {
        "reason": "compiler-message",
        "message": {"level": "error", "message": "aborting due to 1 previous error", "spans": []},
    }
    assert attribute_errors([summary], {}) == ({}, [])


def test_warnings_and_build_records_are_ignored():
    warning = _error("src/doc_examples/example_0000.rs")
    warning["message"]["level"] = "warning"
    assert attribute_errors([warning, {"reason": "build-finished"}], {"example_0000": 0}) == (
        {},
        [],
    )


# ---------------------------------------------------------------------------------------------
# against cargo


def reference_crate(root: Path) -> Path:
    """A crate depending on exactly the reference set: what a scaffolded project declares."""
    if shutil.which("cargo") is None:
        pytest.skip("cargo is not on PATH")
    (root / "src").mkdir(parents=True, exist_ok=True)
    patches = "".join(
        f"{commit.patch_line(crate.name)}\n" for crate, commit in SOLANA_V3.from_commits()
    )
    (root / "Cargo.toml").write_text(
        '[package]\nname = "examples"\nversion = "0.0.0"\nedition = "2021"\n\n'
        f"[dependencies]\n{SOLANA_V3.cargo_dependencies()}\n"
        + (f"\n[patch.crates-io]\n{patches}" if patches else "")
    )
    return root


@pytest.mark.expensive
def test_each_example_gets_its_own_verdict_from_one_crate(tmp_path):
    sources = [
        # The identifiers the CVLR recipes lean on, so a rename upstream fails here.
        "use cvlr::prelude::*;\n#[rule]\nfn r() {\n    let x = 1u64;\n    clog!(x);\n"
        "    cvlr_assert!(x > 0);\n    cvlr_satisfy!(true);\n}\n",
        "use cvlr::prelude::*;\ncvlr_assert_le!(1u64, \"a\");\n",
        "use cvlr::not_a_module::thing;\n",
        "let x = ;\n",
        "use cvlr::prelude::*;\nlet v: u64 = nondet();\ncvlr_assume!(v > 0);\n",
    ]
    verdicts = check_examples(reference_crate(tmp_path / "crate"), sources)
    assert [type(v) for v in verdicts] == [Passed, Failed, Failed, Failed, Passed]
    failures = [v.diagnostics for v in verdicts if isinstance(v, Failed)]
    assert "E0308" in failures[0]  # reported inside the macro's expansion
    assert "E0432" in failures[1]
    assert "expected expression" in failures[2]
