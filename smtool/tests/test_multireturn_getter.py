"""A multi-return observable getter must generate DISTINCT per-component ghosts + readers.

Regression for a `getPair() -> (uint256, int256)` bug: both components defaulted to the same
`<getter>CVL` name, so the model spec redeclared the ghost / overloaded the reader — an uncatchable
typecheck error (the agent has no tool to edit a glue-pinned template ghost). Each component must get
its own ghost/reader; the combined tuple reader projects both.
"""
from smtool.ir import ToolInput, FunctionSpec, Param as P
from smtool.project import Project
from composer.cvl.pretty_print import pretty_print


def _model_text(*fns):
    pr = Project.from_method_specs([ToolInput(cut="C", functions=list(fns))], None, None)
    return pretty_print(pr.model_spec)


def _pair_getter(component):
    # getPair(uint256 id) -> (uint256, int256); one observable per tracked component
    return FunctionSpec.of("getPair", [P("uint256", "id")], ["uint256", "int256"], "view",
                           envfree=True, bind_component=component)


def test_multireturn_components_get_distinct_ghosts():
    # a state-changer so the getters are observables of something (model needs >=1 model method)
    m = FunctionSpec.of("poke", [P("uint256", "id")], [], "nonpayable")
    txt = _model_text(m, _pair_getter(0), _pair_getter(1))
    # distinct component ghosts, no collision
    assert "getPair_0CVL" in txt and "getPair_1CVL" in txt
    assert "mapping(uint256 => uint256) getPair_0CVL" in txt.replace("\n", " ") or "getPair_0CVL;" in txt
    # the combined tuple reader reads BOTH distinct ghosts
    ghost_decls = [l for l in txt.splitlines() if l.strip().startswith("persistent ghost") and "getPairCVL;" in l]
    assert not ghost_decls, "no ghost may be named exactly getPairCVL (that's the combined reader)"


def test_no_duplicate_declarations():
    m = FunctionSpec.of("poke", [P("uint256", "id")], [], "nonpayable")
    txt = _model_text(m, _pair_getter(0), _pair_getter(1))
    # collect every declared ghost + function name; none may repeat
    names = []
    for l in txt.splitlines():
        s = l.strip()
        if s.startswith("persistent ghost"):
            names.append(s.rstrip("{;").split()[-1].rstrip(";"))
        elif s.startswith("function "):
            names.append(s.split("(")[0].split()[-1])
    dups = {n for n in names if names.count(n) > 1}
    assert not dups, f"duplicate declarations (redeclaration typecheck error): {dups}"


# ---- reachable-key type consistency (second driver bug) --------------------------------------------
def test_assumeReachable_call_matches_declared_key_type():
    """The return rule's `assumeReachable(...)` must pass a var of the DECLARED key type. When the
    reachable key is an address (a state-effect frame var) but the method's first param is NOT an
    address (e.g. a uint256 id), passing `m.params[0]` blindly is a type mismatch the agent can't fix."""
    from smtool.ir import ToolInput, FunctionSpec, Param as P, free_var
    from smtool.project import Project
    from composer.cvl.pretty_print import pretty_print
    # a method whose FIRST param is uint256 (id), + an address-keyed observable -> address key
    m = FunctionSpec.of("act", [P("uint256", "id"), P("uint256", "amt"), P("address", "to")],
                        ["uint256"], "nonpayable")
    bal = FunctionSpec.of("balByAccount", [P("address", "acct")], ["uint256"], "view", envfree=True,
                          ghost_name="balCVL", reader_name="mBal",
                          frame_args=[free_var("address", "a")])
    pr = Project.from_method_specs([ToolInput(cut="C", functions=[m, bal])], None, None)
    reach = pretty_print(pr.reachable)
    decl = [l for l in reach.splitlines() if "function assumeReachable" in l][0]
    # the return rule must NOT call assumeReachable(id) (uint256); it declares+passes the key var
    ret = pretty_print(pr.conformance["act"])
    assert "assumeReachable(id)" not in ret, "return rule passes the wrong (uint256) key"
    # the reachable key is the address frame var (here named `a`); the return rule declares + passes it
    assert "address a" in decl, "reachable key should be the address frame var"
    assert "address a;" in ret and "assumeReachable(a" in ret, \
        "return rule must declare + pass an address var matching the declaration"


# ---- MULTI-KEY reachable slots across the WHOLE-model union (third + fifth driver bugs) -------------
def _union_project():
    from smtool.ir import ToolInput, FunctionSpec, Param as P, free_var
    from smtool.project import Project
    act = FunctionSpec.of("act", [P("uint256", "id"), P("uint256", "amt"), P("address", "to")],
                          ["uint256"], "nonpayable")
    bal = FunctionSpec.of("balByAccount", [P("address", "acct")], ["uint256"], "view", envfree=True,
                          ghost_name="balCVL", reader_name="mBal", frame_args=[free_var("address", "a")])
    # preview: computed view, uint256 first param, observables keyed uint256 only (no address)
    prev = FunctionSpec.of("preview", [P("uint256", "id"), P("uint256", "amt")],
                           ["uint256"], "view", model=True)
    sh = FunctionSpec.of("getBal", [P("uint256", "id")], ["uint256"], "view", envfree=True)
    # state-changer with NO address observable
    touch = FunctionSpec.of("touch", [P("uint256", "id"), P("uint256", "amt")], [], "nonpayable")
    return Project.from_method_specs([
        ToolInput(cut="C", functions=[act, bal]),
        ToolInput(cut="C", functions=[prev, sh]),
        ToolInput(cut="C", functions=[touch, sh]),
    ], None, None)


def test_union_reachable_exposes_a_slot_per_key_type():
    """`assumeReachable` must expose ONE slot per DISTINCT key type the model's invariants can range over
    — the address frame var AND the `uint256 id` observable key — so a PER-KEY invariant can be
    requireInvariant'd (a single address slot could not host it). Every conformance rule fills the
    address slot with `a` (framed/fresh) and the id slot with the method's own `id`, both matching the
    declared types."""
    from composer.cvl.pretty_print import pretty_print
    pr = _union_project()
    decl = [l for l in pretty_print(pr.reachable).splitlines() if "function assumeReachable" in l][0]
    # multi-key declaration: address slot first, then the uint256 id slot
    assert "assumeReachable(address a, uint256 id)" in decl, decl
    for name, spec in pr.conformance.items():
        t = pretty_print(spec)
        for s in (l.strip() for l in t.splitlines()):
            if "assumeReachable(" in s and not s.startswith("function"):
                # the id slot is the method's own id; the address slot is a (framed or fresh)
                assert s == "assumeReachable(a, id);", f"{name}: bad reachable call {s!r}"


def test_perkey_invariant_can_be_added_via_requireInvariant():
    """The fix's PURPOSE: a per-key (`id`-keyed) reachable invariant — e.g. to bound real storage so a
    model's mathint->uint256 casts match the real fixed-width fields — must be expressible. Regression
    for the case where the agent got stuck: `requireInvariant balBound(id)` inside `assumeReachable` was
    an undeclared-identifier typecheck error because the single address slot had no `id`."""
    from smtool import mutations, cvlx as cx
    from composer.cvl.pretty_print import pretty_print
    pr = _union_project()
    # add a per-key invariant keyed by the id slot
    r = mutations.add_requireInvariant(
        pr, inv_name="balBound", inv_params=[("uint256", "id")],
        inv_expr=cx.binop("le", cx.call("getBal", [cx.ident("id")]), cx.num(2**120 - 1)),
        require_args=["id"])
    assert r.ok, r.message
    reach = pretty_print(pr.reachable)
    assert "requireInvariant balBound(id)" in reach, reach
    # a NON-slot arg is rejected with an actionable message (not a silent typecheck failure)
    bad = mutations.add_requireInvariant(
        pr, inv_name="bogus", inv_params=[("uint256", "id")],
        inv_expr=cx.binop("le", cx.call("getBal", [cx.ident("id")]), cx.num(1)),
        require_args=["e"])
    assert not bad.ok and "not reachable-key slots" in bad.message, bad.message


# ---- struct method-param must not be pinned as a scalar key (fourth driver bug) --------------------
def test_struct_param_not_pinned_as_scalar_key():
    """`_field_pins` cross-products the method params against each observable's key types. A STRUCT param
    (e.g. `act(uint256, Lib.Info)`) is not a valid mapping key, yet the old `_is_udvt` blocklist treated
    every non-primitive as coercible -> it emitted `readerCVL(info)` against a `uint256`-keyed reader, an
    uncatchable-by-agent typecheck error. Only a type the model actually uses AS a key (a real UDVT) may
    coerce into a numeric key."""
    from smtool.ir import ToolInput, FunctionSpec, Param as P
    from smtool.project import Project
    from composer.cvl.pretty_print import pretty_print

    act = FunctionSpec.of("act", [P("uint256", "id"), P("Lib.Info", "info")], [], "nonpayable")
    idx = FunctionSpec.of("getIdx", [P("uint256", "id")], ["uint256"], "view", envfree=True)
    pr = Project.from_method_specs([ToolInput(cut="C", functions=[act, idx])], None, None)
    t = pretty_print(pr.conformance["act"])
    bad = [l.strip() for l in t.splitlines() if "info" in l and "CVLReader(" in l]
    assert not bad, f"struct param pinned as a scalar key (typecheck error): {bad}"
    # the legit uint256 id key is still pinned (the fix must not over-prune)
    assert "getIdxCVLReader(id)" in t, "valid uint256-key pin was lost"
