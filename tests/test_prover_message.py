"""A Prover message's ``[NNNN]`` code is what pushes a recipe at the author, so it is read from the
one place the Prover fixes: the start of the message (``FormattedErrorMessage`` in the Prover's
``sbf/support/Errors.kt``)."""

import pytest

from composer.prover.ptypes import AnalysisErrorKind, Uncoded, UnknownCode
from composer.prover.results import prover_message

_MESSAGES = {
    AnalysisErrorKind.UNKNOWN_PROVENANCE_DEREF: "[3003] dereference of a register with unknown provenance",
    AnalysisErrorKind.DYNAMIC_MEMCPY: "[3005] memcpy with dynamically sized length",
    AnalysisErrorKind.STACK_POINTER_STORE: "[3006] illegal store of a stack pointer",
    AnalysisErrorKind.ABSOLUTE_ADDRESS_DEREF: "[3308] illegal dereference of an absolute address",
}


@pytest.mark.parametrize("kind", list(AnalysisErrorKind))
def test_each_listed_code_is_read_from_the_start_of_its_message(kind: AnalysisErrorKind):
    text = f"{_MESSAGES[kind]}\nsource: src/state.rs:118\n  from: src/handlers/deposit.rs:42"
    message = prover_message(text)
    assert message.code is kind
    assert message.text == text


def test_a_prover_before_8815_prefixes_the_rule_and_the_code_is_still_read():
    assert prover_message("In rule_deposit: [3308] illegal dereference").code is (
        AnalysisErrorKind.ABSOLUTE_ADDRESS_DEREF
    )


def test_a_code_this_build_does_not_list_is_kept_as_a_number():
    assert prover_message("[4000] no assertion").code == UnknownCode(4000)


def test_a_message_with_no_code_is_uncoded():
    assert prover_message("Rule rule_x timed-out").code == Uncoded()
    # A code that is not at the start is not the message's code.
    assert prover_message("see [3308] above").code == Uncoded()
