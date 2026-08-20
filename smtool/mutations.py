"""Constrained mutation tools (component 2).

Each mutation:
  * takes TYPED inputs that cannot express a discipline violation (constructive layer), and
  * applies on a snapshot, then validates (structural + linter) and commits only if clean,
    otherwise rejects without touching the project (validation layer).

The discipline-critical one is add_requireInvariant (a requireInvariant is ALWAYS paired with a
real-CUT invariant the prover must discharge — never a bare assumption). The model==real glue
equalities are NOT a mutation: they're emitted correct-by-construction by the driver.
"""
from __future__ import annotations

import composer.cvl.schema as S
from composer.cvl.pretty_print import pretty_print

from . import cvlx as x
from . import walk
from .project import Project, Result
from .linter import lint
from . import driver


def _view_only_violations(project: Project, nodes: list) -> list[str]:
    """Every CONTRACT call inside `nodes` must be to a declared view/pure function. A CVL
    call (host None: model reader / builtin / pure mirror) is fine. Used to keep helper-lemma
    inputs side-effect-free — a capture like `x = readAndIncrement()` must be refused."""
    v: list[str] = []
    for n in nodes:
        for fa in walk.contract_calls(n):
            mut = project.function_mutability(fa.name)
            if mut is None:
                v.append(f"lemma input calls {fa.host_contract}.{fa.name} of unknown mutability; "
                         f"only declared view/pure getters may appear in a helper lemma")
            elif mut not in ("view", "pure"):
                v.append(f"lemma input calls state-changing {fa.host_contract}.{fa.name}; "
                         f"helper-lemma inputs must be view/pure only")
    return v


# ---------------------------------------------------------------- transaction
def _commit(project: Project, work: Project) -> Result:
    """The validation layer: given a mutated snapshot `work`, accept it into `project` ONLY if every
    spec (model + conformance + reachable) structurally re-validates + pretty-prints and the discipline
    linter is clean; otherwise reject and leave `project` untouched. This is what makes every mutation
    all-or-nothing and keeps the project always discipline-compliant."""
    # structural: pydantic re-validation + printable
    try:
        specs = [work.model_spec, *work.conformance.values()] + ([work.reachable] if work.reachable else [])
        for spec in specs:
            S.CVLFile.model_validate(spec.model_dump())
            pretty_print(spec)
    except Exception as e:
        return Result(False, f"structural validation failed: {type(e).__name__}: {e}")
    viol = lint(work)
    if viol:
        return Result(False, "rejected: discipline violation(s)", viol)
    project.model_spec = work.model_spec
    project.conformance = work.conformance
    project.confs = work.confs
    project.cls = work.cls
    project.reachable = work.reachable
    return Result(True, "applied")


def _insert_before_return(cmds: list, new_cmds: list) -> None:
    """Splice `new_cmds` in just before the first `return` (or append if none) — used to add
    requireInvariant / input pins into the glue body ahead of its return."""
    for i, c in enumerate(cmds):
        if isinstance(c, S.ReturnCmd):
            cmds[i:i] = new_cmds
            return
    cmds.extend(new_cmds)


def _idx_last_assert(cmds: list) -> int:
    """Index of the LAST assert in a rule (or end if none) — where a helper lemma's intermediate
    assert is inserted, so it precedes the rule's final MAIN assertion."""
    last = len(cmds)
    for i, c in enumerate(cmds):
        if isinstance(c, S.AssertCmd):
            last = i
    return last


def _idx_after_glue(cmds: list, glue_name: str) -> int:
    """Index just after the glue call in a rule (fallback: after the `env e;` decl) — where pre-SUT
    helper-lemma captures go. Exact match on the glue's actual name (resolved via Project.find_glue) —
    no prefix guessing, and not confused by the assumeReachable apply that now follows the glue."""
    for i, c in enumerate(cmds):
        # glue may be applied (void) or bound to a local (declaration whose init calls glue)
        if isinstance(c, S.ApplyCmd) and c.target.name == glue_name:
            return i + 1
        if isinstance(c, S.DeclarationCmd) and c.initial_value is not None:
            fa = getattr(c.initial_value, "application", None)
            if fa is not None and fa.name == glue_name:
                return i + 1
    return 1  # after the `env e;` declaration


def _idx_after_var(cmds: list, var: str) -> int:
    """Index just after the declaration of `var` (e.g. realRev), for post-SUT captures."""
    for i, c in enumerate(cmds):
        if isinstance(c, S.DeclarationCmd) and c.variable.id == var:
            return i + 1
    return _idx_last_assert(cmds)


# The glue's (ii) model==real equalities are NOT a mutation: they are fully determined by the
# bindings (reader, getter, args, env-ness) and emitted correct-by-construction by the driver
# (driver.build_glue). The only AI-driven glue content is (i) requireInvariant, below.


# ---------------------------------------------------------------- reachability (i): requireInvariant
def add_requireInvariant(project: Project, *, inv_name: str,
                         inv_params: list[tuple[str, str]], inv_expr,
                         require_args: list[str]) -> Result:
    """Create a real-CUT invariant AND requireInvariant it in the SHARED `assumeReachable`, atomically.

    The invariant is CUT-global (independent of which method we prove), so it lives once in the
    dedicated reachable spec and every conformance rule assumes it via `assumeReachable`. Idempotent:
    the same invariant requested by several methods lands once. A requireInvariant is ONLY introduced
    together with the invariant it names; that invariant is a proof obligation, so a model assumption
    can never enter as a bare, unproven require.
    NB soundness: the invariant is a CANDIDATE here — it must be DISCHARGED by the reachable conf
    (verify.prune_reachable keeps only prover-VERIFIED ones; check_consistency flags any still-assumed
    unproven invariant). TODO: source/refine candidates via composer's generate->prove->cex pass."""
    work = project.snapshot()
    reach = work.reachable
    if reach is None:
        return Result(False, "no reachable spec to hold the invariant")
    new_inv = x.invariant(inv_name, inv_params, inv_expr)
    existing = next((b for b in reach.blocks
                     if isinstance(b, S.Invariant) and b.name == inv_name), None)
    if existing is not None:
        reach.blocks[reach.blocks.index(existing)] = new_inv   # re-add REPLACES (fix a bad invariant)
    else:
        reach.blocks.append(new_inv)
    fn = work.find_func(reach, driver.ASSUME)
    if fn is None:
        return Result(False, f"no {driver.ASSUME} function in the reachable spec")
    # require_args go into the `requireInvariant inv(...)` call inside assumeReachable, so each must name
    # one of assumeReachable's own key params (the reachable key SLOTS). An arg that isn't a slot (e.g.
    # `e`, or a key type the model has no slot for) would be an undeclared identifier at typecheck — a
    # failure the agent can't localize. Reject it here with the available slots instead.
    slots = [p.id for p in fn.params]
    bad = [a for a in require_args if a not in slots]
    if bad:
        return Result(False, f"requireInvariant args {bad} are not reachable-key slots. assumeReachable "
                             f"exposes only {slots} — the invariant must be keyed by those. A fact over "
                             f"another key needs a matching key slot.")
    ri = x.require_invariant(inv_name, [x.ident(a) for a in require_args])
    if not any(c.model_dump() == ri.model_dump() for c in fn.block.commands):   # idempotent
        fn.block.commands.append(ri)
    return _commit(project, work)


def add_model_ghost_axiom(project: Project, *, ghost_name: str, axiom_expr, initial: bool = False) -> Result:
    """Add an `axiom` to a NON-glued model ghost (HOLE-A) — a definitional fact about model-internal
    state used only in the transition/return computation (asserted, so a wrong one is caught by
    conformance). The linter (lint_glued_ghost_freedom) REJECTS an axiom that constrains a glued /
    pinned-to-real ghost: that would restrict the CUT via the glue require and make conformance
    vacuous — state such facts as a proved reachable invariant (add_requireInvariant) instead."""
    work = project.snapshot()
    g = work.find_ghost(ghost_name)
    if g is None:
        return Result(False, f"no model ghost {ghost_name}")
    new_ax = x.axiom(axiom_expr, initial=initial)
    # idempotent: the SAME axiom (e.g. index>=RAY) is added once even if several methods request it
    if any(a.model_dump() == new_ax.model_dump() for a in g.axioms):
        return Result(True, f"axiom already present on {ghost_name}")
    g.axioms.append(new_ax)
    return _commit(project, work)


# ---------------------------------------------------------------- model bodies / helpers (HOLE-F/M)
def set_model_method_body(project: Project, *, method: str, commands: list) -> Result:
    """Fill <method>CVL's body. The body is CVL — it MAY write model ghosts (that's the state effect)
    and call model helpers; it may NOT call real-contract (Solidity) functions (linter enforces)."""
    work = project.snapshot()
    fn = work.find_func(work.model_spec, driver.model_fn_name(method))
    if fn is None:
        return Result(False, f"no model function {method}CVL")
    fn.block = S.CodeBlock(commands=list(commands))
    return _commit(project, work)


def add_model_function(project: Project, *, name: str, params: list[tuple[str, str]],
                       returns: list[str], commands: list) -> Result:
    """Add — or REPLACE — a model helper CVL function (HOLE-M): a math mirror or any internal helper the
    <f>CVL bodies call. CVL only: it MAY read/write model ghosts, but may NOT call real-contract functions
    (linter enforces). If it is reachable from the GLUE-side readers it additionally must not touch a
    glued/pinned ghost (lint_glued_ghost_freedom); called only from bodies (assert-side), it is free.

    Re-adding an EXISTING name: identical definition -> idempotent no-op (a shared mirror added once and
    reused across methods); DIFFERENT definition -> REPLACE it. The replace path is essential for the
    refine loop — it is how the agent FIXES a helper's body (e.g. a ceiling division that violated CVL's
    single-assignment rule); without it, a re-add would silently keep the old buggy body."""
    work = project.snapshot()
    new_fn = x.func(name, params, returns, commands)
    existing = work.find_func(work.model_spec, name)
    if existing is not None:
        if existing.model_dump() == new_fn.model_dump():
            return Result(True, f"model function {name} already present")   # idempotent shared re-add
        work.model_spec.blocks[work.model_spec.blocks.index(existing)] = new_fn   # replace = fix the body
    else:
        work.model_spec.blocks.append(new_fn)
    return _commit(project, work)


# ---------------------------------------------------------------- model constant (HOLE-K)
def add_model_constant(project: Project, *, name: str, ctype: str, value_expr) -> Result:
    """Add — or REPLACE — a `persistent ghost <ctype> <name> { axiom <name> == value; }` in the model.
    (Getter/methods declarations are NOT here — those are deterministic template output.)
    Re-adding the name: identical -> idempotent no-op (shared across methods); DIFFERENT type/value ->
    REPLACE it, so the agent can FIX a wrong constant (same rationale as add_model_function)."""
    work = project.snapshot()
    g = x.ghost_scalar(name, ctype, axioms=[x.axiom(x.binop("eq", x.ident(name), value_expr))])
    existing = work.find_ghost(name)
    if existing is not None:
        if existing.model_dump() == g.model_dump():
            return Result(True, f"model constant {name} already present")   # idempotent shared re-add
        work.model_spec.blocks[work.model_spec.blocks.index(existing)] = g    # replace = fix the value
    else:
        work.model_spec.blocks.insert(0, g)
    return _commit(project, work)


# ---------------------------------------------------------------- NONDET (HOLE-N)
def add_nondet(project: Project, *, method: str, contract: str | None, name: str,
               param_types: list[str], return_types: list[str], mutability: str,
               visibility: str = "external") -> Result:
    """Add a NONDET summary entry to the conformance methods{} block. The summary is fixed
    to NONDET — no other summary kind is expressible through this tool.

    SOUNDNESS: NONDET is sound ONLY for view/pure functions (it drops side effects, so
    NONDET-ing a state-changing function is unsound and the prover will NOT catch it). The tool
    refuses anything not view/pure, and cross-checks the claim against the compiled scene
    (`Project.scene_mutability`): with a scene wired it FAILS CLOSED (refuses unless the scene
    confirms view/pure); without one it trusts the caller's claim (the remaining gap). It also
    REFUSES any function of the CUT itself — the only valid NONDET targets are the method's off-path
    calls OUT to OTHER in-scene contracts (see below)."""
    # The CUT's own external functions are NEVER valid NONDET targets: in a conformance proof the only
    # calls to the CUT are the method under test and the glue / state-effect observable getters, which
    # MUST return the real value (that equality IS the model<->real correspondence). NONDET-ing one
    # breaks the glue (and, for an envfree getter, the typecheck). Refuse a CUT target whether named
    # concretely (contract == CUT) or reached by a `_.f` wildcard whose name also exists on the CUT.
    if contract == project.inp.cut or (contract in (None, "_")
                                       and name in {f.name for f in project.inp.functions}):
        return Result(False,
            f"refused: {name} is a function of the contract-under-test ({project.inp.cut}). Its calls in a "
            f"conformance proof are the method under test or the glue/state observable getters, which must "
            f"return the REAL value (that equality IS the model<->real correspondence) — NONDET would break "
            f"the glue. NONDET only OFF-PATH calls the method makes to OTHER in-scene contracts (oracle / "
            f"rate strategy). If {name} shows up in the difficulty report it is because glue/state "
            f"legitimately reads it; reduce that cost with a return-pivot lemma or congruence (instructions "
            f"section 4 b/c), not NONDET.")
    if mutability not in ("view", "pure"):
        return Result(False, f"refused: NONDET of {name} is sound only for view/pure "
                             f"functions, not '{mutability}' (it would drop side effects)")
    known = project.function_mutability(name)
    if known is not None and known not in ("view", "pure"):
        return Result(False, f"refused: {name} is state-changing ('{known}' per the inputs/scene); "
                             f"NONDET would be unsound")
    if known is None and project.scene_mutability is not None:
        return Result(False, f"refused: cannot confirm {name} is view/pure — it is not in the modeled "
                             f"inputs and not found in the scene; NONDET needs a confirmed view/pure target")
    work = project.snapshot()
    spec = work.conformance[method]
    mb = next((b for b in spec.blocks if isinstance(b, S.MethodsBlock)), None)
    if mb is None:
        mb = x.methods_block([])
        spec.blocks.insert(0, mb)
    # A CUT getter can never reach here (refused above), so there is no envfree-vs-summary idiom to
    # reconcile. A legitimate target is a call OUT to another in-scene contract: a concrete
    # `C.f(...) => NONDET` (a LINKED callee — the wildcard would not override its resolution), or a
    # `_.f => NONDET` wildcard for an unlinked callee.
    # a wildcard external entry (`_.f external => NONDET`) may NOT specify return types.
    rts = [] if (contract == "_" and visibility == "external") else return_types
    _same = lambda e: (isinstance(e.summary, S.HavocingSummary)                     # a prior NONDET for the
                       and e.signature.method_ref.method_name == name               # same (contract, name)
                       and e.signature.method_ref.contract == contract)             # -> replace (idempotent);
    mb.method_entries = [e for e in mb.method_entries if not _same(e)]              # keeps the envfree decl
    mb.method_entries.append(x.m_nondet(contract, name, param_types, rts, visibility))
    return _commit(project, work)


# ---------------------------------------------------------------- helper lemma (HOLE-P)
def add_helper_lemma(project: Project, *, method: str, rule_name: str,
                     captures: list | None = None, post_captures: list | None = None,
                     assert_expr=None, message: str = "helper lemma") -> Result:
    """Insert an intermediate ASSERT (a proof-decomposition lemma) into a rule, plus optional
    capture declarations: `captures` go right after the glue (pre-SUT); `post_captures` go right
    after the real call (post-SUT, e.g. idxAfter for accrue-idempotence). The tool builds an
    AssertCmd — never a require — so a helper lemma can only ADD a checked obligation, never an
    assumption; and every call in the captures/assert must be view/pure (see _view_only)."""
    work = project.snapshot()
    r = work.find_rule(method, rule_name)
    if r is None:
        return Result(False, f"no rule {rule_name} in conformance[{method}]")
    # soundness: the lemma's inputs (captures + assert) may only read state, never mutate it.
    check_nodes = list(captures or []) + list(post_captures or []) + ([assert_expr] if assert_expr is not None else [])
    vo = _view_only_violations(work, check_nodes)
    if vo:
        return Result(False, "refused: helper-lemma input is not view-only", vo)
    cmds = r.block.commands
    if post_captures:
        j = _idx_after_var(cmds, "realRev")
        cmds[j:j] = list(post_captures)
    if captures:
        glue = work.find_glue(method)
        i = _idx_after_glue(cmds, glue.name if glue else "")
        cmds[i:i] = list(captures)
    if assert_expr is not None:
        k = _idx_last_assert(cmds)
        cmds.insert(k, x.assert_(assert_expr, message))
    return _commit(project, work)


# ---------------------------------------------------------------- removals (retract a wrong guess)
# The property-directed moves (helper lemma, NONDET, input pin) are GUESSES that the prover may reject
# (a lemma whose assert doesn't hold VIOLATES; a NONDET the output actually depends on VIOLATES). The
# refine loop needs to RETRACT a wrong guess, so each such add has a matching remove. (Model bodies are
# already adjustable via set_model_method_body; reachable invariants via verify.prune_reachable.)
def remove_nondet(project: Project, method: str, *, name: str, contract: str | None = None) -> Result:
    """Remove the NONDET methods{} entry for function `name` (optionally scoped to `contract`) from a
    method's conformance. Use to retract a NONDET that turned out unsound — the checked output DID
    depend on the summarized function, so the conformance rule VIOLATED."""
    work = project.snapshot()
    spec = work.conformance[method]
    mb = next((b for b in spec.blocks if isinstance(b, S.MethodsBlock)), None)
    if mb is None:
        return Result(False, f"no methods block in conformance[{method}]")

    def _match(e) -> bool:
        ref = getattr(getattr(e, "signature", None), "method_ref", None)
        return (ref is not None and ref.method_name == name
                and (contract is None or ref.contract == contract))

    kept = [e for e in mb.method_entries if not _match(e)]
    if len(kept) == len(mb.method_entries):
        return Result(False, f"no NONDET entry for {name} in conformance[{method}]")
    mb.method_entries = kept
    return _commit(project, work)


def remove_model_ghost_axiom(project: Project, *, ghost_name: str, axiom_expr) -> Result:
    """Remove a definitional axiom (matched by its boolean expression, regardless of the init_state flag)
    from a NON-glued model ghost — the inverse of add_model_ghost_axiom. Use to retract a wrong axiom
    (a ghost holds a LIST of axioms, so this removes by expression rather than replacing)."""
    work = project.snapshot()
    g = work.find_ghost(ghost_name)
    if g is None:
        return Result(False, f"no model ghost {ghost_name}")
    kept = [a for a in g.axioms if a.exp.model_dump() != axiom_expr.model_dump()]
    if len(kept) == len(g.axioms):
        return Result(False, f"no axiom matching that expression on {ghost_name}")
    g.axioms = kept
    return _commit(project, work)


def remove_model_constant(project: Project, *, name: str) -> Result:
    """Retract a model constant/ghost the AGENT added via add_model_constant — the inverse: removes the
    WHOLE `persistent ghost <ctype> <name> { ... }` declaration (not just an axiom, which would orphan a
    bare colliding ghost). REFUSES the template's observable/binding ghosts (their value is fixed by the
    glue) — the agent may only remove what it itself added."""
    if name in {b.ghost_name for b in project.cls.bindings}:
        return Result(False, f"{name} is a template observable ghost (glue-pinned), not a removable constant")
    work = project.snapshot()
    g = work.find_ghost(name)
    if g is None:
        return Result(False, f"no model ghost/constant {name}")
    work.model_spec.blocks.remove(g)
    return _commit(project, work)


def remove_model_function(project: Project, *, name: str) -> Result:
    """Retract a model helper the AGENT added via add_model_function — the inverse: removes the whole
    FunctionDef. REFUSES the template functions — the per-binding readers and the `<method>CVL` method
    bodies — which are structural (not agent-added). The agent may only remove its own helpers/mirrors."""
    protected = project.reader_names() | {driver.model_fn_name(m.name) for m in project.cls.model}
    if name in protected:
        return Result(False, f"{name} is a template function (a reader or a <method>CVL body), not removable")
    work = project.snapshot()
    fn = work.find_func(work.model_spec, name)
    if fn is None:
        return Result(False, f"no model function {name}")
    work.model_spec.blocks.remove(fn)
    return _commit(project, work)


def remove_helper_lemma(project: Project, method: str, rule_name: str, *, message: str) -> Result:
    """Remove a helper lemma's ASSERT (matched by `message`) from a rule, and prune any capture
    declaration it leaves unreferenced. Use to retract a lemma whose assertion does not actually hold
    (it VIOLATED) — e.g. a preview-getter pivot that isn't exactly the method's return."""
    work = project.snapshot()
    r = work.find_rule(method, rule_name)
    if r is None:
        return Result(False, f"no rule {rule_name} in conformance[{method}]")
    msg = x._msg(message)
    kept = [c for c in r.block.commands if not (isinstance(c, S.AssertCmd) and c.message == msg)]
    if len(kept) == len(r.block.commands):
        return Result(False, f"no helper lemma with message {message!r} in rule {rule_name}")
    # prune capture declarations the lemma introduced that are now unreferenced (dead locals), fixpoint
    # so a chain of captures collapses. Only DeclarationCmds go — never the driver's rule bindings, which
    # stay referenced by the surviving asserts/glue.
    changed = True
    while changed:
        changed = False
        used = {i.name for c in kept for i in walk.iter_instances(c, S.Identifier)}
        pruned = [c for c in kept
                  if not (isinstance(c, S.DeclarationCmd) and c.variable.id not in used)]
        if len(pruned) != len(kept):
            kept, changed = pruned, True
    r.block.commands = kept
    return _commit(project, work)
