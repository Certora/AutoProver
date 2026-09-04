"""What the feedback judge is shown beyond the draft, and why the draft is not enough.

``docs/cvlr-backend-plan.md`` §7.6. The judge's system prompt has always told it to weigh the
author's summaries — *"unlike a mirror it leaves no trace in the harness source, only a
justification you have to weigh"* — and the judge was never given one. ``input_parts`` builds its
input from the draft, the declared rules, the skips and the rebuttals, and a summary appears in none
of those. Munges made the same gap worse in kind rather than in degree: the judge now reviews a
harness while the program underneath it has been edited, and nothing said so.

The instruments are per-invocation state, so the fix is the judge's ``input_lift`` — the one
callback that sees the caller's context. These tests pin the two halves separately: what
:class:`HarnessAssumptions` says, and that lifting it *adds* to the judge's input rather than
replacing what ``input_parts`` built.

No cargo, no network, no prover, no model.
"""

from composer.authoring.judge import JudgeInput
from composer.spec.cvlr.author import with_assumptions
from composer.spec.cvlr.munge import EarlyPanic, FunctionMunge, MockFn
from composer.spec.cvlr.state import HarnessAssumptions, harness_assumptions
from composer.spec.cvlr.tuning import SummaryDirective

_DISPLAY = SummaryDirective(
    pattern="^<vault::VaultError as core::fmt::Display>::fmt$",
    why="[3308] on the #[error_code] Display impl; no property asserts over an error message",
)
_PANIC = FunctionMunge(
    path="programs/vault/src/lib.rs",
    function="apply_withdrawal",
    kind=EarlyPanic(),
    why="[3308] on the checked_sub error construction; no property here is about rejection",
)


def _text(assumptions: HarnessAssumptions) -> str:
    return "\n".join(assumptions.briefing())


# ---------------------------------------------------------------------------------------------
# what the briefing says


def test_a_summary_reaches_the_judge_with_the_argument_for_it():
    """The pattern alone would let the judge see *that* something was summarized. Whether it was
    sound is an argument, and the `why` is the only place that argument exists."""
    briefing = _text(HarnessAssumptions(summaries=(_DISPLAY,), munges=()))
    assert _DISPLAY.pattern in briefing
    assert _DISPLAY.why in briefing


def test_an_unconstrained_return_is_said_rather_than_left_blank():
    """`returns=None` is the common case and it is the *worse* one — the prover assumes anything
    about the value. Rendering it as an empty field reads like a detail nobody filled in."""
    assert "unconstrained" in _text(HarnessAssumptions(summaries=(_DISPLAY,), munges=()))


def test_a_constrained_return_is_shown_instead():
    shaped = SummaryDirective(pattern="^f$", why="w", returns="(*i32)(r1+0):num")
    briefing = _text(HarnessAssumptions(summaries=(shaped,), munges=()))
    assert "(*i32)(r1+0):num" in briefing
    assert "unconstrained" not in briefing


def test_a_munge_reaches_the_judge_naming_the_function_and_what_changed():
    """Which function, in which file, and what the attribute actually did to it. The last is the
    part the judge cannot look up: `early_panic` is a proc macro, so the effect is not visible in
    the source even for a judge that goes and reads the munged file."""
    briefing = _text(HarnessAssumptions(summaries=(), munges=(_PANIC,)))
    assert "apply_withdrawal" in briefing
    assert "programs/vault/src/lib.rs" in briefing
    assert EarlyPanic().describe() in briefing
    assert _PANIC.why in briefing


def test_the_stand_in_is_named_when_a_function_was_mocked():
    """A mock is only reviewable if the judge knows whose code replaced the program's — which is
    the same question section 1 asks about rules, one level down."""
    mock = FunctionMunge(
        path="programs/vault/src/lib.rs",
        function="fee_of",
        kind=MockFn(stand_in="crate::certora::specs::withdraw::flat_fee"),
        why="the fee curve is not what this property is about",
    )
    assert "crate::certora::specs::withdraw::flat_fee" in _text(
        HarnessAssumptions(summaries=(), munges=(mock,))
    )


def test_both_kinds_appear_when_both_were_used():
    briefing = _text(HarnessAssumptions(summaries=(_DISPLAY,), munges=(_PANIC,)))
    assert _DISPLAY.pattern in briefing and "apply_withdrawal" in briefing


def test_having_used_neither_is_stated_rather_than_left_silent():
    """The case that has to be carried and looks like it does not. The system prompt instructs the
    judge to weigh the summaries; silence answers that with "go and find them", and the tuning
    files it would find hold the scaffold's canonical directives and every sibling unit's — none of
    which this author added."""
    briefing = _text(HarnessAssumptions(summaries=(), munges=()))
    assert briefing.strip()
    assert "no points-to summaries" in briefing


# ---------------------------------------------------------------------------------------------
# reading it off the author's state


def test_the_assumptions_are_read_from_the_state_the_tools_write():
    state = {"summaries": [_DISPLAY], "munges": [_PANIC]}
    assumptions = harness_assumptions(state)  # type: ignore[arg-type]
    assert assumptions.summaries == (_DISPLAY,)
    assert assumptions.munges == (_PANIC,)


def test_a_run_that_used_neither_instrument_reads_as_empty():
    assert harness_assumptions({"summaries": [], "munges": []}) == HarnessAssumptions((), ())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------------
# the lift


def _base() -> JudgeInput:
    return JudgeInput(
        input=["The proposed CVLR harness module is", "fn r() {}"],
        curr_spec="fn r() {}",
        memory=None,
        did_read=False,
    )


def test_the_briefing_is_added_to_the_judges_input_rather_than_replacing_it():
    """The failure this guards is silent in the direction that matters: a lift that returned only
    the briefing would leave the judge reviewing summaries with no draft, and the judge reads the
    draft back with `get_harness` anyway — so it would produce a plausible review and nobody would
    see that the input had been emptied."""
    lifted = with_assumptions(_base(), HarnessAssumptions(summaries=(_DISPLAY,), munges=()))
    assert lifted["input"][:2] == _base()["input"]
    assert any(_DISPLAY.pattern in str(part) for part in lifted["input"])


def test_the_rest_of_the_judges_input_survives_the_lift():
    lifted = with_assumptions(_base(), HarnessAssumptions((), ()))
    assert lifted["curr_spec"] == "fn r() {}"
    assert lifted["did_read"] is False


def test_the_briefing_lands_after_what_input_parts_built():
    """Ordering is a claim about what the judge reads first: the artifact under review, then the
    caveats on it. Reversed, the review opens on a list of symbol patterns with no context."""
    lifted = with_assumptions(_base(), HarnessAssumptions(summaries=(_DISPLAY,), munges=()))
    assert _DISPLAY.pattern in str(lifted["input"][-1])


def test_the_judge_is_shown_the_diff_and_not_only_a_description(tmp_path):
    """The half of ``docs/munge-and-working-copies.md`` §8 gap 1 that stayed open: EVM's reviewer
    receives a diff, and this judge received a sentence the editor wrote about its own work. The
    diff is derived from the munge records, so no working tree is involved and none has to exist.
    """
    source = "pub fn calculate_fees(r: &Reserve) -> Result<u64> {\n    Ok(0)\n}\n"
    (tmp_path / "programs" / "p" / "src").mkdir(parents=True)
    (tmp_path / "programs/p/src/reserve.rs").write_text(source)

    munge = FunctionMunge(
        path="programs/p/src/reserve.rs",
        function="calculate_fees",
        kind=EarlyPanic(),
        why="[3308] on the error type's Display impl",
        feature="unit_vault",
    )
    briefing = _text(
        harness_assumptions(
            {"summaries": [], "munges": [munge]},  # type: ignore[arg-type]
            tmp_path,
        )
    )
    assert '+#[cfg_attr(feature = "unit_vault", cvlr::early_panic)]' in briefing
    assert "the summary is the editor's account of its own work" in briefing


def test_a_briefing_without_a_project_still_describes_the_munges(tmp_path):
    """The diff is an addition, not a replacement: a caller with no project on hand gets the weaker
    briefing rather than an error, and the descriptions it always had."""
    munge = FunctionMunge(
        path="p.rs", function="f", kind=EarlyPanic(), why="w", feature="unit_vault"
    )
    briefing = _text(
        harness_assumptions({"summaries": [], "munges": [munge]})  # type: ignore[arg-type]
    )
    assert "f (p.rs)" in briefing
    assert "@@" not in briefing
