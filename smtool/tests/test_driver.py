"""Driver rendering: the deterministic skeleton (glue + return rule + state-effect rule), plus
regression tests for the void-return fix and the multi-return-getter load coalescing."""


def test_render_model_and_conformance(project, spec):
    pr = project(spec("f", [("uint256", "x")], ["uint256"], "nonpayable"),
                 spec("getX", [("uint256", "x")], ["uint256"], "view", envfree=True))
    m = pr.render_model()
    c = pr.render_conformance("f")
    assert "fCVL" in m                                   # model-method body stub for f
    assert "rule conformance_f_return" in c
    assert "rule conformance_f_stateEffect" in c         # state-changing -> gets a state-effect rule
    assert "env e" in c                                  # SpecialType env in the rule/glue
    assert "function glue" in c


def test_view_getter_declared_envfree(project, spec):
    pr = project(spec("f", [("uint256", "x")], ["uint256"], "nonpayable"),
                 spec("getX", [("uint256", "x")], ["uint256"], "view", envfree=True))
    c = pr.render_conformance("f")
    assert "function C.getX(uint256) external returns (uint256) envfree" in c


def test_void_method_has_no_return_rule(project, spec):
    # returns [] -> no `conformance_<m>_return` (nothing to compare) and never `() = call`
    pr = project(spec("act", [("uint256", "x")], [], "nonpayable"),
                 spec("getX", [("uint256", "x")], ["uint256"], "view", envfree=True))
    c = pr.render_conformance("act")
    assert "conformance_act_return" not in c
    assert "() =" not in c
    assert "rule conformance_act_stateEffect" in c       # revert-conformance still covered here


def test_multireturn_getter_is_coalesced(project, spec):
    # ONE getter (getPair) backing TWO observables (component 0 and 1) must LOAD ONCE per program point
    # and be DECLARED once in methods{} — not once per component.
    pr = project(
        spec("f", [("uint256", "x")], ["uint256"], "nonpayable"),
        spec("getPair", [("uint256", "x")], ["uint256", "uint256"], "view",
             envfree=True, bind_component=0, ghost_name="gA", reader_name="rA"),
        spec("getPair", [("uint256", "x")], ["uint256", "uint256"], "view",
             envfree=True, bind_component=1, ghost_name="gB", reader_name="rB"),
    )
    c = pr.render_conformance("f")
    # glue body + state-rule pre + state-rule post = 3 loads (would be 6 without coalescing)
    assert c.count("= getPair(") == 3
    # single methods{} declaration (would be 2 without the dedup)
    assert c.count("function C.getPair(") == 1
    # both components still pinned/asserted (rA against c0, rB against c1)
    assert "rA(" in c and "rB(" in c
