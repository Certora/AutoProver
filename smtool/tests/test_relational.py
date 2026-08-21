"""Relational (k-call) conformance templates — monotonicity. Fast, no LLM/prover/scene, generic names.

The CHECK rule is quantifier-free (the rule's free params are the ∀). The ENCODE side (the monotone ghost
axiom in detsummary) IS quantified, because a CVL ghost axiom must be a closed formula — see
test_detsummary."""
from composer.cvl.pretty_print import pretty_print

from smtool import cvlx as x
from smtool.ir import Signature, Param as P
from smtool.overapprox import OverApproxTarget
from smtool.relational import build_monotonicity_rule, build_monotonicity_spec, MonotoneSpec


def _t(name, params, returns, mutability="pure", **kw):
    return OverApproxTarget(cut="C", sig=Signature(name=name, params=[P(t, n) for t, n in params],
                                                   returns=list(returns), mutability=mutability), **kw)


def _render(rule):
    return pretty_print(x.spec_file(blocks=[rule]))


def test_monotone_rule_scalar_is_quantifier_free():
    """Two real calls agreeing except the varied arg; assert the output is ordered. No quantifier — the
    rule params ARE the universal quantification."""
    r = build_monotonicity_rule(_t("feeOut", [("uint24", "fee"), ("uint256", "amount")], ["uint256"]),
                                MonotoneSpec(arg=0, increasing=True))
    txt = _render(r)
    assert "rule monotone_feeOut_arg0(uint24 fee, uint256 amount, uint24 fee_hi)" in txt
    assert 'require(fee < fee_hi' in txt                          # varied arg strictly increases
    assert "uint256 rLo = feeOut(fee, amount);" in txt           # call A (no @withrevert)
    assert "uint256 rHi = feeOut(fee_hi, amount);" in txt        # call B: only the varied arg changes
    assert "assert(rLo <= rHi" in txt                            # non-decreasing
    assert "forall" not in txt and "@withrevert" not in txt      # quantifier-free; plain calls


def test_monotone_rule_decreasing_multireturn_guard_env():
    g = x.binop("lt", x.ident("fee_hi"), x.num(1000000))
    r = build_monotonicity_rule(
        _t("quote", [("uint24", "fee"), ("uint256", "amt")], ["uint256", "uint24", "uint256"],
           mutability="view", envfree=False),
        MonotoneSpec(arg=0, out=1, increasing=False, guard=g))
    txt = _render(r)
    assert "env e;" in txt                                        # shared env across both calls
    assert 'require(fee_hi < 1000000' in txt                      # domain guard
    assert "(rLo0, rLo1, rLo2) = quote(e, fee, amt);" in txt      # multi-return tuple binds
    assert "(rHi0, rHi1, rHi2) = quote(e, fee_hi, amt);" in txt
    assert "assert(rLo1 >= rHi1" in txt                           # non-increasing, output component 1


def test_monotone_spec_wraps_rule_with_envfree_decl():
    """The runnable spec adds the envfree decl (an envfree target's two calls need no env) + the rule —
    without it the pure calls fail to typecheck ('missing environment parameter')."""
    t = _t("feeOut", [("uint24", "fee"), ("uint256", "amount")], ["uint256"])   # pure => envfree
    txt = pretty_print(build_monotonicity_spec(t, MonotoneSpec(arg=0)))
    assert "function C.feeOut(uint24, uint256) external returns (uint256) envfree;" in txt
    assert "rule monotone_feeOut_arg0(" in txt
    # non-envfree target: env threaded instead, no envfree decl
    t2 = _t("act", [("uint256", "amt")], ["uint256"], mutability="nonpayable")
    txt2 = pretty_print(build_monotonicity_spec(t2, MonotoneSpec(arg=0)))
    assert "envfree" not in txt2 and "env e;" in txt2


def test_monotone_rule_none_for_void_or_bad_index():
    assert build_monotonicity_rule(_t("poke", [("uint256", "a")], [], mutability="nonpayable"),
                                   MonotoneSpec(arg=0)) is None          # void
    assert build_monotonicity_rule(_t("f", [("uint256", "a")], ["uint256"]),
                                   MonotoneSpec(arg=5)) is None          # arg out of range
    assert build_monotonicity_rule(_t("f", [("uint256", "a")], ["uint256"]),
                                   MonotoneSpec(arg=0, out=3)) is None    # out of range
