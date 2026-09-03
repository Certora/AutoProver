"""How a Solana counterexample renders for an analyzer.

``docs/cvlr-backend-plan.md`` §7.7. The fixtures are unedited output from two real Solana Prover
runs against ``test_scenarios/solana_vault_idl``, which makes this the first test of
:mod:`composer.prover.results` that does not submit a live job — the only other one
(``test_tree_parsing``) is marked ``expensive`` and costs a cloud run per assertion.

Two counterexamples, because they fail in the two different ways this backend has actually seen:

* ``assertion_failed`` — a genuine property violation. ``withdraw`` of 100 lamports left the vault
  down only 99, because a fee collector aliased with the vault got the rest.
* ``loop_unwinding`` — not a violation of the property at all, but the prover reporting that it
  could not discharge a loop bound. Worth keeping as a fixture precisely because a findings
  synthesizer handed this would write it up as a bug in the program.
"""

import json
import re
from pathlib import Path

from composer.prover.ptypes import IncompleteCheck, PropertyViolation, classify_violation
from composer.prover.results import (
    EVM_TRACE,
    GENERIC_TRACE,
    SOLANA_TRACE,
    counterexample,
    read_and_format_run_result,
    trace_shape,
)

FIXTURES = Path(__file__).parent / "data" / "solana_cex"


def _messages(cex: str) -> list[str]:
    return re.findall(r"<message>(.*?)</message>", cex, flags=re.DOTALL)


def _rendered(dump: dict, shape) -> str:
    cex = counterexample(dump, shape)
    assert cex is not None
    return cex.render()


def _assertion_failed_dump() -> dict:
    return json.loads((FIXTURES / "assertion_failed" / "rule_output.json").read_text())


def _loop_unwinding_dump() -> dict:
    path = FIXTURES / "loop_unwinding" / "Reports" / "treeView" / "rule_output_2.json"
    return json.loads(path.read_text())


def _loop_unwinding_cex() -> str:
    parsed = read_and_format_run_result(FIXTURES / "loop_unwinding", "solana")
    assert not isinstance(parsed, str), parsed
    cex = parsed["rule_deposit_credits_exactly_the_amount"].cex_dump
    assert cex is not None
    return cex


def test_a_real_solana_run_parses_with_no_chain_specific_code():
    """The claim §5.3 made — one treeView parser serves both chains — against real Solana output."""
    parsed = read_and_format_run_result(FIXTURES / "loop_unwinding", "solana")
    assert not isinstance(parsed, str), parsed
    assert {name: r.status for name, r in parsed.items()} == {
        "rule_deposit_credits_exactly_the_amount": "VIOLATED",
        "rule_dispatch_is_reachable": "ERROR",
        "rule_vault_state_deserializes": "VERIFIED",
    }


def test_the_counterexample_states_what_failed_and_where():
    cex = counterexample(_assertion_failed_dump(), SOLANA_TRACE)
    assert cex is not None
    assert cex.assertion == "assertion failed"
    assert cex.source is not None
    assert cex.source.pprint() == "programs/vault/src/certora/specs/withdrawal_fee_sharing.rs:272"
    rendered = cex.render()
    assert "<assert>assertion failed</assert>" in rendered
    assert (
        "<source>programs/vault/src/certora/specs/withdrawal_fee_sharing.rs:272</source>"
        in rendered
    )


def test_the_counterexample_keeps_the_values_that_make_it_a_finding():
    """CVLR puts a rule's state in the trace as named frames (``clog!``), not in the ``variables``
    table an EVM counterexample uses — which is empty on every Solana output measured. So the
    trace is the only place a finding's numbers exist, and it has to survive rendering."""
    messages = _messages(_rendered(_assertion_failed_dump(), SOLANA_TRACE))
    assert "vault_lamports_before: '100'" in messages
    assert "amount: '100'" in messages
    assert "vault_lamports_after: '1'" in messages
    assert "vault_lamports_before - vault_lamports_after: '99'" in messages
    assert "assert FAIL" in messages


def test_the_loop_bound_failure_survives_rendering():
    """The regression that motivated a per-chain shape.

    EVM drops the ``unknown loop source code`` frame and everything under it. On Solana that frame
    is where a loop-unwinding violation keeps both its per-iteration structure and the assertion
    that failed, so EVM's shape renders a trace that ends at the handler call and states no
    failure at all.
    """
    messages = _messages(_loop_unwinding_cex())
    assert "Assert 'loop has terminated' failed" in messages
    assert "Loop Iteration 2" in messages

    under_evm = _messages(_rendered(_loop_unwinding_dump(), EVM_TRACE))
    assert not any("loop has terminated" in m for m in under_evm)


def test_account_setup_is_elided_rather_than_dropped():
    """A reader can tell setup happened and how much of it was hidden, which is what distinguishes
    eliding from dropping. 128 of the 135 frames in this trace were account materialization."""
    cex = _loop_unwinding_cex()
    assert "<elided>128 frames of setup</elided>" in cex
    assert "cvlr_solana::layout::cvlr_deserialize_nondet_accounts(...)" in cex
    assert "CVT_alloc_slice" not in cex
    assert len(_messages(cex)) == 15


def test_allocator_frames_are_matched_by_name_not_by_the_value_they_hold():
    """``__rust_alloc: '0x300000498'`` is one frame per allocation, all with different text. A shape
    that matched whole messages would drop none of them."""
    assert "__rust_alloc" not in _rendered(_assertion_failed_dump(), SOLANA_TRACE)


def test_the_evm_shape_is_the_one_it_had_before_the_chain_seam():
    """Pinned rather than measured against an EVM fixture, which would cost a cloud run. What
    matters is that introducing the seam changed EVM's rendering only by adding the two new
    elements, and that is a property of these four names and an empty elision set."""
    assert EVM_TRACE.dropped == frozenset(
        {"Setup", "Global State", "Evaluate branch condition", "unknown loop source code"}
    )
    assert EVM_TRACE.elided == frozenset()


def test_a_chain_with_no_measured_trace_claims_nothing_about_it():
    """Soroban reaches this parser once §7.9 lands, and what its setup frame is called is a thing
    to measure from one of its counterexamples rather than inherit from Solana's."""
    assert trace_shape("soroban") == GENERIC_TRACE
    assert GENERIC_TRACE.elided == frozenset()
    assert trace_shape("solana") == SOLANA_TRACE
    assert trace_shape("evm") == EVM_TRACE


def test_output_without_a_call_trace_is_not_a_counterexample():
    assert counterexample({"assertMessage": "assertion failed"}, SOLANA_TRACE) is None


def test_a_loop_bound_the_prover_could_not_discharge_is_not_a_property_violation():
    """The gate this phase needed. Both of these are VIOLATED with a counterexample attached, and
    only one of them is a bug in the program: the other is the prover reporting that it stopped."""
    loop = counterexample(_loop_unwinding_dump(), SOLANA_TRACE)
    match classify_violation(loop):
        case IncompleteCheck(assertion=assertion):
            assert assertion.startswith("Unwinding condition in a loop")
        case other:
            raise AssertionError(f"loop bound classified as {other}")

    real = counterexample(_assertion_failed_dump(), SOLANA_TRACE)
    assert classify_violation(real) == PropertyViolation()


def test_an_unrecognized_assertion_is_treated_as_the_rule_s_own():
    """The classification is a filter that fails safe: drift in the list of prover-generated
    assertions costs a spuriously reported finding, never a suppressed real one."""
    assert classify_violation(None) == PropertyViolation()
    assert classify_violation(
        counterexample({"callTrace": {"message": {"text": "r()", "arguments": []},
                                      "childrenList": []}}, SOLANA_TRACE)
    ) == PropertyViolation()
