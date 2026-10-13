"""The Rust in the CVLR recipes compiles against the pinned crates, or says it is not compiled.

A recipe is prompt text an author copies from, so it is held to the doctest contract the API
corpus is (``composer/cargo/doctest.py``): a ``rust`` block must compile with the reference set
a scaffolded project declares, and a fragment that cannot — one that calls the program under
verification, or leaves the setup as a ``/* ... */`` — is fenced ``rust,ignore``, which both
rustdoc and a reader take to mean "not compiled".
"""

from pathlib import Path

import pytest
import yaml

from composer.cargo.doctest import Failed, FenceClass, check_examples, fences, reveal_hidden_lines
from tests.test_cargo_doctest import reference_crate

_RESOURCES = Path(__file__).parent.parent / "composer" / "kb" / "resources"


def _recipes() -> list[Path]:
    index = yaml.safe_load((_RESOURCES / "cvlr_recipes_index.yaml").read_text())
    return [_RESOURCES / recipe["file"] for recipe in index["recipes"]]


def test_every_recipe_block_is_rust_that_is_compiled_or_says_it_is_not():
    """A typo'd fence (``rust-ignore``, ``rs``) would make a block quietly skip the compile."""
    for recipe in _recipes():
        for fence in fences(recipe.read_text()):
            assert fence.kind in (FenceClass.COMPILED, FenceClass.IGNORED), (
                f"{recipe.name}: a ```{fence.info} block is neither compiled nor marked ignore"
            )


@pytest.mark.expensive
def test_every_compiled_recipe_block_compiles(tmp_path):
    blocks = [
        (recipe.name, reveal_hidden_lines(fence.body))
        for recipe in _recipes()
        for fence in fences(recipe.read_text())
        if fence.kind is FenceClass.COMPILED
    ]
    verdicts = check_examples(reference_crate(tmp_path / "crate"), [s for _, s in blocks])
    failed = [
        f"{name}:\n{verdict.diagnostics}"
        for (name, _), verdict in zip(blocks, verdicts)
        if isinstance(verdict, Failed)
    ]
    assert not failed, "\n\n".join(failed)
