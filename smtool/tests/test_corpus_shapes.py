"""Generators exercised on the pure-math over-approx SHAPES that recur in real FV projects — without an
LLM/prover/scene, and without naming or reproducing any specific project's code. These are generic
patterns (AMM constant-product bounds, integer sqrt, mulDiv) used to check the over-approx / memo
builders, and to PIN the current v1 boundaries (multi-return) as known+tested, not silent.
"""
from smtool.ir import Signature, Param as P
from smtool.overapprox import OverApproxTarget
from smtool.overapprox_project import OverApproxProject
from smtool.detsummary import MemoTarget, render


def _proj(*targets, cut="C"):
    return OverApproxProject.of(cut, list(targets), setup_spec_import="Setup.spec")


# ---------------------------------------------------------------- two-sided algebraic bound Phi
def test_bound_phi_conformance_shape():
    """A per-output over-approx of a pure math fn: `y = f(x); assert bound(x, y)`. Here a constant-product
    AMM output with a two-sided algebraic bound as Phi — the common shape where exact equality is
    intractable (nonlinear) so a proved bound is the right form."""
    pr = _proj(OverApproxTarget(cut="C", sig=Signature(
        name="amountOut", params=[P("uint256", "deducted"), P("uint256", "reserveOut"),
                                   P("uint256", "reserveIn")], returns=["uint256"], mutability="view")))
    pr.set_phi("amountOut",
               "mathint lhs = res * (reserveIn + deducted);"
               "return lhs <= deducted * reserveOut"
               " && lhs >= deducted * (reserveOut - 1) - reserveIn;")
    conf = pr.render_conformance("amountOut")
    assert "rule overApprox_amountOut(uint256 deducted, uint256 reserveOut, uint256 reserveIn)" in conf
    assert "amountOut@withrevert(deducted, reserveOut, reserveIn)" in conf          # calls the REAL fn
    assert "! realRev => amountOutPhi(deducted, reserveOut, reserveIn, retSol)" in conf  # assert Phi on it
    assert "res * (reserveIn + deducted)" in pr.render_phi("amountOut")             # the algebraic bound (in Phi spec)


# ---------------------------------------------------------------- integer sqrt bracket Phi
def test_sqrt_bracket_phi():
    pr = _proj(OverApproxTarget(cut="C", sig=Signature(name="sqrt", params=[P("uint256", "x")],
                                                       returns=["uint256"], mutability="pure")))
    pr.set_phi("sqrt", "mathint r = res; return r * r <= to_mathint(x) && to_mathint(x) < (r + 1) * (r + 1);")
    conf = pr.render_conformance("sqrt")
    assert "rule overApprox_sqrt(uint256 x)" in conf and "sqrt@withrevert(x)" in conf
    assert "r * r <= to_mathint(x)" in pr.render_phi("sqrt")                         # cast round-trips (TO fix)


# ---------------------------------------------------------------- mulDiv: memo keyed on scalars
def test_muldiv_memo_scalar_keys():
    """A pure multi-scalar math fn -> the deterministic memo keys directly on the three uint256 params
    (no array prefix, no cast) — the model's ghost-keying principle applied to a scalar target."""
    t = MemoTarget(cut="C", fn="mulDiv",
                   params=[("uint256", "a"), ("uint256", "b"), ("uint256", "d")], ret="uint256")
    txt = render(t)
    assert "persistent ghost mulDivGhost(uint256, uint256, uint256) returns uint256;" in txt
    assert "uint256 res = mulDivGhost(a, b, d);" in txt
    assert "?" not in txt and "length" not in txt                                    # no array machinery
    assert "function C.mulDiv(uint256 _a, uint256 _b, uint256 _d) internal returns (uint256) => mulDivCVL(_a, _b, _d);" in txt


# ---------------------------------------------------------------- multi-return: Phi over the tuple
def test_multireturn_conformance_over_tuple():
    """A multi-return fn (e.g. a swap quote -> (amount, fee, feeAmount)): Phi ranges over the whole tuple,
    the rule binds it via a multi-assignment `(retSol0, retSol1, retSol2) = f@withrevert(...)`, and the
    summary returns the tuple with `expect (...)`."""
    pr = _proj(OverApproxTarget(cut="C", sig=Signature(
        name="quote", params=[P("bool", "flag"), P("uint256", "amountIn")],
        returns=["uint256", "uint24", "uint256"], mutability="view")))
    assert pr.provable_targets() == ["quote"]                        # multi-return IS provable now
    pr.set_phi("quote", "return res1 < 1000000 && (res0 > 0 => res2 <= amountIn);")
    phi = pr.render_phi("quote")
    assert "function quotePhi(bool flag, uint256 amountIn, uint256 res0, uint24 res1, uint256 res2)" in phi
    conf = pr.render_conformance("quote")
    assert "(retSol0, retSol1, retSol2) = quote@withrevert(flag, amountIn);" in conf   # tuple assignment
    assert "quotePhi(flag, amountIn, retSol0, retSol1, retSol2)" in conf               # Phi over the tuple
    summ = pr.render_summary("quote")
    assert "return (res0, res1, res2);" in summ
    assert "=> quoteCVL(flag, amountIn) expect (uint256, uint24, uint256);" in summ    # tuple binding
