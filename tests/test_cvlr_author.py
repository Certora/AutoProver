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

import asyncio
import json
from types import SimpleNamespace

import pytest

from composer.authoring.state import SkippedProperty, make_validation_stamper, spec_digest
from composer.spec.cvlr.conf import RunOverlay, SelectRules, solana_conf
from composer.spec.cvlr.prover import Submission as CvlrSubmission
from composer.spec.cvlr.harness import (
    DELIVERABLE_DIR,
    CvlrArtifactStore,
    GeneratedHarness,
    HarnessModule,
    module_name,
)
from composer.spec.cvlr.state import (
    PROVER_VALIDATION_KEY,
    DrivesHarnessMirror,
    DrivesProgramFunction,
    PropertyRuleMapping,
    validate_property_rules,
    validate_rule_subjects,
)
from composer.spec.cvlr.tuning import SummaryDirective
from composer.spec.cvlr import verify as verify_mod
from composer.spec.cvlr.verify import _unaccounted, _wrongly_expected

_DRAFT_NO_RULES = "//! Every property blocked; see the skips.\n"

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


# ---------------------------------------------------------------------------------------------
# What each rule drives
#
# The authoring loop's first real run published two units whose every rule drove a hand-written
# mirror of the handler — a `fn deposit_balance_update`, a `fn withdraw_logic` — because [3308] made
# the real handlers unanalyzable. The prover verified them, the judge accepted them, and the only
# record of the substitution was a doc comment nothing downstream read. The prompts are what stop
# that happening (`tests/test_cvlr_knowledge.py`); this is what stops it happening *silently*.


def test_every_declared_rule_must_say_what_it_drives():
    err = validate_rule_subjects(
        [DrivesProgramFunction(rule="rule_solvency", function="crate::vault_program::deposit")],
        DRAFT,
    )
    assert err is not None
    assert "no_underflow_deposit" in err and "no_underflow_withdraw" in err


def test_a_subject_cannot_name_a_rule_the_draft_does_not_declare():
    err = validate_rule_subjects(
        [DrivesProgramFunction(rule="rule_solvency_v2", function="crate::f")], DRAFT
    )
    assert err is not None and "rule_solvency_v2" in err


def test_a_mirror_is_accepted_but_must_say_what_it_stands_in_for():
    """The point is not to forbid the stand-in — today [3308] leaves no alternative for some
    handlers — but to make it a declaration rather than a doc comment."""
    subjects = [
        DrivesProgramFunction(rule="rule_solvency", function="crate::vault_program::deposit"),
        DrivesHarnessMirror(
            rule="no_underflow_withdraw",
            mirrors="crate::vault_program::withdraw",
            reason="[3308] on the #[error_code] path; summarizing and weakening both failed",
        ),
        DrivesHarnessMirror(
            rule="no_underflow_deposit",
            mirrors="crate::vault_program::deposit",
            reason="[3308] on the #[error_code] path; summarizing and weakening both failed",
        ),
    ]
    assert validate_rule_subjects(subjects, DRAFT) is None


def test_an_all_skipped_unit_can_satisfy_the_prover_gate():
    """The wall the honest exit ran into. A unit whose every property is blocked declares no rules,
    so `verify_rules` refused it, so no stamp, so `result` refused it, so `give_up` was the only way
    out — and a considered "nothing here is formalizable, and here is why" got reported as a
    failure. Measured: two of three units in an end-to-end run ended exactly this way."""
    state = _verify_state(_DRAFT_NO_RULES)
    state["skipped"] = [SkippedProperty(property_title="p", reason="[3308] on the handler")]
    deps = SimpleNamespace(stamper=make_validation_stamper(PROVER_VALIDATION_KEY))
    token = verify_mod.VerifyRules._dep_ctx.set(deps)
    try:
        tool = verify_mod.VerifyRules(state=state, tool_call_id="tc")
        stamped = tool._nothing_to_submit()
    finally:
        verify_mod.VerifyRules._dep_ctx.reset(token)
    assert not isinstance(stamped, str), stamped
    assert stamped.update["validations"][PROVER_VALIDATION_KEY] == spec_digest(
        _DRAFT_NO_RULES, state["skipped"], ()
    )


def test_a_draft_with_no_rules_and_no_skips_is_still_unfinished():
    """The other half, and the reason stamping the first case is safe rather than a hole: an empty
    draft with nothing declared and nothing skipped has said nothing at all."""
    tool = verify_mod.VerifyRules(state=_verify_state(_DRAFT_NO_RULES), tool_call_id="tc")
    answer = tool._nothing_to_submit()
    assert isinstance(answer, str) and "nothing to check" in answer


def test_a_mirror_declaration_reaches_the_published_artifact():
    """`expected_failures` set the precedent: a caveat the report needs is carried, not smoothed
    over. A verdict earned against a stand-in is worth something different from one earned against
    the program, and the deliverable is the only place a reader can learn which they have."""
    mirror = DrivesHarnessMirror(
        rule="rule_solvency", mirrors="crate::vault_program::deposit", reason="[3308]"
    )
    published = GeneratedHarness(commentary="c", harness=DRAFT, rule_subjects=[mirror])
    assert published.rule_subjects == [mirror]
    assert published.model_dump()["rule_subjects"][0]["subject"] == "harness_mirror"


def test_the_declarations_are_written_to_disk_and_not_just_to_the_checkpoint(tmp_path):
    """They reached the checkpoint and died there: an end-to-end run published seven rules with
    their subjects correctly declared, and nothing in `certora/cvlr/` said so. The base store writes
    the module, the commentary and the property map — neither of this backend's two claims is any of
    those."""
    store = CvlrArtifactStore(tmp_path, Path("programs/vault"))
    published = GeneratedHarness(
        commentary="c",
        harness=DRAFT,
        rule_subjects=[
            DrivesProgramFunction(rule="rule_solvency", function="crate::vault_program::deposit")
        ],
        summaries=[SummaryDirective(pattern="^<vault::E as Display>::fmt$", why="[3308]")],
    )
    store.write_artifact(HarnessModule(slug="deposits"), published)
    written = json.loads(
        (tmp_path / DELIVERABLE_DIR / "properties" / "cvlr_deposits.assumptions.json").read_text()
    )
    assert written["rule_subjects"][0]["function"] == "crate::vault_program::deposit"
    assert written["summaries"][0]["why"] == "[3308]"


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


# ---------------------------------------------------------------------------
# The prover submission names the draft's rules
# ---------------------------------------------------------------------------

class _StopProbe(Exception):
    """Cuts the tool off once the submission is captured; no prover is involved."""


_DRAFT_TWO_RULES = """
use cvlr::prelude::*;

#[rule]
fn rule_balance_conserved() { cvlr_assert!(true); }

#[rule]
fn rule_only_authority_withdraws() { cvlr_assert!(true); }
"""


def _verify_state(draft: str) -> dict:
    return {
        "messages": [],
        "curr_spec": draft,
        "expected_failures": {},
        "skipped": [],
        "property_rules": [],
        "rule_subjects": [],
        "summaries": [],
        "munges": [],
        "required_validations": [],
        "validations": {},
        "failed": None,
        "budget_curtailed": False,
    }


@pytest.mark.asyncio
async def test_the_submission_names_exactly_the_rules_the_draft_declares(monkeypatch):
    """The conf must carry a ``rule`` entry, and it must be this draft's rules.

    A regression guard on a real outage, not a hypothetical. This line was once
    ``rules=deps.submission.rules`` — a self-assignment — so every submission inherited from a base
    conf that names no rules. A cloud job with no rule selection ends in FAILED with no report and
    nothing on disk, so the authoring loop saw only "status FAILED" and simplified its harness
    until it was a tautology, which failed identically. Nothing else in the suite would notice: the
    conf machinery is correct, the build is correct, and the bug lives entirely in what is handed
    across.

    Also pins *which* selection. ``AllRules`` would look right and be wrong — the build is
    whole-crate, so it would grade this unit on every sibling unit's rules.
    """
    from composer.spec.cvlr import verify as verify_mod

    captured: dict = {}

    async def fake_submit(session, submission, **kwargs):
        captured["rules"] = submission.rules
        raise _StopProbe

    monkeypatch.setattr(verify_mod, "submit", fake_submit)

    deps = SimpleNamespace(
        lock=asyncio.Lock(),
        target=SimpleNamespace(session=None, stage=lambda draft, summaries=(), munges=(): None),
        submission=CvlrSubmission(manifest_path=Path("/w/Cargo.toml"), base_conf={}),
        prover_opts=None,
        analysis=None,
        stamper=None,
    )
    token = verify_mod.VerifyRules._dep_ctx.set(deps)
    try:
        tool = verify_mod.VerifyRules(state=_verify_state(_DRAFT_TWO_RULES), tool_call_id="tc")
        with pytest.raises(_StopProbe):
            await tool.run()
    finally:
        verify_mod.VerifyRules._dep_ctx.reset(token)

    assert captured["rules"] == SelectRules(
        ("rule_balance_conserved", "rule_only_authority_withdraws")
    )
    # And the selection actually reaches the conf as a `rule` entry.
    conf = solana_conf({}, RunOverlay(build_script="/w/b.py", rules=captured["rules"]))
    assert conf["rule"] == ["rule_balance_conserved", "rule_only_authority_withdraws"]


def test_the_unit_workdir_is_not_inside_a_prover_internal_directory():
    """Where the per-unit workspace lives changes what the prover receives.

    The collector skips paths under ``.certora_internal``, so a workspace placed there uploads its
    ``.so`` and tuning files and none of its Rust. Nothing reports it: the job still runs and still
    returns verdicts, and only the report and the counterexample analyzer — which read
    ``.certora_sources`` — come up empty, much later. Confirmed by moving one project between the
    two paths with nothing else changed: seven ``.rs`` files collected from a plain path, zero from
    under ``.certora_internal``.

    Also pins the two places the directory's name has to appear, because forgetting either is its
    own quiet failure: uncopied, or a unit's workspace nests inside another's on the next run.
    """
    from composer.spec.cvlr.pipeline import WORK_DIR, _NOT_COPIED
    from composer.spec.cvlr.scaffold import GITIGNORE_LINES

    assert ".certora_internal" not in WORK_DIR.parts
    assert WORK_DIR.name in GITIGNORE_LINES
    assert _NOT_COPIED("anywhere", [WORK_DIR.name, "src"]) == {WORK_DIR.name}


def test_the_default_conf_enables_vacuity_checking():
    """A blocked author's escape route is to assume the conclusion, and only sanity catches it.

    Both halves of the publish gate pass such a rule: it VERIFIES, and it maps to its property. That
    is not an oversight in the gate — "accounted for, not all green" exists so a violated rule need
    not be smoothed away, and the same choice means the gate has no notion of rule *strength*.
    Vacuity checking is what supplies it, both public examples enable it, and this default had not.
    """
    from composer.spec.cvlr.conf import TEMPLATE_BASE, solana_conf, RunOverlay

    assert TEMPLATE_BASE["rule_sanity"] == "basic"
    # And it survives into a submission's conf rather than being dropped as run-owned.
    conf = solana_conf(dict(TEMPLATE_BASE), RunOverlay(build_script="/w/b.py"))
    assert conf["rule_sanity"] == "basic"


@pytest.mark.parametrize(
    "raw,expected",
    [
        # The one that actually happened, twice in one run.
        ("vault: Deposit & Balance Tracking (8 properties)",
         "vault: Deposit Balance Tracking (8 properties)"),
        ("Withdrawal & Fee Distribution", "Withdrawal Fee Distribution"),
        # Other prose a display name can carry. Non-ASCII is dropped rather than transliterated.
        ("A#B+C;D?E!F", "A B C D E F"),
        ("Café — naïve", "Caf na ve"),
        # Already safe: unchanged, punctuation and all.
        ("ok-name_1.2 [x]", "ok-name_1.2 [x]"),
    ],
)
def test_a_display_name_is_reduced_to_what_the_prover_accepts(raw, expected):
    """``msg`` is built from a component's display name, which is prose written by a model.

    ``certoraRun`` *raises* on a character outside its set, before reading a single rule, so an
    ampersand is enough to fail every submission a unit ever makes — and the author cannot fix it,
    because the name is not in the harness. Two units in one run spent 6 and 13+ submissions on
    exactly this, one of them holding a finished ten-rule harness with three claimed findings.
    """
    from composer.spec.cvlr.conf import safe_msg

    assert safe_msg(raw) == expected


def test_the_accepted_character_set_stays_a_subset_of_the_cli_s():
    """Ours must be a subset of ``certoraValidateFuncs.validate_msg``'s, not merely similar.

    A subset is what makes drift safe in the direction that matters: if the CLI ever *narrows* its
    set, a strict subset stays valid with no edit here. Being a superset by even one character —
    ``;`` was, on the first cut of this — reintroduces the whole failure for the inputs containing
    it, and the test that would catch it is this one rather than any round-trip.
    """
    import string

    from composer.spec.cvlr.conf import _MSG_SAFE

    cli_extra = {"(", " ", ",", "/", "[", "'", "-", '"', "_", "]", ".", ")", ":", "\\", "=", "*", "$"}
    cli = set(string.ascii_letters) | set(string.digits) | cli_extra
    assert _MSG_SAFE <= cli, f"not accepted by the CLI: {_MSG_SAFE - cli}"


def test_a_rule_declared_against_a_dependency_is_refused():
    """A rule can drive a *library* function, verify cleanly, and map onto a program property.

    Measured: an end-to-end run shipped
    ``cvlr_assume!(!accounts[1].is_signer); cvlr_assert!(Signer::try_from(..).is_err())`` against a
    property named "depositor must sign". It was GOOD, it was the only assertion in its unit, and it
    establishes a fact about ``anchor_lang`` rather than about the program. Neither the mapping gate
    nor the verdict can see that; the declaration can, because the author named the dependency
    honestly.

    The harness is a module inside the program's crate, so the program's own items are always
    reachable as ``crate::…``. That makes this a spelling rule rather than a guess about which crates
    count as "the program".
    """
    draft = "#[rule]\nfn rule_signer_rejects_non_signer() { }\n"
    subjects = [
        DrivesProgramFunction(
            rule="rule_signer_rejects_non_signer",
            function="anchor_lang::accounts::signer::Signer::try_from",
        )
    ]
    err = validate_rule_subjects(subjects, draft)
    assert err is not None
    assert "not in the program under verification" in err
    assert "anchor_lang" in err


def test_a_rule_declared_against_the_program_s_own_code_is_accepted_at_any_depth():
    """The check is about authorship, not depth: descending to the accounting core a handler wraps is
    the sanctioned move when a CPI blocks a handler-level property (``docs/upstream-defects.md`` P6),
    so it must pass."""
    draft = "#[rule]\nfn rule_core() { }\n"
    subjects = [
        DrivesProgramFunction(
            rule="rule_core",
            function="crate::vault_accounting::apply_withdrawal",
        )
    ]
    assert validate_rule_subjects(subjects, draft) is None


def test_a_mirror_must_also_name_program_code_as_what_it_stands_in_for():
    """A stand-in's whole claim is "this reproduces *that*". Pointing it at a dependency makes the
    claim unfalsifiable and the caveat meaningless."""
    draft = "#[rule]\nfn rule_mirror() { }\n"
    subjects = [
        DrivesHarnessMirror(
            rule="rule_mirror",
            mirrors="anchor_lang::accounts::account::Account::try_from",
            reason="the real one could not be analyzed",
        )
    ]
    err = validate_rule_subjects(subjects, draft)
    assert err is not None
    assert "not in the program under verification" in err


def _harness_with(subjects: list) -> GeneratedHarness:
    return GeneratedHarness(
        commentary="",
        harness="",
        property_rules=[],
        rule_subjects=subjects,
        skipped=[],
        summaries=[],
    )


def test_a_harness_with_no_rules_is_not_a_harness_that_verifies_itself():
    """The gate's reach predicate asked "does any rule drive the program", which answers *no* both
    for a harness of pure mirrors and for one that skipped every property with a reason. Those are
    opposite outcomes, and a run produced the second — seven skips naming the CPI havoc (P6), the
    summaries attempted, and the absent accounting core — and the gate failed the run for it.

    Pinned here rather than in ``test_cvlr_gate``, whose module-level marker makes every test in it
    an expensive live-prover run.
    """
    from tests.test_cvlr_gate import _verifies_only_its_own_code

    assert not _verifies_only_its_own_code(_harness_with([]))

    mirror_only = [
        DrivesHarnessMirror(
            rule="rule_mirror", mirrors="crate::vault_program::deposit", reason="[3308]"
        )
    ]
    assert _verifies_only_its_own_code(_harness_with(mirror_only))

    mixed = [*mirror_only, DrivesProgramFunction(
        rule="rule_real", function="crate::vault_program::withdraw"
    )]
    assert not _verifies_only_its_own_code(_harness_with(mixed))
