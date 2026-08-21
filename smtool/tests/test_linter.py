"""Discipline linter: a freshly-generated skeleton is clean, and the glue (template model==real
equalities) has no discipline violations."""
from smtool import linter


def _proj(project, spec):
    return project(spec("f", [("uint256", "x")], ["uint256"], "nonpayable"),
                   spec("getX", [("uint256", "x")], ["uint256"], "view", envfree=True))


def test_lint_returns_a_list(project, spec):
    problems = linter.lint(_proj(project, spec))
    assert isinstance(problems, list)


def test_template_glue_is_discipline_clean(project, spec):
    # the glue is deterministic model==real equalities -> lint_glue must find nothing
    assert linter.lint_glue(_proj(project, spec), "f") == []


def test_skeleton_has_no_model_spec_violations(project, spec):
    # empty <f>CVL stubs + persistent ghosts + no setup imports -> lint_model_spec clean
    assert linter.lint_model_spec(_proj(project, spec)) == []
