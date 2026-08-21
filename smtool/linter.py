"""The discipline, as checkable invariants over a Project.

These back the *validation* enforcement layer: after any mutation, the project must
still satisfy every rule below, or the mutation is rejected. (The *constructive* layer
in mutations.py prevents most violations from ever being representable; the linter is
the backstop that also guards raw/body-level mutations.)
"""
from __future__ import annotations

import composer.cvl.schema as S
from . import walk


PURE_BUILTINS = {
    "require_uint256", "assert_uint256", "require_int256", "assert_int256",
    "to_mathint", "require_uint8", "assert_uint8",
}


def _is_reader_call(fa: S.FunctionApplication, readers: set[str]) -> bool:
    """True if `fa` is an unqualified call to a model reader (the model side of a glue equality)."""
    return fa.host_contract is None and fa.name in readers


def _has_reader(expr, readers: set[str]) -> bool:
    """True if `expr` contains a model-reader call anywhere (i.e. it references the model's side)."""
    return any(_is_reader_call(fa, readers) for fa in walk.calls(expr))


def _touches_state(expr, readers: set[str]) -> bool:
    """expr reads model or real state: a model reader, a CUT getter call (host set), or a
    setup-sourced getter (a CVL call that is neither a model reader nor a pure builtin)."""
    for fa in walk.calls(expr):
        if fa.host_contract is not None:
            return True
        if fa.name not in readers and fa.name not in PURE_BUILTINS:
            return True   # setup CVL getter (e.g. tokenBalanceOf)
        if fa.name in readers:
            return True   # model reader
    return False


def _is_gluing_equality(expr, readers: set[str]) -> bool:
    """A model==real equality: an `eq` with a model reader on EXACTLY ONE side (the other side
    is the real/getter/derived-local side). Catches disguised behavioral pins like
    `real == CONST` (no model reader) which have no model side."""
    if not (isinstance(expr, S.BinaryOp) and expr.operator == "eq"):
        return False
    return _has_reader(expr.left, readers) != _has_reader(expr.right, readers)


def _ghost_idents(node, ghost_names: set[str]) -> set[str]:
    """Ghost names appearing in `node` (quantifier vars / params filtered out by intersecting with the
    real ghost-name set)."""
    return {i.name for i in walk.iter_instances(node, S.Identifier)} & ghost_names


def lint_glued_ghost_freedom(project) -> list[str]:
    """SOUNDNESS (the deepest gate). The glue is a `require ghost == realGetter` — an ASSUMPTION that
    pins real state at a point where it is still free. It excludes real states unless the glued ghost
    can represent EVERY reachable real value, i.e. unless it is FREE at glue-time. So no axiom may
    constrain anything the glue's model side depends on — directly OR transitively (e.g. `F1 == a*RAY`
    + `RAY == 10^27` forces F1, hence the real getter, to a multiple of 10^27 → conformance passes
    VACUOUSLY). Such facts about real storage belong in a proved REACHABLE INVARIANT (which propagates
    to the ghost through the glue), never a model axiom.

    Crucially this bites only on the REQUIRE side (the glue's model-side readers). The model's
    transition/return computation (`<f>CVL` bodies, mirrors) is checked by ASSERTs, so it MAY use
    axiomatised ghosts freely — a bad axiom there makes the model diverge/revert and the conformance
    rule catches it. Hence we seed the pinned set ONLY from the glue-side readers, not the bodies.

    pinned P = (ghosts the glue-side readers transitively read, via the model-function call graph)
              closed under axiom co-occurrence (an axiom touching a pinned ghost pins every ghost it
              names). Any axiom referencing P is rejected."""
    spec = project.model_spec
    ghost_names = {b.ghost_name for b in spec.blocks if isinstance(b, S.GhostDef)}
    fns = {b.name: b for b in spec.blocks if isinstance(b, S.FunctionDef)}
    reads = {n: _ghost_idents(f.block, ghost_names) for n, f in fns.items()}
    calls = {n: {fa.name for fa in walk.calls(f.block) if fa.host_contract is None} & set(fns)
             for n, f in fns.items()}

    def transitive_reads(fn: str, seen: set[str]) -> set[str]:
        if fn in seen or fn not in fns:
            return set()
        seen.add(fn)
        out = set(reads[fn])
        for c in calls[fn]:
            out |= transitive_reads(c, seen)
        return out

    readers = {b.reader_name for b in project.cls.bindings}          # the glue's model-side functions
    pinned: set[str] = set()
    for r in readers:
        pinned |= transitive_reads(r, set())
    axioms = [(b.ghost_name, _ghost_idents(ax.exp, ghost_names) | {b.ghost_name})
              for b in spec.blocks if isinstance(b, S.GhostDef) for ax in b.axioms]
    changed = True                                                    # close over axiom co-occurrence
    while changed:
        changed = False
        for _, refs in axioms:
            if (refs & pinned) and not (refs <= pinned):
                pinned |= refs
                changed = True
    v: list[str] = []
    for owner, refs in axioms:
        hit = refs & pinned
        if hit:
            v.append(f"model ghost {owner} has an axiom constraining glued/pinned-to-real ghost(s) "
                     f"{sorted(hit)} — the glue fixes those to real storage, so this restricts the CUT "
                     f"and makes conformance vacuous; state it as a proved reachable invariant "
                     f"(add_requireInvariant), not a model axiom")
    return v


def lint_model_spec(project) -> list[str]:
    """The model spec's structural discipline: no methods{} block (it's self-contained), every ghost
    `persistent` (else a real-contract call havocs it), no model function calls a real contract
    (CVL-only — ghost reads/writes fine), and no setup imports (disjoint namespace)."""
    v: list[str] = []
    spec = project.model_spec
    for b in spec.blocks:
        if isinstance(b, S.MethodsBlock):
            v.append("model spec must have NO methods{} block (it is self-contained)")
        if isinstance(b, S.GhostDef) and not b.persistent:
            v.append(f"model ghost {b.ghost_name} must be `persistent` "
                     f"(else the real contract call havocs it)")
    # model functions are CVL-only: they may read/write ghosts, but must NOT call a real contract
    for b in spec.blocks:
        if isinstance(b, S.FunctionDef):
            for fa in walk.contract_calls(b):
                v.append(f"model function {b.name} calls contract {fa.host_contract}.{fa.name}; "
                         f"the model is CVL-only (no real-contract calls; ghost reads/writes are fine)")
            # SOUNDNESS: a model function is a reverting transition — it must revert via `if (cond) revert();`,
            # NOT `require`/`assert`. A `require` is an ASSUMPTION (prunes paths, doesn't revert) and would
            # break revert-conformance; an `assert` is an obligation that belongs in a conformance rule.
            for _ in walk.iter_instances(b, S.AssumeCmd):
                v.append(f"model function {b.name} uses `require` — model bodies must revert via "
                         f"`if (cond) revert();`, not require (require is an unsound assumption here)")
                break
            for _ in walk.iter_instances(b, S.AssertCmd):
                v.append(f"model function {b.name} uses `assert` — assertions belong in a conformance "
                         f"rule (add_helper_lemma), not in the model body")
                break
            # SOUNDNESS: a `require_uintN`/`require_intN` CAST assumes the value fits, silently PRUNING
            # out-of-range (overflow) inputs — the conformance then passes VACUOUSLY on exactly those
            # inputs (unsound; a require_* has no revert, so !realRev=>!modelRev never fires). Use the
            # `assert_*` cast so the prover CHECKS the cast is total: a provably-in-range one passes, an
            # overflowing one is CAUGHT. Model a genuine wrap explicitly (e.g. assert_uint256(x % 2^256)).
            for fa in walk.calls(b):
                nm = fa.name
                if nm.startswith("require_uint") or nm.startswith("require_int"):
                    v.append(f"model function {b.name} uses `{nm}` (a require_* cast) — it ASSUMES the "
                             f"value is in range, silently pruning out-of-range/overflow inputs and making "
                             f"the conformance vacuous there. Use `assert_{nm.split('_', 1)[1]}` instead "
                             f"(the prover checks the cast is total); model a real wrap explicitly if the "
                             f"value can exceed the type.")
                    break
    if spec.import_specs:
        v.append("model spec must not import setup specs (disjoint namespace)")
    return v


def lint_glue(project, method: str) -> list[str]:
    """The glue is DETERMINISTIC TEMPLATE (model==real correspondence, one equality per observable) — it
    is not agent-editable. This is the backstop: every `require` in it must be a model==real equality.
    ANY other require is rejected — a bare input/well-formedness pin unsoundly narrows the domain the
    model is proven against (a real revert precondition belongs in the model body's `if (cond) revert();`,
    covered by revert-conformance; a state fact belongs in a proved reachable invariant), and a
    requireInvariant belongs in assumeReachable, not the glue."""
    v: list[str] = []
    glue = project.find_glue(method)
    if glue is None:
        return [f"conformance[{method}] has no glue function"]
    readers = project.reader_names() | project.model_function_names()
    for cmd in glue.block.commands:
        if isinstance(cmd, S.AssumeCmd):
            if _is_gluing_equality(cmd.expression, readers):
                continue
            v.append(f'glue[{method}] has a non-correspondence require: "{cmd.message}". The glue is '
                     f"template (model==real equalities only). A real revert precondition goes in the "
                     f"model body's `if (cond) revert();` (revert-conformance covers it); a state fact "
                     f"goes in a proved reachable invariant — never a bare require in the glue.")
        elif isinstance(cmd, S.AssumeInvariantCmd):
            v.append(f"glue[{method}] contains a requireInvariant ({cmd.invariant_name}); "
                     f"reachability assumptions belong in assumeReachable (the reachable spec), "
                     f"not in the correspondence glue")
        # declarations / assignments (binding locals from getters) / returns are fine
    return v


def _lhs_root(lhs) -> str | None:
    """Root identifier of an assignment LHS (`g[i][j] = …` -> `g`)."""
    while isinstance(lhs, S.ArrayAccessLhs):
        lhs = lhs.base
    return lhs.name if isinstance(lhs, S.IdLhs) else None


def lint_model_state_coverage(project) -> list[str]:
    """SOUNDNESS: every OBSERVABLE ghost that a model `<f>CVL` WRITES — directly OR through a helper it
    calls — must be compared post-call by a state_effect assertion. Otherwise the model can set that
    ghost to anything, the conformance proof has no obligation on it, and a downstream proof reading the
    corresponding getter off the model reads a fabricated value — unsound replacement. Writes are traced
    through the model-function call graph because helpers (add_model_function) may also write ghosts."""
    v: list[str] = []
    cls = project.cls
    observable = {b.ghost_name for b in cls.bindings}
    covered = {b.ghost_name for b in cls.bindings if b.state_effect}
    fns = {b.name: b for b in project.model_spec.blocks if isinstance(b, S.FunctionDef)}
    direct_writes = {n: {_lhs_root(lhs) for asg in walk.iter_instances(f, S.AssignmentCmd)
                         for lhs in asg.left_hand_sides} & observable for n, f in fns.items()}
    calls = {n: {fa.name for fa in walk.calls(f) if fa.host_contract is None} & set(fns)
             for n, f in fns.items()}

    def transitive_writes(fn: str, seen: set[str]) -> set[str]:
        if fn in seen or fn not in fns:
            return set()
        seen.add(fn)
        out = set(direct_writes[fn])
        for c in calls[fn]:
            out |= transitive_writes(c, seen)
        return out

    for m in cls.model:
        fn = m.name + "CVL"
        if fn not in fns:
            continue
        for g in sorted(transitive_writes(fn, set()) - covered):
            v.append(f"model {fn} writes observable ghost {g} (directly or via a helper) but no "
                     f"state_effect assertion compares its post-value — unsound (the model could "
                     f"fabricate {g}); set state_effect=True on its getter so the rule pins it post-call")
    return v


def lint_reachable(project) -> list[str]:
    """The shared reachable spec: (a) every `requireInvariant` in `assumeReachable` must name an
    invariant declared in that same spec (no dangling / unproven assumption); (b) SOUNDNESS/proveability
    — every invariant body must be stated over the REAL CUT alone (real getters), NOT a model reader or
    model function. A reachable invariant is discharged against the real contract by the reachable proof;
    if it references model state (`mDrawnIndex == getAssetDrawnIndex`) the real CUT does not constrain
    that ghost, so it can NEVER verify — prune_reachable drops it and the assumption silently vanishes.
    Such a model↔real fact is the GLUE (a correspondence require), not a reachable invariant."""
    v: list[str] = []
    reach = getattr(project, "reachable", None)
    if reach is None:
        return v
    inv_names = {b.name for b in reach.blocks if isinstance(b, S.Invariant)}
    for aic in walk.iter_instances(reach, S.AssumeInvariantCmd):
        if aic.invariant_name not in inv_names:
            v.append(f"reachable: requireInvariant {aic.invariant_name} has no matching invariant "
                     f"declared in the reachable spec (dangling / unproven)")
    model_names = project.reader_names() | project.model_function_names()
    for inv in reach.blocks:
        if isinstance(inv, S.Invariant):
            hit = {fa.name for fa in walk.calls(inv.invariant_expression) if fa.name in model_names}
            if hit:
                v.append(f"reachable invariant {inv.name} references model function(s) {sorted(hit)} — a "
                         f"reachable invariant is proven of the REAL CUT alone, so it must be stated over "
                         f"REAL getters only (the real contract cannot constrain a model ghost, so this "
                         f"can never verify). A model==real fact belongs in the glue, not here.")
    return v


def lint(project) -> list[str]:
    """Run the whole discipline over a Project and return all violations ([] == clean). Called by
    _commit after every mutation, so a project is only ever accepted in a discipline-compliant state."""
    v = list(lint_model_spec(project))
    v += lint_glued_ghost_freedom(project)
    v += lint_model_state_coverage(project)
    v += lint_reachable(project)
    for m in project.conformance:
        v += lint_glue(project, m)
    return v
