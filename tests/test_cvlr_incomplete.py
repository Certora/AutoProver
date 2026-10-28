"""Telling a CVLR rule that stopped on the Solana Prover's own assertion from one that failed its own."""

from composer.prover.ptypes import LoopUnwound, PropertyViolation, RulePath
from composer.spec.cvlr.incomplete import OverflowUnproven, classify, incomplete_checks


def test_the_sound_math_pass_reporting_its_own_limit_is_not_a_property_violation():
    """The sound-signed-math pass asserting its own inference. Misclassified as the rule's own it
    becomes evidence, and the findings synthesizer writes the prover's limitation up as a bug in the
    code under verification."""
    assert classify("Cannot overflow: inferred pointer") == OverflowUnproven("Cannot overflow: inferred pointer")


def test_the_generic_loop_bound_is_still_recognized():
    assertion = "Unwinding condition in a loop. Try increasing the unwinding bound."
    assert classify(assertion) == LoopUnwound(assertion)


def test_an_unrecognized_assertion_is_the_rule_s_own():
    assert classify("assertion failed") == PropertyViolation()


def test_a_rule_is_incomplete_when_any_instance_stopped_on_a_generated_assertion():
    loop = "Unwinding condition in a loop."
    found = incomplete_checks({
        RulePath("rule_a"): "assertion failed",
        RulePath("rule_b", method="first"): "assertion failed",
        RulePath("rule_b", method="second"): loop,
    })

    assert found == {"rule_b": LoopUnwound(loop)}
