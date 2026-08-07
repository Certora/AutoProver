"""Per-function OVER-APPROXIMATION summary generator (an extension of the whole-CUT model in driver.py).

Given a scene function `f_sol` and a predicate `Phi(params, res)`, this emits three artifacts that
share the SAME `Phi` (authored once):

  1. the Phi spec              — `function <f>Phi(<params>, <rt> res) returns bool { <body> }`
  2. the over-approx SUMMARY   — `function <f>CVL(<params>, env e) returns <rt>
                                     { <rt> res; require <f>Phi(<params>, res); return res; }`
                                 + a methods{} binding `C.f(...) => <f>CVL(..., e)`
  3. the conformance PROOF     — `rule overApprox_<f>(<params>)
                                     { env e; <rt> retSol = f(e, ...); assert !revert => <f>Phi(..., retSol); }`

Soundness is by construction: the summary returns ANY value satisfying `Phi`, and the conformance rule
proves the REAL output satisfies `Phi` (so the summary over-approximates `f_sol`). Install the summary
IFF the rule discharges. A passing rule always implies over-approximation (`forall a => exists a`), so
the discipline never trades soundness — the only requirement, enforced by keeping `Phi` a boolean
predicate over `(params, res)` with functionally-pinned internals, is completeness (that the honest
`assert Phi(f_sol(x))` is not accidentally stronger than the `exists`-execution soundness statement).

This mirrors driver.build_return_rule, minus the model/glue: the exact-equality conformance smtool
already emits is the special case `Phi(x,y) = (y == f_cvl_exact(x))`. Reuses cvlx AST builders + ir.
v1: single-return f_sol (void => no rule; multi-return is a later generalization).
"""

from dataclasses import dataclass

import composer.cvl.schema as S

from . import cvlx as x
from .ir import Signature


def phi_name(fn: str) -> str:
    """The predicate function name for `fn` — `<fn>Phi`. One place so summary and proof agree."""
    return fn + "Phi"


def summary_fn_name(fn: str) -> str:
    """The over-approximating summary function name — `<fn>CVL`."""
    return fn + "CVL"


@dataclass
class OverApproxTarget:
    """What to summarize: the contract `cut` holding `f_sol` (signature `sig`), and `Phi`'s body.

    `phi_body` is the command list of the `Phi(params, res)` predicate (correct-by-construction HOLE —
    the agent/user fills it, exactly like driver's HOLE-F). None => the obvious stub `return true`.
    `phi_body` MUST be a boolean predicate over the params + `res` (functionally-pinned internals only);
    that restriction is what makes the emitted conformance rule sound AND complete (see module doc)."""

    cut: str
    sig: Signature
    phi_body: list | None = None
    result_name: str = "res"
    phi_spec_name: str | None = None      # file the Phi spec is written as / imported (default <fn>Phi.spec)
    model_spec_import: str | None = None  # optional model spec Phi/summary read ghosts from
    setup_spec_import: str | None = None  # the scene's setup spec the CONFORMANCE spec imports (scene
                                          # aliases/summaries + the CUT declaration); None => self-contained
    goal: str = ""                        # NL description of what Phi should preserve (agent-facing; drives
                                          # the goal-directed fill — with no goal the agent settles on `true`)
    envfree: bool | None = None           # override env-freeness. None => derive from mutability
                                          # (pure/view). But view != envfree: a `view` fn that reads
                                          # block.timestamp / a getter is env-DEPENDENT and must be
                                          # env-threaded (set False), else the envfree static check fails.

    @property
    def phi_import(self) -> str:
        return self.phi_spec_name or (phi_name(self.sig.name) + ".spec")


def _names(base: str, n: int) -> list:
    """Result-variable names: `[base]` for a single return, `[base0, base1, ...]` for multi-return.
    Single-return keeps the bare `base` (back-compat with the emitted single-return specs)."""
    return [base] if n == 1 else [f"{base}{i}" for i in range(n)]


def _phi_params(t: OverApproxTarget) -> list:
    """Phi's parameter list: the function's params followed by ITS RETURNS — `(<params>, <rt> res)` for a
    single return, `(<params>, T0 res0, T1 res1, ...)` for multi-return (Phi over the whole tuple)."""
    resn = _names(t.result_name, len(t.sig.returns))
    return [(p.type, p.name) for p in t.sig.params] + list(zip(t.sig.returns, resn))


def build_phi(t: OverApproxTarget) -> S.FunctionDef:
    """`function <fn>Phi(<params>, <rt> res) returns bool { <body> }`. Body defaults to a `return true`
    stub (the HOLE) so the skeleton typechecks before Phi is filled."""
    body = t.phi_body if t.phi_body is not None else [x.ret([x.boollit(True)])]
    return x.func(phi_name(t.sig.name), _phi_params(t), ["bool"], body)


def build_phi_spec(t: OverApproxTarget) -> S.CVLFile:
    """The shared Phi spec — just the predicate (optionally importing a model spec it reads)."""
    imports = [t.model_spec_import] if t.model_spec_import else ()
    return x.spec_file(imports=imports, blocks=[build_phi(t)])


def _envfree(t: OverApproxTarget) -> bool:
    """Whether to summarize `f_sol` ENVFREE (no `env` threaded through summary, binding, or proof).
    Honors an explicit `t.envfree` override; otherwise DERIVES from mutability (pure/view). NB view !=
    envfree — a `view` that reads `block.timestamp` or storage is env-DEPENDENT; declaring it envfree
    makes the prover's envfree static check fail, so pass `envfree=False` for those. (Deriving this
    automatically from the body is the proper fix; the override is the escape hatch until then.)"""
    if t.envfree is not None:
        return t.envfree
    return t.sig.mutability in ("pure", "view")


def build_summary(t: OverApproxTarget) -> S.FunctionDef:
    """The over-approximating summary body: havoc each result, `require Phi(params, res...)`, return the
    (tuple of) result(s). Takes a trailing `env e` param only for a non-envfree `f_sol`."""
    rets = list(t.sig.returns)
    resn = _names(t.result_name, len(rets))
    phi_args = [x.ident(p.name) for p in t.sig.params] + [x.ident(n) for n in resn]
    cmds = [x.declare(rt, n) for rt, n in zip(rets, resn)]                       # havoc each result
    cmds.append(x.require(x.call(phi_name(t.sig.name), phi_args), "over-approx: result satisfies Phi"))
    cmds.append(x.ret([x.ident(n) for n in resn]))                              # return res / (res0, res1, ...)
    params = [(p.type, p.name) for p in t.sig.params]
    if not _envfree(t):
        params = params + [("env", "e")]
    return x.func(summary_fn_name(t.sig.name), params, rets, cmds)


def build_summary_spec(t: OverApproxTarget) -> S.CVLFile:
    """The summary spec: imports Phi, defines `<fn>CVL`, and binds `C.f => <fn>CVL(...)`."""
    envfree = _envfree(t)
    call_args = [x.ident(p.name) for p in t.sig.params] + ([] if envfree else [x.ident("e")])
    call = x.call(summary_fn_name(t.sig.name), call_args)
    binding = x.m_expr_summary(t.cut, t.sig.name, [(p.type, p.name) for p in t.sig.params],
                               list(t.sig.returns), call, with_env=None if envfree else "e")
    return x.spec_file(imports=[t.phi_import], contracts=(),
                       blocks=[build_summary(t), x.methods_block([binding])])


def _call_real(t: OverApproxTarget) -> S.FunctionCall:
    """Call the REAL `f_sol` — `f([e,] args...)`, unqualified (resolves to currentContract), `@withrevert`
    so the rule can gate the assertion on real success (mirrors driver._call_real). No leading `e` when
    envfree."""
    lead = [] if _envfree(t) else [x.ident("e")]
    return x.call(t.sig.name, lead + [x.ident(p.name) for p in t.sig.params],
                  host=None, annotation="withrevert")


def build_conformance_rule(t: OverApproxTarget) -> S.RuleBlock | None:
    """`overApprox_<fn>`: call the real function and assert its output satisfies Phi on real success —
    `assert !realReverted => Phi(params, retSol...)`. Proves `f_cvl` over-approximates `f_sol`. Returns
    None for a VOID function (no result to constrain). Handles multi-return: the tuple is bound via a
    multi-assignment `(retSol0, retSol1, ...) = f@withrevert(...)` and Phi ranges over all components."""
    rets = list(t.sig.returns)
    if not rets:
        return None                                                            # void: nothing to constrain
    retn = _names("retSol", len(rets))
    params = [(p.type, p.name) for p in t.sig.params]
    phi_args = [x.ident(p.name) for p in t.sig.params] + [x.ident(n) for n in retn]
    cmds: list = []
    if not _envfree(t):
        cmds.append(x.declare("env", "e"))
    if len(rets) == 1:
        cmds.append(x.declare(rets[0], retn[0], _call_real(t)))                # single: declare + init
    else:
        cmds += [x.declare(rt, n) for rt, n in zip(rets, retn)]                # multi: declare each ...
        cmds.append(x.assign_multi(retn, _call_real(t)))                       # ... then (r0, r1, ...) = f@withrevert(...)
    cmds += [
        x.declare("bool", "realRev", x.ident("lastReverted")),
        x.assert_(
            x.binop("implies", x.unop_not(x.ident("realRev")),
                    x.call(phi_name(t.sig.name), phi_args)),
            "real output must satisfy Phi (summary over-approximates the real function)",
        ),
    ]
    return x.rule("overApprox_" + t.sig.name, params, cmds)


def build_conformance_spec(t: OverApproxTarget) -> S.CVLFile:
    """The conformance spec: imports the scene setup spec (if any, for the scene's aliases/summaries +
    the CUT declaration) + Phi + (for an envfree target) an envfree decl of `f_sol` + the over-approx
    rule (real `f_sol`, no summary imported)."""
    rule = build_conformance_rule(t)
    blocks: list = []
    if _envfree(t):
        blocks.append(x.methods_block([x.m_envfree(t.cut, t.sig.name,
                                                   [p.type for p in t.sig.params], list(t.sig.returns))]))
    if rule is not None:
        blocks.append(rule)
    imports = ([t.setup_spec_import] if t.setup_spec_import else []) + [t.phi_import]
    return x.spec_file(imports=imports, contracts=(), blocks=blocks)
