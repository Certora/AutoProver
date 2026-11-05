"""A Rust ecosystem's component slug names its CVLR harness module, so it must be a legal ``pub mod``."""

import pytest

from composer.spec.util import slugify_rust_module


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("deposit", "deposit"),
        ("token-transfer", "token_transfer"),
        ("2fa_check", "spec_2fa_check"),
        ("_internal", "internal"),
        # From a real program, which drew `non_snake_case` warnings before lowercasing.
        ("Withdrawals_Fee_Distribution", "withdrawals_fee_distribution"),
        ("Withdrawals & Fees", "withdrawals_fees"),
        ("Fee--Split", "fee_split"),
        ("Café", "caf"),
        ("type", "spec_type"),
        ("Type", "spec_type"),
        ("match", "spec_match"),
        ("move", "spec_move"),
        ("Self", "spec_self"),
        ("gen", "spec_gen"),
        # A weak keyword is a legal module name.
        ("union", "union"),
    ],
)
def test_a_name_becomes_an_idiomatic_rust_identifier(name: str, expected: str):
    assert slugify_rust_module(name) == expected


@pytest.mark.parametrize("name", ["", "---", "___", "!!!"])
def test_a_name_with_nothing_left_falls_back(name: str):
    assert slugify_rust_module(name) == "unnamed"
