"""The CVLR author's gates, without an LLM.

Everything here is a property of the machinery around the agent rather than of the agent, which is
what makes it testable in the routine env — and the machinery is where the interesting failures are:

* **The publish gate's ground truth.** ``validate_property_rules`` checks the declared mapping
  against the rules a build listed, both directions. A property claiming a rule that was not
  listed is a hallucinated deliverable; a listed rule nobody claimed is prover time nobody chose
  to spend. The draft text below is an example of what an author writes. These tests pass the
  list in.
* **The shared module declaration.** ``specs/mod.rs`` names every unit, each behind its own cargo
  feature. A module behind a disabled ``cfg`` is never compiled, so a sibling that has not written
  its file yet does not fail this unit's gate.
* **What "accounted for" means.** The prover gate passes with a violated rule in it, provided the
  author marked it. Getting that wrong in either direction is bad: demanding green pushes the author
  to weaken rules until findings disappear, and accepting anything makes the gate decorative.
"""

from pathlib import Path

import asyncio
import contextlib
from types import SimpleNamespace

import pytest
from langgraph.store.memory import InMemoryStore

from composer.authoring.state import SkippedProperty, make_validation_stamper, spec_digest
from composer.prover.conf import SelectRules
from composer.spec.cvlr.conf import TunableConf, RunOverlay, solana_conf
from composer.spec.cvlr.prover import Submission as CvlrSubmission
from composer.spec.cvlr.harness import (
    CvlrArtifactStore,
    GeneratedHarness,
    HarnessModule,
)
from composer.spec.cvlr.state import (
    PROVER_VALIDATION_KEY,
    LastVerdicts,
    PropertyRuleMapping,
    RuleSubject,
    tuning_history,
    validate_property_rules,
    validate_rule_subjects,
)
from composer.spec.cvlr import author
from composer.spec.cvlr import verify as verify_mod
from composer.spec.cvlr.author_tools import unaccounted
from composer.spec.cvlr.verify import CexAnalysis, _externals_note, _unverdicted, _wrongly_expected
from composer.spec.source.cex_capture import CexAnalysisStore

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

#: The names a build's ``.cvlr_rules`` section listed for ``DRAFT``. Not parsed from it.
DECLARED = ("rule_solvency", "no_underflow_withdraw", "no_underflow_deposit")


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
        DECLARED,
    )
    assert err is None


def test_a_property_cannot_claim_a_rule_the_draft_does_not_declare():
    # The hallucination this gate exists for: a mapping that reads plausibly and names nothing real.
    err = validate_property_rules(
        _mapping(solvency_holds=["rule_solvency_v2"]), [], ["solvency_holds"], DECLARED
    )
    assert err is not None
    assert "rule_solvency_v2" in err


def test_a_declared_rule_must_be_tied_back_to_a_property():
    # The other direction. A rule nobody claimed still costs a prover run every iteration, and it is
    # how a harness accumulates work nobody asked for.
    err = validate_property_rules(
        _mapping(solvency_holds=["rule_solvency"]), [], ["solvency_holds"], DECLARED
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
        DECLARED,
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
        [RuleSubject(rule="rule_solvency", function="crate::vault_program::deposit")],
        DECLARED,
    )
    assert err is not None
    assert "no_underflow_deposit" in err and "no_underflow_withdraw" in err


def test_a_subject_cannot_name_a_rule_the_draft_does_not_declare():
    err = validate_rule_subjects(
        [RuleSubject(rule="rule_solvency_v2", function="crate::f")], DECLARED
    )
    assert err is not None and "rule_solvency_v2" in err and "did not list" in err


def test_a_mirror_is_accepted_but_must_say_what_it_stands_in_for():
    """The point is not to forbid the stand-in — today [3308] leaves no alternative for some
    handlers — but to make it a declaration rather than a doc comment."""
    subjects = [
        RuleSubject(rule="rule_solvency", function="crate::vault_program::deposit"),
        RuleSubject(
            rule="no_underflow_withdraw",
            function="crate::vault_program::withdraw",
            reason="[3308] on the #[error_code] path; summarizing and weakening both failed",
        ),
        RuleSubject(
            rule="no_underflow_deposit",
            function="crate::vault_program::deposit",
            reason="[3308] on the #[error_code] path; summarizing and weakening both failed",
        ),
    ]
    assert validate_rule_subjects(subjects, DECLARED) is None


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
        _DRAFT_NO_RULES, state["skipped"], tuning_history(state)
    )
    assert stamped.update["last_verdicts"].rules == ()


def test_a_draft_with_no_rules_and_no_skips_is_still_unfinished():
    """The other half, and the reason stamping the first case is safe rather than a hole: an empty
    draft with nothing declared and nothing skipped has said nothing at all."""
    tool = verify_mod.VerifyRules(state=_verify_state(_DRAFT_NO_RULES), tool_call_id="tc")
    answer = tool._nothing_to_submit()
    assert isinstance(answer, str) and "nothing to submit" in answer


def test_a_mirror_declaration_reaches_the_published_artifact():
    """A verdict on a stand-in is not a verdict on the program. The subject stays on the published
    harness, with the reason that says so."""
    mirror = RuleSubject(
        rule="rule_solvency", function="crate::vault_program::deposit", reason="[3308]"
    )
    published = GeneratedHarness(commentary="c", harness=DRAFT, rule_subjects=[mirror])
    assert published.rule_subjects == [mirror]
    assert published.rule_subjects[0].reason == "[3308]"


def test_a_skipped_property_may_be_absent_but_not_mapped():
    skipped = [SkippedProperty(property_title="forgotten_one", reason="no token model exists")]
    titles = ["solvency_holds", "withdraw_never_underflows", "forgotten_one"]
    full = _mapping(
        solvency_holds=["rule_solvency"],
        withdraw_never_underflows=["no_underflow_withdraw", "no_underflow_deposit"],
    )
    assert validate_property_rules(full, skipped, titles, DECLARED) is None
    # Claiming a rule for a property you also declared unformalizable is a contradiction.
    both = full + _mapping(forgotten_one=["rule_solvency"])
    assert validate_property_rules(both, skipped, titles, DECLARED) is not None


def test_every_unit_is_declared_before_any_unit_authors(tmp_path):
    # Each module is behind its own cargo feature. A module behind a disabled cfg is never
    # compiled, so a sibling that has not written its file yet does not fail this unit's build.
    store = CvlrArtifactStore(tmp_path, Path("programs/prog"))
    modules = [HarnessModule("deposit"), HarnessModule("withdraw")]
    mod_rs, *_ = store.declare_modules(modules)

    text = mod_rs.read_text()
    assert "pub mod deposit;" in text and "pub mod withdraw;" in text
    for module in modules:
        assert not (mod_rs.parent / module.artifact_file).exists()


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
    assert unaccounted(status, expected) == ["rule_c"]


def test_a_declared_rule_the_report_gives_no_verdict_for_blocks_the_gate():
    """The roll-up sees only the rules the report names. A declared rule missing from it would
    otherwise leave the draft stamped as if every rule had been checked."""
    status = {"rule_a": True}
    assert _unverdicted(["rule_a", "rule_b"], status) == ["rule_b"]
    assert unaccounted(status, {}) == []


def test_the_author_is_told_which_functions_were_external_and_what_that_means():
    """Not a gate: the starting configuration keeps most platform code external on purpose. But a
    verdict over one of these is conditional on dropped writes, and the author cannot see that
    from the verdict."""
    assert _externals_note(()) is None
    note = _externals_note(("solana_account_info::AccountInfo::resize",))
    assert note is not None
    assert "solana_account_info::AccountInfo::resize" in note
    assert "writes nothing else" in note and "cannot add inlining directives" in note


def test_the_author_is_shown_how_to_bound_an_external_value_with_edits_it_can_ask_for():
    """Without this the note's only remedies were commentary and skips, so a property over the stake
    program's activation math, which an author can bound with two edits, went unchecked."""
    note = _externals_note(("solana_stake_interface::state::Delegation::stake_activating_and_deactivating",))
    assert note is not None
    assert "an extraction" in note and "`mock_fn`" in note and "`rule_subjects`" in note


def test_a_rule_marked_expected_to_fail_that_verifies_is_reported():
    # The more interesting direction: either the defect is not there or the rule does not test for
    # it, and both want the author's attention before the run is called finished.
    status = {"rule_b": True}
    assert _wrongly_expected(status, {"rule_b": "should underflow"}) == ["rule_b"]


def test_the_published_result_carries_the_harness_and_the_property_map():
    harness = GeneratedHarness(
        commentary="ok",
        harness=DRAFT,
        property_rules=_mapping(solvency_holds=["rule_solvency"]),
        final_link="https://prover.certora.com/output/1/abc",
    )
    assert harness.artifact_text == DRAFT
    assert harness.output_link == "https://prover.certora.com/output/1/abc"
    assert harness.property_checks() == [("solvency_holds", ["rule_solvency"])]


# ---------------------------------------------------------------------------
# The prover submission names the rules the build listed
# ---------------------------------------------------------------------------

class _StopProbe(Exception):
    """Cuts the tool off once the submission is captured; no prover is involved."""


async def _stubbed_stage(draft, summaries=(), overlay={}) -> None:
    return None


def _stubbed_slot() -> contextlib.nullcontext[SimpleNamespace]:
    return contextlib.nullcontext(SimpleNamespace(stage=_stubbed_stage))


def _verify_state(draft: str) -> dict:
    return {
        "messages": [],
        "curr_spec": draft,
        "expected_failures": {},
        "skipped": [],
        "property_rules": [],
        "rule_subjects": [],
        "summaries": [],
        "vfs": {},
        "version_history": [],
        "prover_settings": TunableConf(),
        "required_validations": [],
        "validations": {},
        "failed": None,
        "budget_curtailed": False,
    }


def _built_so(path: Path, payload: bytes):
    """A stand-in chain build whose artifact is ``path``."""
    from composer.cargo.sbf import Built, SbfRun

    path.write_bytes(payload)

    async def build(_session, _submission):
        return SbfRun(
            duration_ms=1,
            verdict=Built(manifest=SimpleNamespace(artifact=path)),  # type: ignore[arg-type]
            confined=True,
        )

    return build


def _unbound_analysis() -> CexAnalysis:
    """Counterexample analysis whose model was never bound.

    ``verify_rules`` builds the handler only once a conf is about to be
    submitted. These runs stop before that. An unbound model fails if a
    handler is built any earlier.
    """
    return CexAnalysis(store=CexAnalysisStore(store=InMemoryStore(), namespace=("t",)))


@pytest.mark.asyncio
async def test_the_submission_names_the_rules_the_build_listed(tmp_path, monkeypatch):
    """The conf's ``rule`` entry is the names in the ``.so``, not names parsed from the draft.

    A regression guard on a real outage, not a hypothetical. This line was once
    ``rules=deps.submission.rules`` — a self-assignment — so every submission inherited from a base
    conf that names no rules. A cloud job with no rule selection ends in FAILED with no report and
    nothing on disk, so the authoring loop saw only "status FAILED" and simplified its harness
    until it was a tautology, which failed identically.

    The draft below declares a name the ELF does not. A reader that scanned the source would submit
    that name. The section lists the two rules the conf must name.
    """

    from tests.test_cvlr_manifest import elf_with_cvlr_rules, record

    so = tmp_path / "prog.so"
    payload = (
        record(0, b"CV1")
        + record(1, b"rule_only_authority_withdraws")
        + record(1, b"rule_balance_conserved")
    )
    captured: dict = {}

    async def write(_session, submission, *, into):
        captured["rules"] = submission.rules
        captured["settings"] = submission.settings
        raise _StopProbe

    async def run(*_args, **_kwargs):
        pytest.fail("write_submission is where this test stops")

    monkeypatch.setattr(verify_mod, "build_for_submission", _built_so(so, elf_with_cvlr_rules(payload)))
    monkeypatch.setattr(verify_mod, "write_submission", write)
    monkeypatch.setattr(verify_mod, "run_submission", run)

    deps = verify_mod.VerifyDeps(
        target=SimpleNamespace(  # type: ignore[arg-type]
            session=None,
            build_slot=_stubbed_slot,
        ),
        # What the run started from. The state below carries different settings, which is what a
        # settings edit would have produced and what the submission must actually use.
        submission=CvlrSubmission(manifest_path=Path("/w/Cargo.toml"), stem="unit", msg="m"),
        submission_dir=Path("/w/submissions"),
        prover_opts=None,  # type: ignore[arg-type]
        analysis=_unbound_analysis(),
        stamper=None,  # type: ignore[arg-type]
    )
    token = verify_mod.VerifyRules._dep_ctx.set(deps)
    try:
        # A decoy. The ELF does not list it, so a source scan would name the wrong rule.
        state = {
            **_verify_state("#[rule]\nfn rule_decoy_not_in_the_elf() {}\n"),
            "prover_settings": TunableConf(loop_iter=5),
        }
        tool = verify_mod.VerifyRules(state=state, tool_call_id="tc")
        with pytest.raises(_StopProbe):
            await tool.run()
    finally:
        verify_mod.VerifyRules._dep_ctx.reset(token)

    assert captured["rules"] == SelectRules(
        ("rule_balance_conserved", "rule_only_authority_withdraws")
    )
    # The settings come from state, not from the deps the run was constructed with: they are the
    # author's to change, and a submission built from the starting copy would send the old settings
    # while `version_history` had already recorded the new ones.
    assert captured["settings"] == TunableConf(loop_iter=5)
    # And the selection actually reaches the conf as a `rule` entry.
    conf = solana_conf(
        TunableConf(), RunOverlay(build_script=Path("/w/b.py"), rules=captured["rules"])
    )
    assert conf["rule"] == ["rule_balance_conserved", "rule_only_authority_withdraws"]


@pytest.mark.asyncio
async def test_a_missing_rule_section_writes_no_conf(tmp_path, monkeypatch):
    from tests.test_cvlr_manifest import elf_without_cvlr_rules

    so = tmp_path / "prog.so"

    async def write(*_args, **_kwargs):
        pytest.fail("a missing section must not write a conf")

    monkeypatch.setattr(verify_mod, "build_for_submission", _built_so(so, elf_without_cvlr_rules()))
    monkeypatch.setattr(verify_mod, "write_submission", write)

    deps = SimpleNamespace(
        target=SimpleNamespace(session=None, build_slot=_stubbed_slot),
        submission=CvlrSubmission(manifest_path=Path("/w/Cargo.toml"), stem="unit", msg="m"),
        submission_dir=Path("/w/submissions"),
        prover_opts=None,
        analysis=_unbound_analysis(),
        stamper=None,
    )
    token = verify_mod.VerifyRules._dep_ctx.set(deps)
    try:
        answer = await verify_mod.VerifyRules(
            state=_verify_state("#[rule]\nfn rule_decoy() {}\n"), tool_call_id="tc"
        ).run()
    finally:
        verify_mod.VerifyRules._dep_ctx.reset(token)
    assert isinstance(answer, str)
    assert ".cvlr_rules" in answer and "Nothing was submitted" in answer


@pytest.mark.asyncio
async def test_a_corrupt_rule_section_writes_no_conf(tmp_path, monkeypatch, caplog):
    import logging

    from tests.test_cvlr_manifest import elf_with_cvlr_rules, record

    so = tmp_path / "prog.so"

    async def write(*_args, **_kwargs):
        pytest.fail("a corrupt section must not write a conf")

    monkeypatch.setattr(
        verify_mod,
        "build_for_submission",
        _built_so(so, elf_with_cvlr_rules(record(2, b"SECRET_SECTION_BYTES"))),
    )
    monkeypatch.setattr(verify_mod, "write_submission", write)

    deps = SimpleNamespace(
        target=SimpleNamespace(session=None, build_slot=_stubbed_slot),
        submission=CvlrSubmission(manifest_path=Path("/w/Cargo.toml"), stem="unit", msg="m"),
        submission_dir=Path("/w/submissions"),
        prover_opts=None,
        analysis=_unbound_analysis(),
        stamper=None,
    )
    token = verify_mod.VerifyRules._dep_ctx.set(deps)
    try:
        with caplog.at_level(logging.WARNING):
            answer = await verify_mod.VerifyRules(
                state=_verify_state("fn helper() {}"), tool_call_id="tc"
            ).run()
    finally:
        verify_mod.VerifyRules._dep_ctx.reset(token)
    assert isinstance(answer, str) and "offset 0" in answer
    assert str(so) in caplog.text and "offset 0" in caplog.text
    assert "SECRET_SECTION_BYTES" not in caplog.text


@pytest.mark.asyncio
async def test_an_empty_rule_section_submits_nothing(tmp_path, monkeypatch):
    from tests.test_cvlr_manifest import elf_with_cvlr_rules

    so = tmp_path / "prog.so"

    async def write(*_args, **_kwargs):
        pytest.fail("an empty listing must not write a conf")

    monkeypatch.setattr(verify_mod, "build_for_submission", _built_so(so, elf_with_cvlr_rules(b"")))
    monkeypatch.setattr(verify_mod, "write_submission", write)

    deps = SimpleNamespace(
        target=SimpleNamespace(session=None, build_slot=_stubbed_slot),
        submission=CvlrSubmission(manifest_path=Path("/w/Cargo.toml"), stem="unit", msg="m"),
        submission_dir=Path("/w/submissions"),
        prover_opts=None,
        analysis=_unbound_analysis(),
        stamper=None,
    )
    token = verify_mod.VerifyRules._dep_ctx.set(deps)
    try:
        answer = await verify_mod.VerifyRules(
            state=_verify_state("fn helper() {}"), tool_call_id="tc"
        ).run()
    finally:
        verify_mod.VerifyRules._dep_ctx.reset(token)
    assert isinstance(answer, str) and "nothing to submit" in answer


def _stamped(draft: str, *, rules: tuple[str, ...] | None, store: bool) -> dict:
    """A draft whose prover stamp matches, with or without a stored rule list."""
    state = _verify_state(draft)
    state["required_validations"] = [PROVER_VALIDATION_KEY]
    stamp = make_validation_stamper(PROVER_VALIDATION_KEY)(state, tuning_history(state))
    state["validations"] = stamp
    if store:
        state["last_verdicts"] = LastVerdicts(
            status={}, incomplete=[], unverdicted=[], stamp=stamp, rules=rules
        )
    return state


async def _publish(state: dict, rules: list[str], subjects: list[RuleSubject]):
    tool = author.PublishResultTool(
        state=state,  # type: ignore[arg-type]
        tool_call_id="tc",
        commentary="the account",
        property_rules=_mapping(solvency_holds=rules),
        rule_subjects=subjects,
    )
    token = author.PublishResultTool._dep_ctx.set(["solvency_holds"])
    try:
        return await tool.run()
    finally:
        author.PublishResultTool._dep_ctx.reset(token)


@pytest.mark.asyncio
async def test_publish_checks_the_stored_listing_and_not_the_draft():
    """The draft's `#[rule]` name is a decoy. The stored list is what the mapping is checked against."""
    state = _stamped(
        "#[rule]\nfn rule_decoy_not_in_the_elf() {}\n",
        rules=("rule_solvency",),
        store=True,
    )
    out = await _publish(
        state,
        ["rule_solvency"],
        [RuleSubject(rule="rule_solvency", function="crate::vault::deposit")],
    )
    assert not isinstance(out, str)
    assert out.update["result"] == "the account"

    refused = await _publish(
        state,
        ["rule_decoy_not_in_the_elf"],
        [RuleSubject(rule="rule_decoy_not_in_the_elf", function="crate::vault::deposit")],
    )
    assert isinstance(refused, str) and "rule_decoy_not_in_the_elf" in refused


@pytest.mark.asyncio
@pytest.mark.parametrize("store", [False, True])
async def test_publish_refuses_a_draft_whose_rules_were_not_listed(store):
    """No ``last_verdicts``, and a verdict record whose ``rules`` is still ``None``, both fail closed."""
    state = _stamped("#[rule]\nfn rule_solvency() {}\n", rules=None, store=store)
    answer = await _publish(
        state,
        ["rule_solvency"],
        [RuleSubject(rule="rule_solvency", function="crate::vault::deposit")],
    )
    assert isinstance(answer, str) and "have not been listed" in answer


@pytest.mark.asyncio
async def test_the_run_work_directory_is_not_inside_a_prover_internal_directory():
    """Where the run's workspace lives changes what the prover receives.

    The collector skips paths under ``.certora_internal``, so a workspace placed there uploads its
    ``.so`` and tuning files and none of its Rust. Nothing reports it: the job still runs and still
    returns verdicts, and only the report and the counterexample analyzer — which read
    ``.certora_sources`` — come up empty, much later. Confirmed by moving one project between the
    two paths with nothing else changed: seven ``.rs`` files collected from a plain path, zero from
    under ``.certora_internal``.
    """
    from composer.spec.cvlr.pipeline import run_work_directory

    async with run_work_directory() as work:
        assert work.is_dir()
        assert ".certora_internal" not in work.parts
    assert not work.exists()


def test_the_default_conf_enables_vacuity_checking():
    """A blocked author's escape route is to assume the conclusion, and only sanity catches it.

    Both halves of the publish gate pass such a rule: it VERIFIES, and it maps to its property. That
    is not an oversight in the gate — "accounted for, not all green" exists so a violated rule need
    not be smoothed away, and the same choice means the gate has no notion of rule *strength*.
    Vacuity checking is what supplies it, both public examples enable it, and this default had not.
    """
    conf = solana_conf(TunableConf(), RunOverlay(build_script=Path("/w/b.py")))
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
    from composer.prover.conf import safe_msg

    assert safe_msg(raw) == expected


def test_the_accepted_character_set_stays_a_subset_of_the_cli_s():
    """Ours must be a subset of ``certoraValidateFuncs.validate_msg``'s, not merely similar.

    A subset is what makes drift safe in the direction that matters: if the CLI ever *narrows* its
    set, a strict subset stays valid with no edit here. Being a superset by even one character —
    ``;`` was, on the first cut of this — reintroduces the whole failure for the inputs containing
    it, and the test that would catch it is this one rather than any round-trip.
    """
    import string

    from composer.prover.conf import _MSG_SAFE

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
    subjects = [
        RuleSubject(
            rule="rule_signer_rejects_non_signer",
            function="anchor_lang::accounts::signer::Signer::try_from",
        )
    ]
    err = validate_rule_subjects(subjects, ["rule_signer_rejects_non_signer"])
    assert err is not None
    assert "not in the program under verification" in err
    assert "anchor_lang" in err


def test_a_rule_declared_against_the_program_s_own_code_is_accepted_at_any_depth():
    """The check is about authorship, not depth: descending to the accounting core a handler wraps is
    the sanctioned move when a CPI blocks a handler-level property (``docs/upstream-defects.md`` P6),
    so it must pass."""
    subjects = [
        RuleSubject(
            rule="rule_core",
            function="crate::vault_accounting::apply_withdrawal",
        )
    ]
    assert validate_rule_subjects(subjects, ["rule_core"]) is None


def test_a_mirror_must_also_name_program_code_as_what_it_stands_in_for():
    """A stand-in's whole claim is "this reproduces *that*". Pointing it at a dependency makes the
    claim unfalsifiable and the caveat meaningless."""
    subjects = [
        RuleSubject(
            rule="rule_mirror",
            function="anchor_lang::accounts::account::Account::try_from",
            reason="the real one could not be analyzed",
        )
    ]
    err = validate_rule_subjects(subjects, ["rule_mirror"])
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
        RuleSubject(
            rule="rule_mirror", function="crate::vault_program::deposit", reason="[3308]"
        )
    ]
    assert _verifies_only_its_own_code(_harness_with(mirror_only))

    mixed = [*mirror_only, RuleSubject(
        rule="rule_real", function="crate::vault_program::withdraw"
    )]
    assert not _verifies_only_its_own_code(_harness_with(mixed))


# ---------------------------------------------------------------------------------------------
# carrying a draft between runs


class _FakeGraph:
    """Just enough of the compiled graph for ``_remember_attempt``: a last checkpoint."""

    def __init__(self, values: dict[str, object] | None = None, raises: bool = False):
        self._values = values or {}
        self._raises = raises

    async def aget_state(self, _config: object) -> object:
        if self._raises:
            raise RuntimeError("checkpointer is gone")
        return SimpleNamespace(values=self._values)


class _RecordingCtx:
    """``ctx.child(KEY).cache_{get,put}(...)`` without a store behind it."""

    def __init__(self, held: object = None):
        self.put: object = None
        self._held = held

    def child(self, _key: object) -> "_RecordingCtx":
        return self

    async def cache_get(self, _ty: object) -> object:
        return self._held

    async def cache_put(self, value: object) -> None:
        self.put = value


def test_a_units_draft_and_its_edits_are_carried_to_the_next_run():
    """The expensive half is authoring, and a budget-cut unit used to lose all of it: measured on
    the stake benchmark as thousands of lines re-written at full price. The edits travel as their
    edit-store ids, as EVM's do: every id in the store was reviewed and compiled before it got
    there, and the next run restores the working copy from the last one."""
    ctx = _RecordingCtx()
    graph = _FakeGraph({
        "curr_spec": "#[rule]\npub fn rule_x() {}",
        "version_history": ["edit-1", "edit-2"],
    })

    asyncio.run(author._remember_attempt(ctx, graph, "tid"))  # type: ignore[arg-type]

    assert ctx.put is not None
    assert ctx.put.spec == "#[rule]\npub fn rule_x() {}"
    assert ctx.put.version_history == ["edit-1", "edit-2"]


def test_a_unit_that_authored_nothing_caches_nothing():
    # An empty draft would seed the next run with "here is your last draft" and then nothing,
    # which is worse than starting clean.
    ctx = _RecordingCtx()
    asyncio.run(author._remember_attempt(ctx, _FakeGraph({"curr_spec": ""}), "tid"))  # type: ignore[arg-type]
    assert ctx.put is None


def test_losing_the_cache_never_loses_the_error_that_caused_it():
    """This runs in a `finally`, often while a BudgetExceeded is already unwinding. A raise here
    would replace the real failure with a bookkeeping one."""
    ctx = _RecordingCtx()
    asyncio.run(author._remember_attempt(ctx, _FakeGraph(raises=True), "tid"))  # type: ignore[arg-type]
    assert ctx.put is None


def test_a_curtailed_unit_does_not_overwrite_a_fuller_draft():
    """The wrap-up order has a curtailed unit delete every rule it cannot stand behind, so its last
    draft is often a fraction of what it held. Writing that over an earlier run's fuller draft loses
    work nobody reconsidered — measured on the stake benchmark as 6,176 cached lines replaced by
    1,609."""
    held = author.LastCvlrAttempt(spec="#[rule]\n" * 40)
    ctx = _RecordingCtx(held=held)
    graph = _FakeGraph({"curr_spec": "#[rule]\npub fn rule_survivor() {}", "budget_curtailed": True})

    asyncio.run(author._remember_attempt(ctx, graph, "tid"))  # type: ignore[arg-type]

    assert ctx.put is None


def test_a_curtailed_unit_still_seeds_an_empty_slot():
    # The case the cache exists for: a run cut before it ever cached anything. Nothing can regress,
    # so the guard must not turn into "a curtailed unit never caches".
    ctx = _RecordingCtx(held=None)
    graph = _FakeGraph({"curr_spec": "#[rule]\npub fn rule_x() {}", "budget_curtailed": True})

    asyncio.run(author._remember_attempt(ctx, graph, "tid"))  # type: ignore[arg-type]

    assert ctx.put is not None and ctx.put.spec == "#[rule]\npub fn rule_x() {}"


def test_a_unit_that_finished_on_its_own_terms_still_overwrites():
    # Not curtailed: the last draft is the considered one even when it is smaller, and CVL's
    # unconditional overwrite is the behaviour to keep here.
    held = author.LastCvlrAttempt(spec="#[rule]\n" * 40)
    ctx = _RecordingCtx(held=held)
    graph = _FakeGraph({"curr_spec": "#[rule]\npub fn rule_considered() {}"})

    asyncio.run(author._remember_attempt(ctx, graph, "tid"))  # type: ignore[arg-type]

    assert ctx.put is not None and ctx.put.spec == "#[rule]\npub fn rule_considered() {}"
