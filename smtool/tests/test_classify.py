"""Classification (MODEL vs OBSERVABLE) + default ghost/reader names."""
from smtool import classify


def test_default_names():
    assert classify.default_ghost_name("getX") == "getXCVL"
    assert classify.default_reader_name("getX") == "getXCVLReader"


def test_state_changing_and_computed_view_are_model_methods(project, spec):
    pr = project(
        spec("f", [("uint256", "x")], ["uint256"], "nonpayable"),          # state-changing -> model
        spec("getX", [("uint256", "x")], ["uint256"], "view", envfree=True),  # view -> observable
        spec("preview", [("uint256", "x")], ["uint256"], "view", model=True),  # computed view -> model
    )
    model_names = {m.name for m in pr.cls.model}
    getter_names = {g.name for g in pr.cls.getters}
    assert "f" in model_names
    assert "preview" in model_names          # model=True view is a model method (return-only)
    assert "getX" in getter_names
    assert "getX" not in model_names


def test_observable_getter_gets_a_binding(project, spec):
    pr = project(spec("f", [("uint256", "x")], ["uint256"], "nonpayable"),
                 spec("getX", [("uint256", "x")], ["uint256"], "view", envfree=True))
    binding_getters = {b.getter.name for b in pr.cls.bindings}
    assert "getX" in binding_getters
