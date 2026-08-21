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


def reverts_name(fn: str) -> str:
    """The REVERT-predicate (Ψ) function name for `fn` — `<fn>Reverts`. `Ψ(params)` is true exactly where
    the summary must revert; it is the dual of `Phi` (which constrains the RETURNED value on success)."""
    return fn + "Reverts"


def revert_rule_name(fn: str) -> str:
    """The revert-conformance rule name — `revertConform_<fn>` (dual of `overApprox_<fn>`)."""
    return "revertConform_" + fn


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
    psi_body: list | None = None          # the REVERT predicate Ψ(params) body — a boolean formula over
                                          # the params, true exactly where the summary must revert. None =>
                                          # the summary never reverts: still SOUND (summary-reverts ⊆
                                          # real-reverts = ∅), but COARSER — it hands back a value where the
                                          # real `f` would revert, so a consumer proof can explore an
                                          # impossible non-revert path (spurious CEX). A Ψ proven by
                                          # `revertConform_<fn>` (Ψ(x) => f reverts on x) makes the summary
                                          # revert where `f` does — a faithful, still-sound summary.
    result_name: str = "res"
    phi_spec_name: str | None = None      # file the Phi spec is written as / imported (default <fn>Phi.spec)
    psi_spec_name: str | None = None      # file the Ψ spec is written as / imported (default <fn>Reverts.spec)
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

    @property
    def psi_import(self) -> str:
        return self.psi_spec_name or (reverts_name(self.sig.name) + ".spec")


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


def _psi_params(t: OverApproxTarget) -> list:
    """Ψ's parameter list — just `f`'s params (a revert predicate over the inputs). Kept env-free like
    `Phi` (§ _phi_params): the predicate is a pure boolean formula over the params, so it typechecks
    standalone and reads the same in the summary guard and the conformance rule."""
    return [(p.type, p.name) for p in t.sig.params]


def build_psi(t: OverApproxTarget) -> S.FunctionDef:
    """`function <fn>Reverts(<params>) returns bool { <body> }`. Body defaults to `return false` (the
    never-revert stub — the current summary behavior) so the skeleton typechecks before Ψ is filled."""
    body = t.psi_body if t.psi_body is not None else [x.ret([x.boollit(False)])]
    return x.func(reverts_name(t.sig.name), _psi_params(t), ["bool"], body)


def build_psi_spec(t: OverApproxTarget) -> S.CVLFile:
    """The shared Ψ spec — just the revert predicate (optionally importing a model spec it reads)."""
    imports = [t.model_spec_import] if t.model_spec_import else ()
    return x.spec_file(imports=imports, blocks=[build_psi(t)])


def _idents(node) -> set:
    """Every identifier NAME appearing in a rendered (model_dump'd) expression/command tree."""
    out: set = set()
    if isinstance(node, dict):
        if node.get("type") == "identifier":
            out.add(node.get("name"))
        for v in node.values():
            out |= _idents(v)
    elif isinstance(node, list):
        for v in node:
            out |= _idents(v)
    return out


def lint_phi(t: OverApproxTarget) -> list:
    """SOUNDNESS guardrail: flag DOMAIN-RESTRICTING `require`s in Phi's body. A `require` whose condition
    touches NO fresh (havoc'd, no-initializer) local constrains only the params/result — a domain
    restriction. That is UNSOUND: in the conformance `assert !reverted => Phi(x, f(x))` the require makes
    the assert pass VACUOUSLY on the excluded inputs, and the installed `require Phi` then silently DROPS
    those real inputs, so the summary is no longer an over-approximation. The ONLY sanctioned `require`
    introduces/constrains a fresh WITNESS local (e.g. `uint248 v; require to_bytes31(v) == res;`), which
    references that local and is total over the domain. Syntactic heuristic (a require referencing a fresh
    local passes); the rigorous check is Phi-totality (a prover obligation — TODO). A genuine domain fact
    belongs in a proved reachable invariant, not a require in Phi."""
    if not t.phi_body:
        return []
    fresh = {c.model_dump()["variable"]["id"] for c in t.phi_body
             if c.model_dump().get("type") == "declaration" and c.model_dump().get("initial_value") is None}
    problems = []
    for c in t.phi_body:
        d = c.model_dump()
        if d.get("type") == "assume":                              # a `require` statement
            refs = _idents(d.get("expression"))
            if not (refs & fresh):
                problems.append(
                    f"Phi[{t.sig.name}]: a `require` constrains only params/result "
                    f"(uses {sorted(r for r in refs if r)}, no fresh witness local) — a DOMAIN RESTRICTION, "
                    "which is UNSOUND (it silently narrows the summary; the conformance would pass "
                    "vacuously). Fix by one of: (1) if it restates the callee's REVERT condition "
                    "(div-by-zero, insufficient balance), move it to the REVERT PREDICATE via set_psi "
                    "(`return c == 0;`) — the summary then reverts there exactly like `f` (faithful); "
                    "OMIT it only if that condition is inexpressible (never-revert is sound but coarser); "
                    "(2) if it is a result property, put it in Phi's `return` (which the conformance "
                    "ASSERTS), not a `require`; (3) if it is a genuine reachable fact, relocate it to a "
                    "proved requireInvariant. Only a fresh WITNESS-local require may stay (e.g. "
                    "`uint248 v; require to_bytes31(v) == res;`).")
    return problems


def lint_psi(t: OverApproxTarget) -> list:
    """SOUNDNESS guardrail for Ψ: the revert predicate is a PURE boolean formula over the params — the
    revert condition belongs in the `return` (e.g. `return c == 0;`), never in a `require`. A `require`
    inside Ψ would PRUNE inputs when Ψ is evaluated, defeating the point (and it has no witness-local
    idiom to justify it, unlike Phi). Flags any `require` in Ψ's body."""
    if not t.psi_body:
        return []
    problems = []
    for c in t.psi_body:
        if c.model_dump().get("type") == "assume":                 # a `require` statement
            problems.append(
                f"Psi[{t.sig.name}]: the revert predicate must be a PURE boolean over the params — state "
                "the revert condition in the `return` (e.g. `return c == 0 || bal < amt;`), never a "
                "`require` (a require in Ψ prunes inputs rather than modeling a revert).")
    return problems


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
    """The over-approximating summary body. When Ψ is present: `if (<fn>Reverts(params)) revert();` FIRST
    — the summary reverts exactly where `f` does (proven by `revertConform_<fn>`). Then havoc each result,
    `require Phi(params, res...)`, return the (tuple of) result(s). Takes a trailing `env e` param only for
    a non-envfree `f_sol`."""
    rets = list(t.sig.returns)
    resn = _names(t.result_name, len(rets))
    phi_args = [x.ident(p.name) for p in t.sig.params] + [x.ident(n) for n in resn]
    cmds: list = []
    if t.psi_body is not None:                                                  # revert where the real f reverts
        psi_args = [x.ident(p.name) for p in t.sig.params]
        cmds.append(x.if_(x.call(reverts_name(t.sig.name), psi_args),
                          [x.revert("over-approx: f reverts on these inputs")]))
    cmds += [x.declare(rt, n) for rt, n in zip(rets, resn)]                      # havoc each result
    cmds.append(x.require(x.call(phi_name(t.sig.name), phi_args), "over-approx: result satisfies Phi"))
    cmds.append(x.ret([x.ident(n) for n in resn]))                              # return res / (res0, res1, ...)
    params = [(p.type, p.name) for p in t.sig.params]
    if not _envfree(t):
        params = params + [("env", "e")]
    return x.func(summary_fn_name(t.sig.name), params, rets, cmds)


def build_summary_spec(t: OverApproxTarget) -> S.CVLFile:
    """The summary spec: imports Phi (+ Ψ when present), defines `<fn>CVL`, binds `C.f => <fn>CVL(...)`."""
    envfree = _envfree(t)
    call_args = [x.ident(p.name) for p in t.sig.params] + ([] if envfree else [x.ident("e")])
    call = x.call(summary_fn_name(t.sig.name), call_args)
    binding = x.m_expr_summary(t.cut, t.sig.name, [(p.type, p.name) for p in t.sig.params],
                               list(t.sig.returns), call, with_env=None if envfree else "e")
    imports = [t.phi_import] + ([t.psi_import] if t.psi_body is not None else [])
    return x.spec_file(imports=imports, contracts=(),
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


def build_revert_rule(t: OverApproxTarget) -> S.RuleBlock | None:
    """`revertConform_<fn>` (dual of `overApprox_<fn>`): call the real `f` and assert the summary reverts
    ONLY where `f` reverts — `assert Psi(params) => realReverted`. This is the SOUND direction (summary
    reverts ⊆ real reverts): a proven Ψ makes the summary revert where `f` does without ever dropping a
    real non-revert behavior. Returns None when Ψ is unset (the summary then never reverts — sound but
    coarser). Independent of the return arity (bare `f@withrevert` call, return value ignored), so it
    applies to multi-return and even void `f`."""
    if t.psi_body is None:
        return None
    params = [(p.type, p.name) for p in t.sig.params]
    psi_args = [x.ident(p.name) for p in t.sig.params]
    cmds: list = []
    if not _envfree(t):
        cmds.append(x.declare("env", "e"))
    cmds.append(x.apply(_call_real(t)))                                        # f@withrevert([e,] args...)
    cmds += [
        x.declare("bool", "realRev", x.ident("lastReverted")),
        x.assert_(
            x.binop("implies", x.call(reverts_name(t.sig.name), psi_args), x.ident("realRev")),
            "summary reverts only where the real function reverts (Psi => realReverted)",
        ),
    ]
    return x.rule(revert_rule_name(t.sig.name), params, cmds)


def build_conformance_spec(t: OverApproxTarget) -> S.CVLFile:
    """The conformance spec: imports the scene setup spec (if any, for the scene's aliases/summaries +
    the CUT declaration) + Phi (+ Ψ when present) + (for an envfree target) an envfree decl of `f_sol` +
    the over-approx value rule and, when Ψ is set, the revert-conformance rule (real `f_sol`, no summary
    imported)."""
    rule = build_conformance_rule(t)
    rrule = build_revert_rule(t)
    blocks: list = []
    if _envfree(t):
        blocks.append(x.methods_block([x.m_envfree(t.cut, t.sig.name,
                                                   [p.type for p in t.sig.params], list(t.sig.returns))]))
    if rule is not None:
        blocks.append(rule)
    if rrule is not None:
        blocks.append(rrule)
    imports = (([t.setup_spec_import] if t.setup_spec_import else []) + [t.phi_import]
               + ([t.psi_import] if t.psi_body is not None else []))
    return x.spec_file(imports=imports, contracts=(), blocks=blocks)
