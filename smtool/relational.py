"""Relational (k-call) conformance-rule templates — properties of `f_sol` that need MORE THAN ONE call
to state (monotonicity, injectivity, ...), unlike overapprox's per-output Phi (one call).

`build_conformance_rule` (overapprox) is the 1-call, per-output template: `y = f(x); assert Phi(x,y)`.
This module adds k-call templates. A discharged relational rule LICENSES adding the property as a ghost
AXIOM on the deterministic-memo summary (detsummary) — the same sound-by-construction gate as the
per-output Phi, but the property rides on the ghost (`fGhost` is monotone/injective), not `require Phi`
(one call can't state a cross-call property). Generic/templated: parameterized by the target signature +
which arg/output/relation.

v1: MONOTONICITY of a SCALAR argument (multi-return aware) and INJECTIVITY (scalar-tuple key or a single
array param's bounded prefix key). Struct-field ("equal-except") relations are follow-ups.
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


def build_monotonicity_spec(t: OverApproxTarget, spec: MonotoneSpec) -> S.CVLFile | None:
    """The monotonicity rule wrapped in a runnable spec: the scene setup import (if any) + an envfree
    decl of `f_sol` (for an envfree target, so the two calls need no env) + the rule. Mirrors
    overapprox.build_conformance_spec's envfree/setup wiring."""
    rule = build_monotonicity_rule(t, spec)
    if rule is None:
        return None
    return _wrap_relational_spec(t, rule)


def _wrap_relational_spec(t: OverApproxTarget, rule: S.RuleBlock) -> S.CVLFile:
    """A relational rule wrapped in a runnable spec: the scene setup import (if any) + an envfree decl of
    `f_sol` (for an envfree target, so the calls need no env) + the rule."""
    blocks: list = []
    if _envfree(t):
        blocks.append(x.methods_block([x.m_envfree(t.cut, t.sig.name,
                                                   [p.type for p in t.sig.params], list(t.sig.returns))]))
    blocks.append(rule)
    imports = [t.setup_spec_import] if t.setup_spec_import else ()
    return x.spec_file(imports=imports, blocks=blocks)


@dataclass
class InjectiveSpec:
    """`f` is injective: distinct inputs produce distinct outputs. The input KEY that must differ matches
    detsummary's ghost keying, so a discharged rule licenses injectivity on the memo:
    - SCALAR params -> the whole param tuple (distinct = any param differs).
    - one ARRAY param -> the bounded PREFIX `(length, first `key_len` elements)`, so the rule proves
      `distinct-prefix => distinct-output` (sound for the prefix-keyed ghost; the real hash conflates
      only same-prefix inputs, which the ghost is allowed to collapse). `out` selects the output
      component compared (single-return: 0). `guard` optionally restricts the domain."""
    out: int = 0                      # output component asserted distinct (0 for single-return)
    key_len: int = 3                  # array-prefix length (must match MemoTarget.key_len)
    elem_cast: str = "assert_uint256" # cast an array element to a comparable scalar (matches detsummary)
    guard: object = None              # optional S.Expression bool guard over the A-params; None = unguarded
    call_host: str | None = None      # call the real fn through this alias (`<alias>.f(...)`) when the
                                      # target is a dependency reached by a `using` alias in a larger CUT;
                                      # None = bare call `f(...)` (target IS the verified contract)


def injective_rule_name(fn: str) -> str:
    return f"injective_{fn}"


def _prefix_key_exprs(aname: str, spec: InjectiveSpec) -> list:
    """The ghost-key components of array param `aname` as expressions: `[length, e0, e1, ...]` where
    `ei = length > i ? <cast>(aname[i]) : 0` — identical to detsummary._key_and_body_array's key."""
    length = x.field(x.ident(aname), "length")
    keys = [length]
    for i in range(spec.key_len):
        elem = x.call(spec.elem_cast, [x.idx(x.ident(aname), x.num(i))])
        keys.append(x.cond(x.binop("gt", length, x.num(i)), elem, x.num(0)))
    return keys


def _or(exprs: list):
    """Left-fold a non-empty list of booleans with `||`."""
    acc = exprs[0]
    for e in exprs[1:]:
        acc = x.binop("or", acc, e)
    return acc


def build_injectivity_rule(t: OverApproxTarget, spec: InjectiveSpec = InjectiveSpec()) -> S.RuleBlock | None:
    """`injective_<fn>`: two REAL calls on independent inputs A and B; require the input KEYS differ, then
    assert the chosen output component differs. Plain calls (a reverting input prunes the path), matching
    build_monotonicity_rule. Returns None for a void / out-of-range target."""
    sig = t.sig
    rets = list(sig.returns)
    if not rets or spec.out >= len(rets):
        return None
    envfree = _envfree(t)
    a = {p.name: p.name + "_a" for p in sig.params}
    b = {p.name: p.name + "_b" for p in sig.params}
    rule_params = ([(p.type, a[p.name]) for p in sig.params]
                   + [(p.type, b[p.name]) for p in sig.params])
    lead = [] if envfree else [x.ident("e")]
    args_a = [x.ident(a[p.name]) for p in sig.params]
    args_b = [x.ident(b[p.name]) for p in sig.params]
    lo, hi = _names("rA", len(rets)), _names("rB", len(rets))

    arr = [p for p in sig.params if p.type.rstrip().endswith("[]")]
    single_array = len(sig.params) == 1 and len(arr) == 1
    if single_array:                                          # distinct = the bounded prefix keys differ
        ka = _prefix_key_exprs(a[arr[0].name], spec)
        kb = _prefix_key_exprs(b[arr[0].name], spec)
        distinct = _or([x.binop("ne", ca, cb) for ca, cb in zip(ka, kb)])
    else:                                                     # distinct = any scalar param differs
        distinct = _or([x.binop("ne", x.ident(a[p.name]), x.ident(b[p.name])) for p in sig.params])

    cmds: list = []
    if not envfree:
        cmds.append(x.declare("env", "e"))
    cmds.append(x.require(distinct, "the two inputs differ on the summary key"))
    if spec.guard is not None:
        cmds.append(x.require(spec.guard, "injectivity domain guard"))
    call_a = x.call(sig.name, lead + args_a, host=spec.call_host)   # plain call: non-reverting inputs only
    call_b = x.call(sig.name, lead + args_b, host=spec.call_host)
    if len(rets) == 1:
        cmds += [x.declare(rets[0], lo[0], call_a), x.declare(rets[0], hi[0], call_b)]
    else:
        cmds += [x.declare(rt, n) for rt, n in zip(rets, lo)]
        cmds.append(x.assign_multi(lo, call_a))
        cmds += [x.declare(rt, n) for rt, n in zip(rets, hi)]
        cmds.append(x.assign_multi(hi, call_b))
    cmds.append(x.assert_(
        x.binop("ne", x.ident(lo[spec.out]), x.ident(hi[spec.out])),
        f"injective: distinct inputs give a distinct output {spec.out}"))
    return x.rule(injective_rule_name(sig.name), rule_params, cmds)


def build_injectivity_spec(t: OverApproxTarget, spec: InjectiveSpec = InjectiveSpec()) -> S.CVLFile | None:
    """The injectivity rule wrapped in a runnable spec (setup import + envfree decl + rule)."""
    rule = build_injectivity_rule(t, spec)
    return _wrap_relational_spec(t, rule) if rule is not None else None
