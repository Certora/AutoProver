"""The deterministic driver (component 1): inputs -> skeleton CVL AST + conf.

Everything here is correct-by-construction. The parts the AI fills later (component 2, via the
mutation tools) are left as explicit HOLES:
  HOLE-K  constants     Named numeric constants the model math needs (e.g. RAY==10^27), emitted as a
                        `persistent ghost T c { axiom c == v; }`. Added by `add_model_constant`.
  HOLE-A  ghost axioms  Definitional facts about NON-glued, model-internal ghosts used in the
                        transition/return computation (asserted, so a wrong one is caught). A
                        restriction on a GLUED ghost is NOT allowed here — it would restrict the CUT
                        via the glue; state it as a reachable invariant instead (see §4 / lint_glued_ghost_freedom).
  HOLE-M  model helpers CVL helper functions the <f>CVL bodies call — typically math mirrors (a
                        library fn in its exact structural form so the return rule closes by
                        congruence), but any internal helper. CVL-only (no real-contract calls; may
                        read/write ghosts). Added by `add_model_function`.
  HOLE-F  <f>CVL body   The model method body: a permissive revert-guard, the π state effect (ghost
                        writes), and the return via the HOLE-M mirrors. Skeleton emits an unconstrained
                        return (obvious stub); filled by `set_model_method_body`.
  HOLE-N  per-method NONDET  view/pure functions to over-approximate as NONDET in this method's
                        conformance (a property-directed speedup). Skeleton emits none; added by `add_nondet`.
  HOLE-P  accrue-idempotence  An intermediate `assert` (+ view-only captures) decomposing an
                        accrue-sensitive return proof (e.g. index stable across the call). Added by `add_helper_lemma`.
The deterministic parts: the observable ghosts+readers (shape derived from the getter signature —
key types = params, value type = return, nothing to choose), <f>CVL signatures, glue (model==real)
equalities, `assumeReachable` calls, the two rule shapes, the methods-block envfree scaffold, and the
.conf rewrite. (WHICH getters are observable is an input/π choice, upstream of the driver, not a hole.)
"""
from __future__ import annotations

import copy

import composer.cvl.schema as S
from . import cvlx as x
from .ir import ToolInput, FunctionSpec, Binding, CUT_ARG, CALLER_ARG, FREE_PREFIX
from .classify import ModelLayout, classify


# ---------------------------------------------------------------- model spec
def _nested_ghost(b: Binding) -> S.GhostDef:
    """The persistent ghost that shadows a binding's real storage. Shape is derived from the getter
    signature: a scalar for a no-arg getter, else a nested `mapping(k0 => (k1 => ... => val))` keyed by
    the getter's param types (`key_types`) with the tracked return as the value (`val_type`). Emitted
    axiom-free — its value is fixed by the glue; see lint_glued_ghost_freedom."""
    if not b.key_types:
        return x.ghost_scalar(b.ghost_name, b.val_type)
    # build mapping(k0 => (k1 => ... => val)) right-to-left
    inner = x.prim(b.val_type)
    for kt in reversed(b.key_types):
        inner = x.mapping(kt, inner)
    return S.GhostDef(
        type="ghost_def", ghost_name=b.ghost_name, persistent=True,
        ghost_type=S.GhostVariable(type="ghost_type", base_type=inner), axioms=[],
    )


def _reader(b: Binding) -> S.FunctionDef:
    """The reader function for a binding's ghost — `<reader_name>(k0, k1, ...) { return ghost[k0][k1]; }`.
    A pure accessor so the model side of the glue reads like a getter call (`m_G(key)`) rather than a
    raw ghost index. This is the glue's model-side function (see lint_glued_ghost_freedom's seed)."""
    key_params = [(kt, f"k{i}") for i, kt in enumerate(b.key_types)]
    access = x.ident(b.ghost_name)
    for i in range(len(b.key_types)):
        access = x.idx(access, x.ident(f"k{i}"))
    return x.func(b.reader_name, key_params, [b.val_type], [x.ret([access])])


def _multi_getter_groups(cls: ModelLayout) -> dict:
    """FULLY-tracked multi-return CUT observable getters, grouped by getter name (components ordered).

    A multi-return getter (e.g. `getPair() -> (a, b)`) has one Binding per tracked
    component. A methods{} summary body must be a SINGLE call (no inline tuple), so exposing such a
    getter needs one COMBINED reader returning all components — which is only sound when EVERY component
    is tracked by a ghost. A partially-tracked multi-return getter (e.g. underlying+decimals, only the
    underlying tracked) is left to the real getter (immutable config), the pre-existing behavior."""
    groups: dict = {}
    for b in cls.bindings:
        g = b.getter
        if g.getter_host != "cut" or not g.declare_in_methods or not b.is_multi_return:
            continue
        groups.setdefault(g.name, []).append(b)
    full = {n: sorted(bs, key=lambda b: b.component_index)
            for n, bs in groups.items() if len(bs) == len(bs[0].getter.returns)}
    return full


def _combined_reader(getter_name: str, bs: list) -> S.FunctionDef:
    """`<getter>CVL(<params>) returns (<t0>, <t1>, ...) { return (ghost0[keys], ghost1[keys], ...); }` —
    the single reader the multi-return summary binds to, projecting each component ghost in order."""
    g = bs[0].getter
    key_params = [(p.type, p.name) for p in g.params]
    accesses = []
    for b in bs:
        acc = x.ident(b.ghost_name)
        for p in g.params:
            acc = x.idx(acc, x.ident(p.name))
        accesses.append(acc)
    return x.func(model_fn_name(getter_name), key_params, [b.val_type for b in bs], [x.ret(accesses)])


def _fcvl_stub(m: FunctionSpec) -> S.FunctionDef:
    """The skeleton `<f>CVL(address self, <params>, env e)` for a MODEL method — signature fixed, body a
    HOLE-F stub that just declares unconstrained result(s) and returns them (typechecks, obviously not
    filled). `set_model_method_body` replaces the body. The leading `self` param is the address the
    model acts as — the CUT address (currentContract) in conformance — passed at the call site."""
    params = [("address", "self")] + [(p.type, p.name) for p in m.params] + [("env", "e")]
    if not m.returns:
        return x.func(model_fn_name(m.name), params, [], [x.ret([])])
    # HOLE-F: declare unconstrained result(s) and return them -> typechecks, obviously a stub
    cmds, rvals = [], []
    for i, rt in enumerate(m.returns):
        rn = "result" if len(m.returns) == 1 else f"result{i}"
        cmds.append(x.declare(rt, rn))
        rvals.append(x.ident(rn))
    cmds.append(x.ret(rvals))
    return x.func(model_fn_name(m.name), params, list(m.returns), cmds)


def build_model_spec(inp: ToolInput, cls: ModelLayout) -> S.CVLFile:
    """The shared model spec (`Symbolic<CUT>Model.spec`): self-contained — no imports, no methods{},
    persistent ghosts + their readers + one <f>CVL stub per MODEL method."""
    blocks: list = []
    blocks += [_nested_ghost(b) for b in cls.bindings]
    blocks += [_reader(b) for b in cls.bindings]
    blocks += [_combined_reader(n, bs) for n, bs in _multi_getter_groups(cls).items()]
    blocks += [_fcvl_stub(m) for m in cls.model]
    return x.spec_file(imports=(), contracts=(), blocks=blocks)


# ---------------------------------------------------------------- conformance spec
def _cap(name: str) -> str:
    """Capitalize the first letter only (`draw` -> `Draw`), for `<CUT><Method>Conformance` filenames."""
    return name[:1].upper() + name[1:]


# The CUT is the `verify` target, i.e. `currentContract`; an unqualified CVL call resolves to it.
# So CUT calls need NO alias/host, and the CUT-as-address value is `currentContract`.
CURRENT = "currentContract"


def _cut_addr(inp) -> str:
    """CVL value for the CUT-as-address (model `self`, `CUT_ARG`): the using-ALIAS when the modeled
    contract is a dependency reached via alias (the consumer stays the verify target), else
    `currentContract` (the modeled contract IS the verify target)."""
    return inp.alias or CURRENT


def _cut_host(inp, getter=None):
    """Host for a CUT method/getter call: the alias when set, else None (unqualified -> currentContract).
    A setup-hosted getter is a CVL function -> always None regardless of alias."""
    if getter is not None and getter.getter_host != "cut":
        return None
    return inp.alias

# The single SHARED reachability function every conformance rule calls (after the glue) to assume the
# CUT's proven invariants. Lives in the dedicated reachable spec (build_reachable_spec), NOT in the
# per-method conformance spec — so `glue` stays the sole FunctionDef there. add_requireInvariant fills
# its body (idempotent across methods).
ASSUME = "assumeReachable"

# The correspondence function every conformance rule applies (model==real pinning). It's the SOLE
# FunctionDef in a conformance spec, so Project.find_glue identifies it structurally — this name is
# only the emit label. Kept as a constant so the emit site and the call site can't drift.
GLUE = "glue"


def model_fn_name(method: str) -> str:
    """The model function that stands in for CUT method `<method>` — `<method>CVL`. One place so the
    convention (and any future smt_ prefixing) stays consistent across driver/mutations/project."""
    return method + "CVL"


def build_summary_spec(inp: ToolInput, cls: ModelLayout) -> S.CVLFile:
    """The CONSUMER summary-application spec (`Symbolic<CUT>Summary.spec`): imports the model and, in a
    methods{} block, SUMMARIZES each real CUT function with its model counterpart so a downstream proof
    runs against the (trusted, conformance-verified) symbolic model instead of the heavy real CUT.

    - each MODEL method f -> `function CUT.f(<params>) external with (env e) => fCVL(currentContract,
      <params>, e) expect (<returns>);`
    - each OBSERVABLE single-return CUT getter -> `function CUT.g(<params>) external [with (env e)] =>
      <reader>(<params>) expect <val>;`  (so the consumer reads MODEL state, kept consistent with the
      model's writes).
    Multi-return getters (config observables the model doesn't write, e.g. underlying+decimals) and
    setup-hosted getters are left to the real getter — sound because they're immutable config the model
    never mutates; only the single-return observables the methods WRITE need model readers. Apply by
    adding `import "Symbolic<CUT>Summary.spec";` to the consumer's spec (only AFTER conformance passes)."""
    entries = []
    for m in cls.model:
        call = x.call(model_fn_name(m.name),
                      [x.ident(_cut_addr(inp)), *[x.ident(p.name) for p in m.params], x.ident("e")])
        entries.append(x.m_expr_summary(inp.cut, m.name, [(p.type, p.name) for p in m.params],
                                        list(m.returns), call, with_env="e"))
    for b in cls.bindings:
        g = b.getter
        if g.getter_host != "cut" or not g.declare_in_methods or len(g.returns) != 1:
            continue   # setup getters (modeled elsewhere) / multi-return config getters -> real getter
        call = x.call(b.reader_name, [x.ident(p.name) for p in g.params])
        entries.append(x.m_expr_summary(inp.cut, g.name, [(p.type, p.name) for p in g.params],
                                        [b.val_type], call, with_env=None if g.effective_envfree else "e"))
    # multi-return observable getters (fully tracked): bind to the combined reader (single-call body)
    for name, bs in _multi_getter_groups(cls).items():
        g = bs[0].getter
        call = x.call(model_fn_name(name), [x.ident(p.name) for p in g.params])
        entries.append(x.m_expr_summary(inp.cut, name, [(p.type, p.name) for p in g.params],
                                        list(g.returns), call, with_env=None if g.effective_envfree else "e"))
    return x.spec_file(imports=[inp.model_spec], contracts=(), blocks=[x.methods_block(entries)])


def _reachable_keys(cls: ModelLayout) -> list[tuple[str, str]]:
    """The ORDERED, DISTINCT key slots `assumeReachable` exposes — one per key TYPE the model's reachable
    invariants may range over, so an invariant keyed by ANY of them (not only the address) can be
    requireInvariant'd. Slot 1 (when present) is the state-effect ADDRESS frame var: a reachable
    invariant is universal over accounts, so assuming it for the ARBITRARY compared account covers a
    multi-account method (e.g. `transferFrom` credits `to`). The remaining slots are the DISTINCT
    non-address observable key types (e.g. `uint256 id`), named after the getter's key param so the
    agent can pass them by name. Each conformance rule fills the address slot from its framed/fresh
    account and the other slots from the METHOD's matching param — so a per-key invariant is assumed
    for the key actually under test (a per-key bound the model needs to discharge cast-safety, which
    a SINGLE address key could not express). Falls back to the leading model-method param when there are
    no observables (a return-only method with no getters)."""
    slots: list[tuple[str, str]] = []
    seen: set[str] = set()
    for b in cls.bindings:                        # address slot: the state-effect frame var (old single key)
        if not b.state_effect:
            continue
        for fa in b.frame_arg_names:
            if fa.startswith(FREE_PREFIX):
                _, ty, var = fa.split(":", 2)
                if ty == "address":
                    slots.append((ty, var)); seen.add(ty); break
        if seen:
            break
    for b in cls.bindings:                        # one slot per remaining DISTINCT observable key type
        for i, kt in enumerate(b.key_types):
            if kt not in seen:
                seen.add(kt)
                nm = b.getter.params[i].name if i < len(b.getter.params) else kt
                slots.append((kt, nm))
    if not slots:
        p = cls.model[0].params[0]
        slots.append((p.type, p.name))
    return slots


def _reachable_call_cmds(cls: ModelLayout, m: FunctionSpec, keys: list[tuple[str, str]],
                         framed_names: set[str]) -> tuple[list, object]:
    """`([decls], assumeReachable(args))` for a conformance rule. Each key slot is filled by: a framed
    var of that name when the rule already declares one (state-effect framing); else — for a NON-address
    key — the METHOD's param of that type (same-name preferred, e.g. `id`), so a per-key invariant
    is assumed for the key under test; else a FRESHLY declared var (the address slot in a rule with no
    framing — universal over accounts). A method's own address param (e.g. a transfer `to`) is never
    used for the address slot; that slot stays the framed/fresh universal account."""
    decls: list = []
    args: list = []
    for ty, name in keys:
        if name in framed_names:
            args.append(x.ident(name)); continue
        chosen = None
        if ty != "address":
            chosen = next((p.name for p in m.params if p.name == name and p.type == ty), None) \
                or next((p.name for p in m.params if p.type == ty), None)
        if chosen is not None:
            args.append(x.ident(chosen))
        else:
            decls.append(x.declare(ty, name)); args.append(x.ident(name))
    return decls, x.apply(x.call(ASSUME, args))


def build_reachable_spec(inp: ToolInput, cls: ModelLayout) -> S.CVLFile:
    """The dedicated SHARED spec: envfree decls for the invariant-support (non-observable) getters +
    an initially-empty `assumeReachable(<key>)` function. The CUT invariants + their `requireInvariant`s
    are added by add_requireInvariant (best-effort; only prover-VERIFIED ones are kept — that
    proof/prune runs against <CUT>Reachable.conf, separate from the conformance runs).
    TODO(reuse): populate/refine these invariants via composer's generate->prove->cex pass
    (see composer/spec/source/struct_invariant.py) instead of hand-supplied ones."""
    # Declare EVERY effective-envfree getter here (observable AND non-observable support), deduped by
    # (name, arity). Invariants live in this shared spec and reference REAL getters — including
    # observable ones (e.g. a per-key balance bound) — so those getters must be declared HERE, or the
    # standalone reachable PROOF conf resolves them against the scene's real (non-envfree) signature and
    # fails ("missing environment parameter"). Conformance specs import this spec and therefore do NOT
    # re-declare these getters (build_conformance_spec skips them), so there is no duplicate.
    seen: set = set()
    decl_getters = []
    for g in cls.getters:
        if not (g.effective_envfree and g.declare_in_methods):
            continue
        key = (g.name, len(g.params))
        if key not in seen:
            seen.add(key); decl_getters.append(g)
    blocks: list = []
    if decl_getters:
        blocks.append(x.methods_block([x.m_envfree(inp.cut, g.name, [p.type for p in g.params], g.returns)
                                       for g in decl_getters]))
    blocks.append(x.func(ASSUME, _reachable_keys(cls), [], []))   # empty; filled by add_requireInvariant
    return x.spec_file(imports=(), blocks=blocks)


def _resolve_arg(inp: ToolInput, name: str):
    """A glue/frame arg name -> Expression. `CUT_ARG` is the CUT address (`currentContract`);
    `CALLER_ARG` is the calling account (`e.msg.sender`, requires `env e` in scope — true in the glue
    fn and the stateEffect rule); else a plain identifier (a method param or a glue-local like `u`)."""
    if name == CUT_ARG:
        return x.ident(_cut_addr(inp))
    if name == CALLER_ARG:
        return x.field(x.field(x.ident("e"), "msg"), "sender")
    return x.ident(name)


def _getter_call(inp: ToolInput, b: Binding, arg_names: list[str]):
    """The real-getter call for a binding's glue equality — `getter([e,] args...)`, prepending `env e`
    if the getter is env-taking (`envful`). Called UNQUALIFIED (host=None): a CUT getter resolves to
    currentContract; a setup-sourced getter is a CVL function. (A CUT getter is never NONDET-summarized —
    add_nondet refuses CUT functions — so its concrete `envfree` decl always governs these spec reads.)"""
    args = ([x.ident("e")] if b.envful else []) + [_resolve_arg(inp, a) for a in arg_names]
    return x.call(b.getter.name, args, host=_cut_host(inp, b.getter))


def _group_by_getter(bindings: list, args_of) -> list:
    """Group bindings that read the SAME real getter with the SAME args (key = `(name, arg-names)`), so a
    multi-return getter backing several observables (e.g. `getPair() -> (a, b)`, one observable per
    component) is LOADED ONCE and each component pinned/asserted separately — instead of a full load
    per component (redundant external calls + extra prover work). Preserves first-seen order.
    `args_of(b)` = the binding's arg-name list (glue_arg_names for the glue, frame_arg_names for the rule).
    The group's component locals come from `group[0].component_names` (bindings in a group agree on the
    getter, so component_index selects the right one)."""
    order: list = []
    groups: dict = {}
    for b in bindings:
        key = (b.getter.name, tuple(args_of(b)))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(b)
    return [groups[k] for k in order]


def build_glue(inp: ToolInput, cls: ModelLayout, m: FunctionSpec) -> S.FunctionDef:
    """Glue = (ii) model==real equalities, correct-by-construction from the bindings. Handles
    setup-sourced getters, multi-return getters (loaded ONCE, each component pinned — see
    _group_by_getter), derived keys (glue_args referencing a local like `u`), and an optional return of
    a designated component local."""
    params = [(p.type, p.name) for p in m.params]
    array_params = {p.name for p in m.params if p.type.endswith("[]")}
    cmds: list = []
    ret_local, ret_type = None, []
    for group in _group_by_getter(cls.bindings, lambda b: b.glue_arg_names):
        b0 = group[0]
        if any(a in array_params for a in b0.glue_arg_names):
            continue                                  # array-keyed observable: pinned per-element by _field_pins
        args = [_resolve_arg(inp, a) for a in b0.glue_arg_names]
        call = x.call(b0.getter.name, ([x.ident("e")] if b0.envful else []) + args, host=_cut_host(inp, b0.getter))
        if b0.is_multi_return:
            names = b0.component_names
            for cn, ct in zip(names, b0.getter.returns):
                cmds.append(x.declare(ct, cn))
            cmds.append(x.assign_multi(names, call))                 # ONE load; shared across the group
            value = lambda b, names=names: x.ident(names[b.component_index])
        else:
            value = lambda b, call=call: call
        for b in group:
            reader = x.call(b.reader_name, args)
            cmds.append(x.require(x.binop("eq", reader, value(b)), f"glue: model == real for {b.getter.name}"))
            if b.glue_return and b0.is_multi_return:
                ret_local, ret_type = names[b.component_index], [b.val_type]
    if ret_local:
        cmds.append(x.ret([x.ident(ret_local)]))
    return x.func(GLUE, params + [("env", "e")], ret_type, cmds)


def _glue_returns(cls: ModelLayout):
    """The type the glue function returns — the val_type of the binding flagged `glue_return` (the
    derived local, e.g. the underlying `u`, that the rule reuses), or None if the glue is void."""
    for b in cls.bindings:
        if b.glue_return:
            return b.val_type
    return None


def _call_real(inp: ToolInput, m: FunctionSpec, withrevert=True):
    """Call the REAL CUT method — `f(e, args...)`, unqualified (resolves to currentContract),
    `@withrevert` so the rule can inspect `lastReverted` for revert-conformance."""
    args = [x.ident("e")] + [x.ident(p.name) for p in m.params]
    return x.call(m.name, args, host=_cut_host(inp), annotation="withrevert" if withrevert else None)


def _call_model(inp: ToolInput, m: FunctionSpec, withrevert=True):
    """Call the MODEL method — `fCVL(currentContract, args..., e)`, `@withrevert`. The leading
    arg is the CUT address for `<f>CVL`'s `self` param; the `env e` goes last (model convention)."""
    args = [x.ident(_cut_addr(inp))] + [x.ident(p.name) for p in m.params] + [x.ident("e")]
    return x.call(model_fn_name(m.name), args, annotation="withrevert" if withrevert else None)


def _glue_apply(inp: ToolInput, cls: ModelLayout, m: FunctionSpec, bind_to: str | None = None):
    """Emit the glue call at the top of a rule — `glue(args..., e);`, or `<type> bind_to = glue(...);`
    when the glue returns a local the rule needs (e.g. `u`)."""
    args = [x.ident(p.name) for p in m.params] + [x.ident("e")]
    call = x.call(GLUE, args)
    if bind_to is not None:
        return x.declare(_glue_returns(cls), bind_to, call)
    return x.apply(call)


def _revert_conf(inp: ToolInput):
    """The revert-conformance assert. DEFAULT (`precise_reverts=False`) = OVER-APPROXIMATION: real
    success => model success — the model may be MORE permissive (revert LESS), a sound coarsening that
    lets a model soundly ignore e.g. access-control reverts unreachable in the consumer. `precise_reverts`
    switches to EXACT: `realRev == modelRev`, forbidding any over-approximation (the model must revert
    exactly like real)."""
    if inp.precise_reverts:
        return x.assert_(x.binop("eq", x.ident("realRev"), x.ident("modelRev")),
                         "model must revert exactly when real reverts (precise)")
    return x.assert_(x.binop("implies", x.unop_not(x.ident("realRev")), x.unop_not(x.ident("modelRev"))),
                     "real success must imply model success (over-approximation)")


def build_return_rule(inp: ToolInput, cls: ModelLayout, m: FunctionSpec,
                      reachable_keys: list[tuple[str, str]] | None = None) -> S.RuleBlock | None:
    """`conformance_<f>_return`: glue + assumeReachable, then call real and model `@withrevert` and
    assert return agreement WHEN BOTH SUCCEED. The revert conformance (`realRev == modelRev`) is asserted
    in the state-effect rule for a state changer (de-dup), or HERE for a return-only method. Single vs
    multi-return handled separately; multi compares only the `return_compare`-flagged components.
    Returns None for a VOID method (no return to compare — `() = call` is not valid CVL): its
    revert-conformance is asserted by the state-effect rule (which every state-changing method gets)."""
    if not m.returns:
        return None
    params = [(p.type, p.name) for p in m.params]
    cmds: list = [x.declare("env", "e"), _glue_apply(inp, cls, m)]
    cmds += _field_pins(inp, cls, m, [])   # conservative typed model==real pins (no frame vars here)
    # assumeReachable over the SHARED key slots (`reachable_keys`, from the whole-model layout). The
    # return rule has no framing, so the address slot is declared FRESH; the per-key slot reuses the
    # method's own `id` param (in scope as a rule param). The keys MUST be the shared ones — the
    # per-method `_reachable_keys(cls)` can differ (e.g. a preview with no address observable), which
    # would mismatch the shared `assumeReachable(...)` declaration — a typecheck conflict the agent
    # cannot fix (it reads as "overload assumeReachable / cast address<->uint256").
    keys = reachable_keys or _reachable_keys(cls)
    rdecls, rassume = _reachable_call_cmds(cls, m, keys, framed_names=set())
    cmds += [*rdecls, rassume]
    # HOLE-P: previewBefore capture + A assertion go here when the return is accrue-sensitive.
    single = len(m.returns) == 1
    # Revert conformance (realRev == modelRev) is asserted by the STATE-EFFECT rule for a state changer;
    # a return-only method (no state-effect rule) carries it here. Return agreement is checked only when
    # BOTH sides succeed (a revert mismatch is the state-effect / here-only revert assert's job).
    both_ok = x.binop("and", x.unop_not(x.ident("realRev")), x.unop_not(x.ident("modelRev")))
    revert_assert = [] if m.is_state_changing else [_revert_conf(inp)]
    if single:
        cmds += [
            x.declare(m.returns[0], "retSol", _call_real(inp, m)),
            x.declare("bool", "realRev", x.ident("lastReverted")),
            x.declare(m.returns[0], "retModel", _call_model(inp, m)),
            x.declare("bool", "modelRev", x.ident("lastReverted")),
            *revert_assert,
            x.assert_(x.binop("implies", both_ok, x.binop("eq", x.ident("retSol"), x.ident("retModel"))),
                      "returns must agree when both succeed"),
        ]
    else:
        # multi-return: bind both tuples, compare the flagged components (others over-approximated).
        real_ns = [f"retSol{i}" for i in range(len(m.returns))]
        model_ns = [f"retModel{i}" for i in range(len(m.returns))]
        for rt, n in zip(m.returns, real_ns):
            cmds.append(x.declare(rt, n))
        cmds.append(x.assign_multi(real_ns, _call_real(inp, m)))
        cmds.append(x.declare("bool", "realRev", x.ident("lastReverted")))
        for rt, n in zip(m.returns, model_ns):
            cmds.append(x.declare(rt, n))
        cmds.append(x.assign_multi(model_ns, _call_model(inp, m)))
        cmds.append(x.declare("bool", "modelRev", x.ident("lastReverted")))
        cmds += revert_assert
        compare = m.return_compare or [True] * len(m.returns)
        for i, (rn, mn) in enumerate(zip(real_ns, model_ns)):
            if compare[i]:
                cmds.append(x.assert_(x.binop("implies", both_ok,
                            x.binop("eq", x.ident(rn), x.ident(mn))),
                            f"return component {i} must agree when both succeed"))
    return x.rule(f"conformance_{m.name}_return", params, cmds)


def _frame_resolve(inp: ToolInput, name: str):
    """A frame arg -> (decl_or_None, Expression). A `FREE_PREFIX` arg ("FREE:<type>:<var>") declares a
    fresh free var (framing over all such values); otherwise resolves like a glue arg."""
    if name.startswith(FREE_PREFIX):
        _, ty, var = name.split(":", 2)
        return x.declare(ty, var), x.ident(var)
    return None, _resolve_arg(inp, name)


def _group_load(inp, group: list, args: list, suffix: str) -> tuple[list, "callable"]:
    """Load a getter shared by `group` (same getter+args) ONCE at the current point; return
    (decls, value) where value(b) is the tracked scalar for binding b. A multi-return getter binds its
    components to fresh `<component>_<suffix>` locals (suffix disambiguates the pre vs post read); a
    single-return getter needs no decls and value is the call itself. Coalesces the per-component
    double-load (see _group_by_getter)."""
    b0 = group[0]
    call = x.call(b0.getter.name, ([x.ident("e")] if b0.envful else []) + args, host=_cut_host(inp, b0.getter))
    if not b0.is_multi_return:
        return [], (lambda b, call=call: call)
    names = [f"{cn}_{suffix}" for cn in b0.component_names]
    decls = [x.declare(ct, n) for n, ct in zip(names, b0.getter.returns)]
    decls.append(x.assign_multi(names, call))
    return decls, (lambda b, names=names: x.ident(names[b.component_index]))


def _numeric_ty(ty: str) -> bool:
    return ty == "mathint" or ty.startswith("uint") or ty.startswith("int")


def _is_udvt(ty: str) -> bool:
    """A user value type (e.g. Token.Id) — not a CVL primitive. It wraps a number, and the
    readers coerce it into a numeric key slot (exactly as the model body already does)."""
    if ty in ("address", "bool", "string", "bytes", "mathint") or _numeric_ty(ty) or ty.startswith("bytes"):
        return False
    return True


def _key_matches(var_ty: str, key_ty: str, coercible: frozenset[str] = frozenset()) -> bool:
    """May an in-scope var of `var_ty` fill a mapping key of `key_ty`? Exact match, or — for a numeric
    key — a numeric var, or a var of a KNOWN coercible (UDVT) type. `coercible` is the model's own
    non-primitive KEY types: a UDVT `type Id is uint256` legitimately coerces into a numeric key, but a
    non-primitive is only trusted to coerce when the model actually uses it AS a key. A struct method
    param (e.g. a `Lib.Info`) is never a mapping key, so it is NOT coercible — without this it was pinned
    as a `uint256` key (`readerCVL(info)`), an uncatchable-by-agent typecheck error."""
    if var_ty == key_ty:
        return True
    if _numeric_ty(key_ty):
        return _numeric_ty(var_ty) or var_ty in coercible
    return False


# TODO(perf): this pins the WHOLE allowed set unconditionally (safe default, zero agent burden, but it
# over-pins — every pin is a live getter call, a keccak slot read on assembly tokens). Over-pinning is
# only a PERFORMANCE cost, never a soundness one (more `model==real` pre-pins can't hide a divergence).
# Optimization to consider IF timing shows the pins cost: expose this set as a deterministic ALLOWED
# MENU and let the agent add pins from it SELECTIVELY (in response to an unpinned-read counterexample),
# instead of emitting all upfront. Sound because the menu is sound-by-construction (the agent can only
# pick a valid pin) and a MISSING needed pin surfaces as a VIOLATION, not a silent pass (self-correcting;
# relies on the require-cast lint having removed the vacuity path). Needs a constrained `add_pin` tool +
# a precise "real succeeded but model read unpinned cell X" message (the agent once mis-read this exact
# gap as an auth problem). Alternative with no agent rounds: deterministic BODY-SCAN (pin exactly the
# reader-calls in the filled body) — same precision, but requires rebuilding the glue on body change.
def _field_pins(inp: ToolInput, cls: ModelLayout, m: FunctionSpec, frame_vars: list) -> list:
    """Conservative model==real PRE-pins for every single-return observable FIELD, at the TYPED cross
    product of the in-scope vars for each key position (method params + e.msg.sender + `frame_vars`).
    Body-free and over-pinning: pinning `ghost[k] == getter(k)` at more keys only strengthens the
    'model starts equal to real' premise, and it guarantees every key the model READS is pinned — so
    the model can't diverge on an unpinned (havoc'd) cell (e.g. a transfer's credit-side balance).
    Scalars pin once. A key position with no matching in-scope var yields no combo for that field; the
    per-frame pin (kept alongside) remains its fallback."""
    import itertools
    # scope: (key_type, key_EXPR). Scalars contribute their identifier; an ARRAY param T[] contributes
    # its bounded elements arr[0..loop_iter) as element-typed keys (CVL has no loops, so arrays are
    # addressed by fixed indices up to the run's loop_iter). NOT the frame free vars: those are pinned
    # by the per-frame pre-pin (kept alongside).
    _ = frame_vars
    scope: list = []
    for p in m.params:
        if p.type.endswith("[]"):
            elem = p.type[:-2]
            for k in range(inp.loop_iter):
                scope.append((elem, x.index(p.name, k)))
        else:
            scope.append((p.type, x.ident(p.name)))
    scope.append(("address", _resolve_arg(inp, CALLER_ARG)))
    # A non-primitive type is trusted to coerce into a numeric key only when the model uses it as a
    # scalar INDEX: either a declared observable KEY type, or an ARRAY-ELEMENT type (arr[i] indexing an
    # observable). Both are UDVTs (`type Id is uint256`) by construction. A bare non-primitive SCALAR
    # method param (e.g. a struct `Lib.Info`) is NOT an index — excluding it stops it
    # being pinned as a `uint256` key, an uncatchable-by-agent typecheck error. (Residual: an array of
    # STRUCTS would still be trusted; not a shape the models use — the model keys are always scalars.)
    coercible = frozenset(
        [kt for b in cls.bindings for kt in b.key_types if _is_udvt(kt)]
        + [p.type[:-2] for p in m.params if p.type.endswith("[]") and _is_udvt(p.type[:-2])])
    def vars_of(kt: str) -> list:
        return [e for (ty, e) in scope if _key_matches(ty, kt, coercible)]
    cmds: list = []
    seen: set = set()
    for b in cls.bindings:
        if b.is_multi_return:
            continue                                  # multi-return: covered by the frame pin (TODO)
        kts = b.key_types
        combos = [()] if not kts else itertools.product(*[vars_of(kt) for kt in kts])
        for combo in combos:
            key = (b.reader_name, tuple(str(e.model_dump()) for e in combo))
            if key in seen:
                continue
            seen.add(key)
            argexprs = list(combo)
            reader = x.call(b.reader_name, argexprs)
            getter = x.call(b.getter.name, ([x.ident("e")] if b.envful else []) + argexprs,
                            host=_cut_host(inp, b.getter))
            cmds.append(x.require(x.binop("eq", reader, getter), f"pin: model == real for {b.getter.name}"))
    return cmds


def _frame_free_vars(cls: ModelLayout) -> list:
    """The distinct FREE frame vars (type, name) declared across the bindings' frame args."""
    out, seen = [], set()
    for b in cls.bindings:
        for fa in b.frame_arg_names:
            if fa.startswith(FREE_PREFIX):
                _, ty, var = fa.split(":", 2)
                if var not in seen:
                    seen.add(var); out.append((ty, var))
    return out


def build_state_effect_rule(inp: ToolInput, cls: ModelLayout, m: FunctionSpec,
                            reachable_keys: list[tuple[str, str]] | None = None) -> S.RuleBlock:
    """After the call, EVERY observable's post-state must agree model==real — the WHOLE pi, by default
    (safety-by-default: an effect the model gets wrong is caught). Each observable is framed (free vars
    for the keys it ranges over), pinned pre (model == real) and asserted post. Observables opt OUT via
    state_effect=False (e.g. ones not yet provable whole).
    TODO(perf): narrow the checked set to the observables a method actually WRITES (from a per-method
    write-set analysis) instead of all of pi — a future optimization; checking everything is safe but
    each extra observable is prover work (and may need its own lemma, e.g. accrue-idempotence)."""
    params = [(p.type, p.name) for p in m.params]
    se = [b for b in cls.bindings if b.state_effect]
    ret = _glue_returns(cls)
    cmds: list = [x.declare("env", "e")]
    cmds.append(_glue_apply(inp, cls, m, bind_to="u" if ret else None))
    # free-var framing decls + pre-pins, GROUPED so a getter shared by several observables loads once
    framed: list = []
    for group in _group_by_getter(se, lambda b: b.frame_arg_names):
        b0 = group[0]
        args, decls = [], []
        for a in b0.frame_arg_names:
            d, expr = _frame_resolve(inp, a)
            if d is not None:
                decls.append(d)
            args.append(expr)
        cmds += decls
        gdecls, value = _group_load(inp, group, args, "pre")
        cmds += gdecls
        for b in group:
            reader = x.call(b.reader_name, args)
            cmds.append(x.require(x.binop("eq", reader, value(b)), f"pin pre: model == real for {b.getter.name}"))
        framed.append((group, args))
    # conservative typed pinning: model==real for every field at the cross product of in-scope vars
    # per key type (subsumes the single frame pre-pin; pins the keys the model reads, e.g. a credit `to`).
    cmds += _field_pins(inp, cls, m, _frame_free_vars(cls))
    # assume the CUT reachable invariants over the SHARED key slots (matches the `assumeReachable(...)`
    # declaration). The address slot reuses the FRAMED account (covers the arbitrary compared account,
    # incl. a multi-account method's credit target); the per-key slot uses the method's own `id`
    # (so a per-key bound is assumed for the key under test). Any slot not already framed is declared
    # fresh (sound: an invariant is universal, so assuming it for an arbitrary extra key is harmless).
    keys = reachable_keys or _reachable_keys(cls)
    framed_names = {fa.split(":", 2)[2] for b in cls.bindings if b.state_effect
                    for fa in b.frame_arg_names if fa.startswith(FREE_PREFIX)}
    rdecls, rassume = _reachable_call_cmds(cls, m, keys, framed_names)
    cmds += [*rdecls, rassume]
    cmds += [x.apply(_call_real(inp, m)), x.declare("bool", "realRev", x.ident("lastReverted")),
             x.apply(_call_model(inp, m)), x.declare("bool", "modelRev", x.ident("lastReverted")),
             _revert_conf(inp)]
    for group, args in framed:
        gdecls, value = _group_load(inp, group, args, "post")
        cmds += gdecls
        for b in group:
            reader = x.call(b.reader_name, args)
            cmds.append(x.assert_(x.binop("implies", x.unop_not(x.ident("realRev")), x.binop("eq", value(b), reader)),
                                  f"observable {b.getter.name} effect must agree"))
    return x.rule(f"conformance_{m.name}_stateEffect", params, cmds)


def build_conformance_spec(inp: ToolInput, cls: ModelLayout, m: FunctionSpec,
                           setup_spec_import: str | None = None, declared=None,
                           reachable_spec_import: str | None = None,
                           reachable_keys: list[tuple[str, str]] | None = None) -> S.CVLFile:
    """Assemble one method's conformance spec: imports [setup, reachable, model] + a methods{} block
    (envfree decls for the observable getters, minus what the setup already summarizes) + the glue +
    the return rule + the state-effect rule."""
    # Import the ONE setup spec (its whole closure — imports + methods{} — is resolved by CVL), the
    # shared reachable spec (assumeReachable + CUT invariants), and the shared model.
    # `setup_spec_import` comes from the setup .conf (smtool.setup.consume_setup); None only for
    # standalone/offline builds that don't wire the setup.
    imports = ([setup_spec_import] if setup_spec_import else []) \
        + ([reachable_spec_import] if reachable_spec_import else []) + [inp.model_spec]
    # methods{}: envfree decls for OBSERVABLE getters that want declaring (invariant-support /
    # non-observable getters are declared in the reachable spec instead; setup CVL getters like
    # tokenBalanceOf already exist, so declare_in_methods=False). NONDET: HOLE-N.
    # RECONCILE: skip an entry the setup's resolved closure already summarizes (`declared` =
    # smtool.resolved_ast.summarized_methods; keyed by (name, arity)). Plain-decl clashes the resolved
    # AST can't show are left to the reactive typecheck fallback (TODO).
    declared = set(declared or ())
    # When the reachable spec is imported it already declares every effective-envfree getter (so its
    # invariants resolve) — skip them here to avoid a duplicate declaration across the import.
    if reachable_spec_import:
        declared |= {(g.name, len(g.params)) for g in cls.getters
                     if g.effective_envfree and g.declare_in_methods}
    entries = []
    for g in cls.getters:
        if not (g.observable and g.effective_envfree and g.declare_in_methods):
            continue
        key = (g.name, len(g.params))
        if key in declared:                # skip setup-summarized AND repeated (multi-component) getters
            continue
        declared.add(key)
        entries.append(x.m_envfree(inp.cut, g.name, [p.type for p in g.params], g.returns))
    blocks: list = []
    if entries:
        blocks.append(x.methods_block(entries))
    blocks.append(build_glue(inp, cls, m))
    return_rule = build_return_rule(inp, cls, m, reachable_keys)   # None for a void method (see build_return_rule)
    if return_rule is not None:
        blocks.append(return_rule)
    # A computed VIEW model method (model=True on a view) has no state effect — only a return rule.
    # Its inputs' storage is kept real by the state-effect rules of the methods that WRITE them.
    if m.is_state_changing:
        blocks.append(build_state_effect_rule(inp, cls, m, reachable_keys))
    return x.spec_file(imports=imports, contracts=(), blocks=blocks)


# ---------------------------------------------------------------- reachable proof (prove-incrementally)
def build_reachable_proof_spec(inp: ToolInput, setup_spec_import: str | None,
                               invariant_names: list[str]) -> S.CVLFile:
    """The verify target that PROVES the shared reachable invariants against the CUT — imports the
    setup (scene), the reachable spec (invariant decls + support getters), and the model (shared
    constants like RAY), and `use invariant`s each one so it's checked. Run its conf ONCE; the
    conformance runs then only assume the VERIFIED invariants. Prove-incrementally: adding a new
    invariant (add_requireInvariant) + re-running this conf extends the proven set; drop any that
    don't verify (best-effort — TODO: automate the prune via smtool.verify)."""
    imports = ([setup_spec_import] if setup_spec_import else []) + [inp.reachable_spec, inp.model_spec]
    return x.spec_file(imports=imports, blocks=[x.use_invariant(n) for n in invariant_names])


def rewrite_reachable_conf(setup_conf: dict, inp: ToolInput, invariant_names: list[str]) -> dict:
    """The conf that PROVES the reachable invariants: the setup conf (scene) with verify pointed at
    <CUT>ReachableProof.spec and `rule` = the invariant names. Run once; feeds verify.prune_reachable."""
    conf = copy.deepcopy(setup_conf)
    # ALIAS path: the modeled contract is a dependency; the CONSUMER stays the verify target (so the
    # imported setup spec's unqualified consumer methods resolve, and the invariant — stated over the
    # alias — is proven in the same scene as the conformance). Non-alias: the modeled contract IS the CUT.
    verify_target = setup_conf["verify"].split(":", 1)[0] if inp.alias else inp.cut
    conf["verify"] = f"{verify_target}:{inp.specs_dir}/{inp.cut}ReachableProof.spec"
    conf["msg"] = f"{inp.cut} reachable invariants"
    conf["rule"] = list(invariant_names)
    conf["multi_assert_check"] = True
    return conf


# ---------------------------------------------------------------- conf rewrite
def invariant_names(spec: S.CVLFile) -> list[str]:
    """Names of the invariants declared in `spec`."""
    return [b.name for b in spec.blocks if isinstance(b, S.Invariant)]


def verifiable_names(spec: S.CVLFile) -> list[str]:
    """The rules + invariants we want VERIFIED in a spec's own run. For conformance specs this is just
    the conformance rules (the reachable invariants live in the reachable spec and are proven by
    <CUT>Reachable.conf, not here); the invariant term stays for specs that DO declare invariants."""
    return ([b.rule_name for b in spec.blocks if isinstance(b, S.RuleBlock)]
            + invariant_names(spec))


def rewrite_conf(setup_conf: dict, inp: ToolInput, m: FunctionSpec,
                 conformance_spec: S.CVLFile | None = None) -> dict:
    """One method's conformance conf: the setup conf (scene inherited untouched) with verify -> this
    method's conformance spec, a CUT-derived msg, multi_assert_check on, and `rule` = just this spec's
    conformance rules (via verifiable_names on the post-mutation spec)."""
    conf = copy.deepcopy(setup_conf)
    spec = f"{inp.specs_dir}/{inp.conformance_prefix}{_cap(m.name)}Conformance.spec"
    # ALIAS path: the modeled contract is a dependency (reached via alias); the CONSUMER stays the verify
    # target (from the setup conf), so the conformance runs in exactly the consumer scene. Non-alias:
    # the modeled contract IS the verify target (inp.cut).
    verify_target = setup_conf["verify"].split(":", 1)[0] if inp.alias else inp.cut
    conf["verify"] = f"{verify_target}:{spec}"
    conf["msg"] = f"{inp.conformance_prefix} {m.name} conformance"
    conf["multi_assert_check"] = True
    # rule-filter (complete-by-construction): run exactly our rules, so the setup's imported `sanity`
    # rule is defined-but-not-run. The shared reachable invariants are NOT listed here — they're
    # proven separately by <CUT>Reachable.conf (prove-once) and only ASSUMED via requireInvariant.
    if conformance_spec is not None:
        conf["rule"] = verifiable_names(conformance_spec)
    # The conformance spec imports the setup spec directly (build_conformance_spec's
    #   `setup_spec_import`), so the setup's whole closure — imports + methods{} — is inherited and the
    #   sanity rule is simply not in this conf's `rule` list. Our own methods{} entries are reconciled
    #   against the setup's RESOLVED summarized set (smtool.resolved_ast, via -printAst) so we don't
    #   re-add a method the setup already summarizes.
    # TODO(reconcile): plain envfree-DECL clashes aren't in the resolved AST's summary lists — add the
    #   reactive fallback (typecheck -> drop on "duplicate declaration", reusing
    #   certora_autosetup/typechecker_loop.py's error parsing) for those.
    # TODO(names): prefix everything smtool introduces with `smt_` (+ optional deterministic nonce
    #   for recursive modeling), EXCEPT the CUT-method signatures in methods{} (must match the ABI).
    # TODO(perf): one conformance spec per method is inefficient — a model change forces re-verifying
    #   all previously-passing rules. Group methods that share the same NONDET set into one spec/conf.
    return conf
