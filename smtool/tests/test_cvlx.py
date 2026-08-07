"""cvlx AST builders + the SpecialType CVL-IR extension (env/mathint/method/calldataarg).

These guard the composer.cvl.schema.SpecialType node smtool depends on — the exact thing that was
missing when smtool was checkpointed off master."""
from composer.cvl.pretty_print import pretty_print

from smtool import cvlx as x


def _render(*blocks):
    return pretty_print(x.spec_file(blocks=list(blocks)))


def test_ty_dispatches_special_vs_primitive():
    assert x.ty("env").type_name == "env"           # SpecialType
    assert x.ty("mathint").type_name == "mathint"   # SpecialType
    assert x.ty("uint256").type_name == "uint256"   # PrimitiveType (not special)


def test_special_types_render_in_a_function():
    # a CVL function with env + mathint params and a mathint return — pure SpecialType exercise
    f = x.func("g", [("env", "e"), ("mathint", "m")], ["mathint"], [x.ret([x.ident("m")])])
    txt = _render(f)
    assert "env e" in txt
    assert "mathint m" in txt


def test_ghost_and_rule_render():
    g = x.ghost_mapping("gh", "uint256", "uint256")
    r = x.rule("r", [("uint256", "k")],
               [x.assert_(x.binop("eq", x.ident("k"), x.ident("k")), "trivial")])
    txt = _render(g, r)
    assert "persistent ghost" in txt
    assert "rule r" in txt


def test_if_revert_renders_with_body():
    # regression: brace-less `if (c) revert();` must keep its then-branch (the milestone-7 _block bug)
    f = x.func("h", [("uint256", "b")], ["uint256"],
               [x.if_(x.binop("eq", x.ident("b"), x.num(0)), [x.revert()]),
                x.ret([x.ident("b")])])
    txt = _render(f)
    assert "revert()" in txt
    assert "if" in txt
