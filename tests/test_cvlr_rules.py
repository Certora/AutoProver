"""Which rules a CVLR harness declares. A submission names these.

A name this misses is a rule the submission will not check. A name it invents
is a rule the prover will not recognize. The parametric cases are checked
against the names the crate's documentation says its macros generate.
"""

import io
import pathlib

import json5
import pytest

from composer.spec.cvlr.rules import UnreadableRules, generated_name, rule_names, snake_case

# Verbatim from `cvlr_invariant_rules!`'s doc comment, which states the three rules it creates.
CRATE_DOC_EXAMPLE = """
cvlr_invariant_rules! {
    name: "non_negative",
    assumption: counter_is_positive,
    invariant: counter_non_negative,
    bases: [
        base_update_counter,
        base_reset_counter,
        base_increment_counter,
    ]
}
"""


def test_the_parametric_names_match_what_the_crate_documents():
    # The crate's doc comment lists these three names. A different spelling
    # would not match the rule the prover reports.
    assert rule_names(CRATE_DOC_EXAMPLE) == (
        "non_negative_update_counter",
        "non_negative_reset_counter",
        "non_negative_increment_counter",
    )


@pytest.mark.parametrize(
    "written,expected",
    [
        ("non_negative", "non_negative"),
        ("Vault Solvency", "vault_solvency"),
        ("vault-solvency", "vault_solvency"),
        ("Vault  --  Solvency", "vault_solvency"),
        ("__leading_and_trailing__", "leading_and_trailing"),
    ],
)
def test_the_spec_name_is_snake_cased_the_way_the_crate_does(written, expected):
    # The crate's to_snake_case: lowercase, non-alphanumerics to underscore, runs collapsed.
    assert snake_case(written) == expected


def test_a_base_prefix_is_stripped_from_the_base_function():
    # cvlr_rule_for_spec! strips a leading `base_`. Both spellings produce the same rule.
    assert generated_name("solvency", "base_deposit") == "solvency_deposit"
    assert generated_name("solvency", "deposit") == "solvency_deposit"


def test_a_spec_expression_with_its_own_brackets_does_not_swallow_the_bases():
    # Brackets inside the spec expression must not be read as the `bases` list.
    source = """
    cvlr_rules! {
        name: "Vault Solvency",
        spec: cvlr_spec! { requires: [a, b], ensures: cvlr_and(x, y) },
        bases: [base_deposit, withdraw],
    }
    """
    assert rule_names(source) == ("vault_solvency_deposit", "vault_solvency_withdraw")


def test_an_attribute_separated_from_its_signature_is_still_found():
    # `#[rule]` may sit above a doc comment and another attribute. A same-line scan misses that.
    source = """
    #[rule]
    /// What this rule shows.
    #[allow(dead_code)]
    pub unsafe fn rule_solvency() {}
    """
    assert rule_names(source) == ("rule_solvency",)


def test_names_come_back_in_source_order_whichever_kind_they_are():
    # `#[rule]` and the parametric macros are separate scans. Concatenating them would put `last`
    # ahead of the rules the macro expands to.
    source = "#[rule]\nfn first() {}\n" + CRATE_DOC_EXAMPLE + "#[rule]\nfn last() {}\n"
    assert rule_names(source) == (
        "first",
        "non_negative_update_counter",
        "non_negative_reset_counter",
        "non_negative_increment_counter",
        "last",
    )


def test_an_invocation_with_no_bases_declares_nothing():
    # An unfinished macro call declares no rule. A name invented here would be submitted.
    assert rule_names('cvlr_rules! { name: "x", spec: s, bases: [] }') == ()
    assert rule_names('cvlr_rules! { name: "x", spec: s }') == ()


def test_a_doc_comment_does_not_steal_the_functions_name():
    # The next `fn` token after `#[rule]` may sit in the doc comment. The attribute decorates
    # `rule_first`. Reporting only `rule_other` would let that one verified rule stamp the draft.
    source = """
    #[rule]
    /// Same shape as fn rule_other.
    fn rule_first() {}

    #[rule]
    fn rule_other() {}
    """
    assert rule_names(source) == ("rule_first", "rule_other")


def test_a_comment_in_the_bases_list_is_not_part_of_a_name():
    source = """
    cvlr_rules! {
        name: "solvency",
        spec: spec,
        bases: [
            base_deposit, // the deposit path
            base_withdraw,
        ]
    }
    """
    assert rule_names(source) == ("solvency_deposit", "solvency_withdraw")


def test_the_attributes_other_spellings_name_the_function():
    # `#[rule]` ignores its tokens. `cvlr::rule` and `cvlr_rule` are the same attribute.
    source = """
    #[cvlr::rule]
    fn rule_through_the_crate() {}
    #[cvlr_rule]
    pub fn rule_renamed() {}
    #[rule(never_panic)]
    fn rule_with_tokens() {}
    """
    assert rule_names(source) == (
        "rule_through_the_crate",
        "rule_renamed",
        "rule_with_tokens",
    )


def test_a_direct_cvlr_rule_for_spec_names_its_base():
    source = """
    cvlr_rule_for_spec! {
        name: "solvency",
        spec: MySpec,
        base: base_deposit,
    }
    """
    assert rule_names(source) == ("solvency_deposit",)


def test_a_function_mentioned_in_a_string_or_a_comment_is_not_a_rule():
    source = """
    #[rule]
    fn rule_real() {}
    const NOTE: &str = "#[rule]\\nfn rule_hidden() {}";
    /* #[rule] fn rule_in_a_comment() {} */
    // #[rule]
    // fn rule_on_a_commented_line() {}
    """
    assert rule_names(source) == ("rule_real",)


def test_a_macro_template_declares_no_rule_until_an_invocation_pastes_one():
    source = """
    macro_rules! cvlr_impl_rule {
        ($rule_name:ident, $spec:expr, $base:ident) => {
            #[cvlr::rule]
            pub fn $rule_name() {}
        };
    }
    cvlr_rules! {
        name: "solvency",
        spec: spec,
        bases: [base_deposit],
    }
    """
    assert rule_names(source) == ("solvency_deposit",)


def test_a_rule_attribute_that_does_not_decorate_a_function_is_refused():
    source = "#[rule]\nstruct NotARule {}\n#[rule]\nfn rule_real() {}\n"
    with pytest.raises(UnreadableRules, match="withheld"):
        rule_names(source)


def test_a_base_that_is_not_an_identifier_is_refused():
    source = """
    cvlr_rules! {
        name: "solvency",
        spec: spec,
        bases: [base_deposit, 1],
    }
    #[rule]
    fn rule_real() {}
    """
    with pytest.raises(UnreadableRules, match="not an identifier"):
        rule_names(source)


def test_a_repeated_name_is_reported_once_and_left_to_the_compiler():
    # A duplicated name is a compiler error. Report it once, so a set comparison stays honest.
    assert rule_names("#[rule]\nfn dup() {}\n#[rule]\nfn dup() {}\n") == ("dup",)


EXAMPLES = pathlib.Path("~/src/SolanaExamples/cvlr_by_example").expanduser()


@pytest.mark.parametrize(
    "spec_file,conf_file",
    [
        ("first_example/src/certora/spec/checks.rs", "first_example/certora/conf/Default.conf"),
        ("vault_application/src/certora/spec.rs", "vault_application/certora/conf/Default.conf"),
    ],
)
def test_the_extracted_names_are_exactly_what_the_examples_authors_declared(spec_file, conf_file):
    """The names match a hand-written conf's ``rule`` list.

    Skipped when the examples repo is absent. A vendored copy would be a second
    list to keep in step, and this check is only useful if someone else wrote
    the names.
    """
    spec, conf = EXAMPLES / spec_file, EXAMPLES / conf_file
    if not spec.is_file() or not conf.is_file():
        pytest.skip(f"the public examples repo is not checked out at {EXAMPLES}")
    declared = json5.load(io.StringIO(conf.read_text()), allow_duplicate_keys=False)["rule"]
    assert set(rule_names(spec.read_text())) == set(declared)
