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

    @property
    def phi_import(self) -> str:
        return self.phi_spec_name or (phi_name(self.sig.name) + ".spec")


def _phi_params(t: OverApproxTarget) -> list:
    """Phi's parameter list: the function's params followed by the result — `(<params>, <rt> res)`."""
    return [(p.type, p.name) for p in t.sig.params] + [(t.sig.returns[0], t.result_name)]


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
    """A pure/view `f_sol` is summarized ENVFREE — no `env` threaded through summary, binding, or proof.
    This is both the natural shape for the over-approx targets (pure nonlinear math) and avoids emitting
    an `env`-typed param. State-changing `f_sol` uses the env form."""
    return t.sig.mutability in ("pure", "view")


def build_summary(t: OverApproxTarget) -> S.FunctionDef:
    """The over-approximating summary body: havoc `res`, `require Phi(params, res)`, `return res`.
    Takes a trailing `env e` param only for a non-envfree `f_sol`."""
    rt = t.sig.returns[0]
    res = t.result_name
    phi_args = [x.ident(p.name) for p in t.sig.params] + [x.ident(res)]
    cmds = [
        x.declare(rt, res),                                                     # havoc the result
        x.require(x.call(phi_name(t.sig.name), phi_args), "over-approx: result satisfies Phi"),
        x.ret([x.ident(res)]),
    ]
    params = [(p.type, p.name) for p in t.sig.params]
    if not _envfree(t):
        params = params + [("env", "e")]
    return x.func(summary_fn_name(t.sig.name), params, [rt], cmds)


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
    `assert !realReverted => Phi(params, retSol)`. Proves `f_cvl` over-approximates `f_sol`. Returns None
    for a void function (no result to constrain). Single-return in v1."""
    if len(t.sig.returns) != 1:
        return None
    rt = t.sig.returns[0]
    params = [(p.type, p.name) for p in t.sig.params]
    phi_args = [x.ident(p.name) for p in t.sig.params] + [x.ident("retSol")]
    cmds: list = []
    if not _envfree(t):
        cmds.append(x.declare("env", "e"))
    cmds += [
        x.declare(rt, "retSol", _call_real(t)),
        x.declare("bool", "realRev", x.ident("lastReverted")),
        x.assert_(
            x.binop("implies", x.unop_not(x.ident("realRev")),
                    x.call(phi_name(t.sig.name), phi_args)),
            "real output must satisfy Phi (summary over-approximates the real function)",
        ),
    ]
    return x.rule("overApprox_" + t.sig.name, params, cmds)


def build_conformance_spec(t: OverApproxTarget) -> S.CVLFile:
    """The conformance spec: imports Phi + (for an envfree target) an envfree decl of `f_sol` + the
    over-approx rule (real `f_sol`, no summary imported)."""
    rule = build_conformance_rule(t)
    blocks: list = []
    if _envfree(t):
        blocks.append(x.methods_block([x.m_envfree(t.cut, t.sig.name,
                                                   [p.type for p in t.sig.params], list(t.sig.returns))]))
    if rule is not None:
        blocks.append(rule)
    return x.spec_file(imports=[t.phi_import], contracts=(), blocks=blocks)
