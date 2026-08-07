"""Relational (k-call) conformance-rule templates — properties of `f_sol` that need MORE THAN ONE call
to state (monotonicity, injectivity, ...), unlike overapprox's per-output Phi (one call).

`build_conformance_rule` (overapprox) is the 1-call, per-output template: `y = f(x); assert Phi(x,y)`.
This module adds k-call templates. A discharged relational rule LICENSES adding the property as a ghost
AXIOM on the deterministic-memo summary (detsummary) — the same sound-by-construction gate as the
per-output Phi, but the property rides on the ghost (`fGhost` is monotone/injective), not `require Phi`
(one call can't state a cross-call property). Generic/templated: parameterized by the target signature +
which arg/output/relation.

v1: MONOTONICITY of a SCALAR argument (multi-return aware). Injectivity and struct-field ("equal-except")
relations are follow-ups.
"""
from dataclasses import dataclass

import composer.cvl.schema as S

from . import cvlx as x
from .overapprox import OverApproxTarget, _envfree, _names


@dataclass
class MonotoneSpec:
    """`f` is monotone in scalar param `arg` w.r.t. output component `out`: raising `arg` (others fixed)
    does not lower (increasing) / not raise (decreasing) that output. `guard` is an optional CVL bool
    AST over the params (e.g. the raised value < a cap) restricting the domain where it holds."""
    arg: int                     # index of the (scalar) param that varies
    out: int = 0                 # output component compared (0 for single-return)
    increasing: bool = True      # non-decreasing (True) vs non-increasing (False)
    guard: object = None         # optional S.Expression bool guard over the params; None = unguarded


def monotone_rule_name(fn: str, arg: int) -> str:
    return f"monotone_{fn}_arg{arg}"


def build_monotonicity_rule(t: OverApproxTarget, spec: MonotoneSpec) -> S.RuleBlock | None:
    """`monotone_<fn>_arg<j>`: two REAL calls that agree on every arg except `arg` (call B raises it),
    then assert the chosen output component is ordered. Calls are plain (no @withrevert) — a reverting
    input prunes the path, so the property is asserted only where both calls succeed (matches the hand
    proofs). Returns None for a void/out-of-range target."""
    sig = t.sig
    rets = list(sig.returns)
    if not rets or spec.arg >= len(sig.params) or spec.out >= len(rets):
        return None
    envfree = _envfree(t)
    vname = sig.params[spec.arg].name
    vtype = sig.params[spec.arg].type
    v_hi = vname + "_hi"                                   # call B's raised value for the varied arg
    rule_params = [(p.type, p.name) for p in sig.params] + [(vtype, v_hi)]
    lead = [] if envfree else [x.ident("e")]              # a shared env across both calls
    args_lo = [x.ident(p.name) for p in sig.params]
    args_hi = [x.ident(p.name) if i != spec.arg else x.ident(v_hi) for i, p in enumerate(sig.params)]
    lo, hi = _names("rLo", len(rets)), _names("rHi", len(rets))

    cmds: list = []
    if not envfree:
        cmds.append(x.declare("env", "e"))
    cmds.append(x.require(x.binop("lt", x.ident(vname), x.ident(v_hi)), "the varied argument strictly increases"))
    if spec.guard is not None:
        cmds.append(x.require(spec.guard, "monotonicity domain guard"))
    call_lo = x.call(sig.name, lead + args_lo)            # plain call: non-reverting inputs only
    call_hi = x.call(sig.name, lead + args_hi)
    if len(rets) == 1:
        cmds += [x.declare(rets[0], lo[0], call_lo), x.declare(rets[0], hi[0], call_hi)]
    else:
        cmds += [x.declare(rt, n) for rt, n in zip(rets, lo)]
        cmds.append(x.assign_multi(lo, call_lo))
        cmds += [x.declare(rt, n) for rt, n in zip(rets, hi)]
        cmds.append(x.assign_multi(hi, call_hi))
    op = "le" if spec.increasing else "ge"
    cmds.append(x.assert_(
        x.binop(op, x.ident(lo[spec.out]), x.ident(hi[spec.out])),
        f"monotone: raising arg {spec.arg} does not "
        f"{'lower' if spec.increasing else 'raise'} output {spec.out}"))
    return x.rule(monotone_rule_name(sig.name, spec.arg), rule_params, cmds)
