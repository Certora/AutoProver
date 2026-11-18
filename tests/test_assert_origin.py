"""Telling a rule that stopped on the Prover's own assertion from one that failed its own.

The Prover reports where a violated assertion came from as ``assertOrigin``. The message is not read.
"""

import pytest

from composer.prover.ptypes import (
    Counterexample,
    GeneratedAssertion,
    GeneratedKind,
    IncompleteCheck,
    PropertyViolation,
    UnclassifiedAssertion,
    UnknownKind,
    UserAssertion,
    classify_violation,
)
from composer.prover.results import assert_origin


def _cex(origin, assertion: str | None = "msg") -> Counterexample:
    return Counterexample(trace="<trace/>", origin=origin, assertion=assertion)


def test_the_rule_s_own_assertion():
    assert assert_origin("user") == UserAssertion()


@pytest.mark.parametrize("kind", list(GeneratedKind))
def test_every_kind_the_prover_names_is_read(kind: GeneratedKind):
    assert assert_origin(kind.value) == GeneratedAssertion(kind)


@pytest.mark.parametrize("reported", [None, 3])
def test_an_origin_the_prover_did_not_give(reported: object):
    assert assert_origin(reported) == UnclassifiedAssertion()


def test_a_kind_this_version_does_not_know_is_still_generated(caplog: pytest.LogCaptureFixture):
    assert assert_origin("someKindAddedLater") == GeneratedAssertion(UnknownKind("someKindAddedLater"))
    assert "someKindAddedLater" in caplog.text


def test_the_origin_decides_not_the_message():
    """A rule may well assert something worded like the Prover's own message."""
    cex = _cex(UserAssertion(), "Unwinding condition in a loop.")
    assert classify_violation(cex) == PropertyViolation()


def test_a_limit_of_the_prover_is_an_incomplete_check():
    cex = _cex(GeneratedAssertion(GeneratedKind.CANNOT_OVERFLOW), "Cannot overflow: inferred pointer")
    assert classify_violation(cex) == IncompleteCheck(
        GeneratedKind.CANNOT_OVERFLOW, "Cannot overflow: inferred pointer"
    )


@pytest.mark.parametrize("kind", [GeneratedKind.PANIC, GeneratedKind.MEMCPY_OVERLAP])
def test_a_check_the_prover_makes_on_the_program_s_behalf_is_a_finding(kind: GeneratedKind):
    assert classify_violation(_cex(GeneratedAssertion(kind))) == PropertyViolation()


def test_a_kind_this_version_does_not_know_is_an_incomplete_check():
    """Read as the rule's own, a new limit of the Prover would be written up as a bug in the program."""
    kind = UnknownKind("someKindAddedLater")
    assert classify_violation(_cex(GeneratedAssertion(kind), "msg")) == IncompleteCheck(kind, "msg")


def test_an_unclassified_assertion_is_the_rule_s_own():
    """A gap in the Prover's tagging can report a finding that is not one, and cannot hide a real one."""
    assert classify_violation(_cex(UnclassifiedAssertion())) == PropertyViolation()
