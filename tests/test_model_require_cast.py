"""lint_model_spec rejects `require_uintN`/`require_intN` CAST expressions in a model body.

A require_* cast ASSUMES its argument fits the target type, silently pruning out-of-range (overflow)
inputs. In a conformance rule that makes `!realRev => ...` pass VACUOUSLY on exactly those inputs — a
false positive (confirmed on an ERC20 transferFrom credit-side balance). The sound form is the
`assert_*` cast: the prover then CHECKS the cast is total, so a provably-in-range cast passes and an
overflowing one is caught.
"""
from types import SimpleNamespace

from smtool import cvlx as x
from smtool.linter import lint_model_spec


def _model(*fns):
    return SimpleNamespace(model_spec=x.spec_file(blocks=list(fns)))


def _credit(cast):
    # colBalCVL[to] = <cast>(bal + amount)   -- the transferFrom credit
    body = [x.assign_index("colBalCVL", [x.ident("to")],
            x.call(cast, [x.binop("add", x.ident("bal"), x.ident("amount"))]))]
    return x.func("transferFromCVL", [("address", "to"), ("uint256", "bal"), ("uint256", "amount")], [], body)


def test_require_uint_cast_is_flagged():
    problems = lint_model_spec(_model(_credit("require_uint256")))
    assert any("require_uint256" in p and "require_* cast" in p for p in problems)


def test_require_int_cast_is_flagged():
    problems = lint_model_spec(_model(_credit("require_int128")))
    assert any("require_int128" in p for p in problems)


def test_assert_uint_cast_is_not_flagged():
    # the sound form — prover-checked totality; no soundness flag
    problems = lint_model_spec(_model(_credit("assert_uint256")))
    assert not any("cast" in p for p in problems)


def test_flag_message_names_assert_replacement():
    problems = lint_model_spec(_model(_credit("require_uint256")))
    msg = next(p for p in problems if "require_uint256" in p)
    assert "assert_uint256" in msg
