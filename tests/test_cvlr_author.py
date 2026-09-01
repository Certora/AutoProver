"""The CVLR author's gates, without an LLM.

Everything here is a property of the machinery around the agent rather than of the agent, which is
what makes it testable in the routine env — and the machinery is where the interesting failures are:

* **The publish gate's ground truth.** ``validate_property_rules`` checks the declared mapping
  against the rules the *draft* declares, both directions. A property claiming a rule that does not
  exist is a hallucinated deliverable; a rule nobody claimed is prover time nobody chose to spend.
* **The shared module declaration.** ``specs/mod.rs`` names every unit, and a named module with no
  file is a compile error — so a unit whose sibling has not authored yet would fail its own gate.
  That is the whole reason the formalizer is staged.
* **What "accounted for" means.** The prover gate passes with a violated rule in it, provided the
  author marked it. Getting that wrong in either direction is bad: demanding green pushes the author
  to weaken rules until findings disappear, and accepting anything makes the gate decorative.
"""

from pathlib import Path

import pytest

from composer.authoring.state import SkippedProperty
from composer.spec.cvlr.harness import (
    CvlrArtifactStore,
    GeneratedHarness,
    HarnessModule,
    module_name,
)
from composer.spec.cvlr.state import PropertyRuleMapping, validate_property_rules
from composer.spec.cvlr.verify import _unaccounted, _wrongly_expected

DRAFT = """
use cvlr::prelude::*;

/// solvency_holds
#[rule]
pub fn rule_solvency() {
    let x: u64 = nondet();
    clog!(x);
    cvlr_assert!(x == x);
}

cvlr_rules! {
    name: "no_underflow",
    spec: my_spec,
    bases: [base_withdraw, base_deposit],
}
"""


def _mapping(**pairs: list[str]) -> list[PropertyRuleMapping]:
    return [
        PropertyRuleMapping(property_title=title, rules=rules) for title, rules in pairs.items()
    ]


def test_a_mapping_that_matches_the_draft_is_accepted():
    # The parametric invocation's *generated* names are what a property may claim — naming the
    # invocation would be naming something the prover never reports.
    err = validate_property_rules(
        _mapping(
            solvency_holds=["rule_solvency"],
            withdraw_never_underflows=["no_underflow_withdraw", "no_underflow_deposit"],
        ),
        [],
        ["solvency_holds", "withdraw_never_underflows"],
        DRAFT,
    )
    assert err is None


def test_a_property_cannot_claim_a_rule_the_draft_does_not_declare():
    # The hallucination this gate exists for: a mapping that reads plausibly and names nothing real.
    err = validate_property_rules(
        _mapping(solvency_holds=["rule_solvency_v2"]), [], ["solvency_holds"], DRAFT
    )
    assert err is not None
    assert "rule_solvency_v2" in err


def test_a_declared_rule_must_be_tied_back_to_a_property():
    # The other direction. A rule nobody claimed still costs a prover run every iteration, and it is
    # how a harness accumulates work nobody asked for.
    err = validate_property_rules(
        _mapping(solvency_holds=["rule_solvency"]), [], ["solvency_holds"], DRAFT
    )
    assert err is not None
    assert "no_underflow_withdraw" in err


def test_a_property_that_is_neither_skipped_nor_mapped_is_rejected():
    err = validate_property_rules(
        _mapping(
            solvency_holds=["rule_solvency"],
            withdraw_never_underflows=["no_underflow_withdraw", "no_underflow_deposit"],
        ),
        [],
        ["solvency_holds", "withdraw_never_underflows", "forgotten_one"],
        DRAFT,
    )
    assert err is not None and "forgotten_one" in err


def test_a_skipped_property_may_be_absent_but_not_mapped():
    skipped = [SkippedProperty(property_title="forgotten_one", reason="no token model exists")]
    titles = ["solvency_holds", "withdraw_never_underflows", "forgotten_one"]
    full = _mapping(
        solvency_holds=["rule_solvency"],
        withdraw_never_underflows=["no_underflow_withdraw", "no_underflow_deposit"],
    )
    assert validate_property_rules(full, skipped, titles, DRAFT) is None
    # Claiming a rule for a property you also declared unformalizable is a contradiction.
    both = full + _mapping(forgotten_one=["rule_solvency"])
    assert validate_property_rules(both, skipped, titles, DRAFT) is not None


@pytest.mark.parametrize(
    "slug,expected",
    [
        ("deposit", "deposit"),
        ("token-transfer", "token_transfer"),
        ("2fa_check", "spec_2fa_check"),
        ("_internal", "internal"),
        # The three the first live run against a real program actually produced. They were valid
        # module paths and every one of them earned a `non_snake_case` warning.
        ("Deposits", "deposits"),
        ("Vault_Initialization", "vault_initialization"),
        ("Withdrawals_Fee_Distribution", "withdrawals_fee_distribution"),
        # Separator runs collapse rather than producing `a__b`.
        ("Withdrawals & Fees", "withdrawals_fees"),
    ],
)
def test_a_slug_becomes_an_idiomatic_rust_identifier(slug, expected):
    # A component slug is not constrained to Rust's identifier grammar, and a module path the
    # compiler rejects is discovered long after the name was chosen. Validity is not the only bar,
    # though: this name is written into someone else's crate, so it also has to be snake_case — a
    # crate that denies `non_snake_case` turns the warning into an error the author cannot fix.
    assert module_name(slug) == expected


def test_two_slugs_that_reduce_to_one_module_are_refused(tmp_path):
    # Silent is the failure mode to avoid: they would share a file, so one unit's harness would
    # overwrite the other's, both gates would pass, and the report would claim two delivered units
    # on one body of work.
    store = CvlrArtifactStore(tmp_path, Path("programs/prog"))
    with pytest.raises(ValueError, match="share a harness module name"):
        store.declare_modules([HarnessModule("Fee-Split"), HarnessModule("fee_split")])


def test_every_unit_gets_a_module_and_a_file_before_any_unit_authors(tmp_path):
    # `mod x;` with no `x.rs` is a compile error, so a unit whose sibling has not authored yet would
    # fail its *own* compile gate. This is what the staged formalizer is for.
    store = CvlrArtifactStore(tmp_path, Path("programs/prog"))
    modules = [HarnessModule("deposit"), HarnessModule("withdraw")]
    mod_rs = store.declare_modules(modules)

    text = mod_rs.read_text()
    assert "mod deposit;" in text and "mod withdraw;" in text
    for module in modules:
        placeholder = mod_rs.parent / module.artifact_file
        assert placeholder.is_file(), f"{module.artifact_file} was declared but not created"


def test_declaring_modules_never_clobbers_an_authored_harness(tmp_path):
    # Re-declaration happens on a resumed run, and by then some units have real content.
    store = CvlrArtifactStore(tmp_path, Path("."))
    store.declare_modules([HarnessModule("deposit")])
    authored = tmp_path / "src" / "certora" / "specs" / "deposit.rs"
    authored.write_text("#[rule]\nfn rule_real() {}\n")

    store.declare_modules([HarnessModule("deposit"), HarnessModule("withdraw")])
    assert "rule_real" in authored.read_text()


def test_a_marked_rule_does_not_block_the_gate_but_an_unmarked_failure_does():
    status = {"rule_a": True, "rule_b": False, "rule_c": False}
    expected = {"rule_b": "the program really does underflow here"}
    # rule_b is a finding the author took responsibility for; rule_c is unfinished work.
    assert _unaccounted(status, expected) == ["rule_c"]


def test_a_rule_marked_expected_to_fail_that_verifies_is_reported():
    # The more interesting direction: either the defect is not there or the rule does not test for
    # it, and both want the author's attention before the run is called finished.
    status = {"rule_b": True}
    assert _wrongly_expected(status, {"rule_b": "should underflow"}) == ["rule_b"]


def test_the_published_result_reports_the_rules_it_read_off_the_draft():
    # `declared_rules` is read from the draft, not transcribed by the model, so the report cannot
    # claim a rule the harness does not contain.
    harness = GeneratedHarness(
        commentary="ok",
        harness=DRAFT,
        property_rules=_mapping(solvency_holds=["rule_solvency"]),
        declared_rules=["rule_solvency", "no_underflow_withdraw", "no_underflow_deposit"],
        final_link="https://prover.certora.com/output/1/abc",
    )
    assert harness.artifact_text == DRAFT
    assert harness.output_link == "https://prover.certora.com/output/1/abc"
    assert harness.property_checks() == [("solvency_holds", ["rule_solvency"])]
