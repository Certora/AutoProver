"""The Rust authoring session's publish gate (``composer.rustapp.session``).

Publishing is gated on *stamps over the buffer as it now stands*, not on a Python retry loop. Three
things follow, and each is a rule this file pins down:

* a checker run stamps the draft it saw, so any later edit silently invalidates it — the gate
  refuses rather than publishing something nothing has checked;
* a check that failed blocks publishing unless the author marked it expected-to-fail with a reason,
  which is how a real counterexample reaches the report as a finding rather than as noise;
* the declared property→checks mapping is checked against the checks the stamping run actually
  covered, in both directions — the author names the checks, so the run is what holds those names
  to the artifact.
"""

from typing import Any, cast

import pytest

from composer.authoring.state import SkippedProperty, check_completion, spec_digest
from composer.rustapp.session import (
    FEEDBACK_KEY, VALIDATE_KEY, PropertyCheckMapping, RustSessionState, _unexplained,
    _verdict_report, declared_checks, declared_names, targets_of,
)
from composer.rustapp.wire import Check, Outcome, Verdict


DRAFT = "fn c_no_free_mint(f: &mut Fixture) {}"


def _state(**kw) -> RustSessionState:
    """The session state the gate reads (messages are irrelevant to it)."""
    base = {
        "curr_spec": DRAFT,
        "skipped": [],
        "validations": {},
        "required_validations": [VALIDATE_KEY],
        "property_checks": [],
        "expected_failures": {},
        "verdicts": {},
        "ran": [],
        "failed": None,
    }
    return cast(RustSessionState, {**base, **kw})


def _check(name: str, target: str | None = None, properties: list[str] | None = None) -> Check:
    return Check(name=name, properties=properties or ["p"], target=target)


def _mapped(**by_title: list[str]) -> list[PropertyCheckMapping]:
    return [PropertyCheckMapping(property_title=t, checks=c) for t, c in by_title.items()]


def _verdict(outcome: Outcome, detail: str | None = None) -> Verdict:
    return Verdict(outcome=outcome, line=None, duration_seconds=None, unit_file=None,
                   detail=detail, accounting=None)


# ---------------------------------------------------------------------------
# Stamps go stale
# ---------------------------------------------------------------------------

def test_a_clean_run_satisfies_the_gate_for_the_draft_it_saw():
    stamped = _state(validations={VALIDATE_KEY: spec_digest(DRAFT, [])})
    assert check_completion(stamped) is None


def test_editing_the_spec_after_a_clean_run_invalidates_it():
    # The whole reason the stamp is a digest: nothing has to remember to clear it.
    stamped = _state(
        curr_spec=DRAFT + "\n// one more line",
        validations={VALIDATE_KEY: spec_digest(DRAFT, [])},
    )
    assert "stale" in (check_completion(stamped) or "")


def test_declaring_a_skip_after_a_clean_run_also_invalidates_it():
    # A skip is part of what was reviewed — "this property is left out, here is why" is a claim the
    # gate must not carry over from a draft that didn't make it.
    stamped = _state(
        skipped=[SkippedProperty(property_title="no_free_mint", reason="no oracle")],
        validations={VALIDATE_KEY: spec_digest(DRAFT, [])},
    )
    assert "stale" in (check_completion(stamped) or "")


def test_every_required_check_must_have_stamped_the_current_draft():
    # A wheel that declares a judge requires both; a validate stamp alone is not enough.
    both = _state(
        required_validations=[VALIDATE_KEY, FEEDBACK_KEY],
        validations={VALIDATE_KEY: spec_digest(DRAFT, [])},
    )
    assert FEEDBACK_KEY in (check_completion(both) or "")


def test_nothing_written_is_reported_as_such_rather_than_as_a_stale_stamp():
    assert "no spec written yet" in (check_completion(_state(curr_spec=None)) or "")


# ---------------------------------------------------------------------------
# A failing check blocks, unless it is the finding
# ---------------------------------------------------------------------------

def test_a_failing_check_blocks_the_gate():
    verdicts = {"c_no_free_mint": _verdict(Outcome.BAD, "counterexample: mint(0)")}
    assert _unexplained(verdicts, {}) == {"c_no_free_mint"}


def test_a_check_marked_expected_to_fail_does_not_block():
    # The counterexample IS the finding. Marking it is what turns a blocked run into a reported one,
    # and the reason is what a human reads next to it.
    verdicts = {"c_no_free_mint": _verdict(Outcome.BAD, "counterexample: mint(0)")}
    assert _unexplained(verdicts, {"c_no_free_mint": "the program really does allow this"}) == set()


def test_every_non_good_outcome_blocks_not_just_a_refutation():
    # An ERROR or a TIMEOUT is not a passing check; treating only BAD as a failure would publish a
    # check nothing actually decided.
    for outcome in (Outcome.ERROR, Outcome.TIMEOUT, Outcome.UNKNOWN):
        assert _unexplained({"c_x": _verdict(outcome)}, {}) == {"c_x"}


def test_the_report_says_which_checks_were_expected_to_fail():
    report = _verdict_report(
        {"c_a": _verdict(Outcome.GOOD), "c_b": _verdict(Outcome.BAD, "cex")},
        {"c_b": "known bug"},
    )
    assert "c_a: GOOD" in report
    assert "(expected to fail)" in report and "cex" in report


# ---------------------------------------------------------------------------
# What a session is still expected to check
# ---------------------------------------------------------------------------

def test_the_declared_names_are_what_runs_deduplicated():
    # One check discharging three properties is one thing to run, not three. The mapping is
    # many-to-many; the work list is its distinct names.
    mapping = _mapped(a=["c_shared"], b=["c_shared", "c_b"], c=["c_shared"])
    assert declared_names(mapping) == ["c_shared", "c_b"]


def test_a_declared_check_is_paired_with_the_grouping_the_wheel_gives_it():
    # The two halves of a check come from the two parties that can know them: the name from the
    # author, the invocation it runs under from the wheel.
    class _Wheel:
        def target_for(self, _input_json: str, check: str) -> str | None:
            return "shared" if check != "c_own" else None

    checks = declared_checks(cast(Any, _Wheel()), "{}", _mapped(a=["c_a"], b=["c_own"]))
    assert [(c.name, c.target) for c in checks] == [("c_a", "shared"), ("c_own", None)]
    # …and each carries the author's claim about it, so a backend whose diagnostics speak in
    # properties can place a finding without the host parsing anything.
    assert [c.properties for c in checks] == [["a"], ["b"]]


def test_a_checks_target_defaults_to_its_own_name():
    assert [t.name for t in targets_of([_check("c_p")])] == ["c_p"]


def test_checks_sharing_a_target_are_run_once_and_carry_their_own():
    # A wheel may check a whole property set in one run; the host groups, so the wheel does not have
    # to re-derive the grouping it was already given.
    checks = [_check("c_a", "shared"), _check("c_b", "shared"), _check("c_c")]
    targets = targets_of(checks)
    assert [t.name for t in targets] == ["shared", "c_c"]
    assert [c.name for c in targets[0].checks] == ["c_a", "c_b"]


def test_a_check_the_run_never_covered_cannot_be_claimed():
    # The author names the checks, so nothing but the run can say a name is real. A name that no
    # target covered is one the wheel never answered for.
    from composer.authoring.state import validate_check_mapping
    from composer.rustapp.session import _MAPPING

    err = validate_check_mapping(
        [(m.property_title, m.checks) for m in _mapped(no_free_mint=["c_invented"])],
        [], ["no_free_mint"], _MAPPING, ran=["c_no_free_mint"],
    )
    assert err is not None and "c_invented" in err


def test_one_check_may_be_claimed_by_several_properties():
    # A single rule discharging three related invariants: three report rows, one thing that ran.
    from composer.authoring.state import validate_check_mapping
    from composer.rustapp.session import _MAPPING

    err = validate_check_mapping(
        [(m.property_title, m.checks) for m in _mapped(a=["c_all"], b=["c_all"], c=["c_all"])],
        [], ["a", "b", "c"], _MAPPING, ran=["c_all"],
    )
    assert err is None


def test_a_property_that_is_neither_skipped_nor_mapped_is_refused():
    from composer.authoring.state import validate_check_mapping
    from composer.rustapp.session import _MAPPING

    err = validate_check_mapping(
        [], [], ["no_free_mint"], _MAPPING, ran=["c_no_free_mint"],
    )
    assert err is not None and "neither skipped nor mapped" in err


@pytest.mark.parametrize("outcome", [Outcome.GOOD, Outcome.BAD])
def test_verdicts_are_recorded_verbatim(outcome: Outcome):
    # Attribution is the wheel's — it owns its result format, so it decides which check a
    # counterexample belongs to and the host records the answer without reinterpreting it.
    v = _verdict(outcome, "as the wheel said it")
    assert v.outcome is outcome and v.detail == "as the wheel said it"
