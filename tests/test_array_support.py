"""smtool array support: array-param (T[]) methods are modeled by UNROLLING to the run's loop_iter.

CVL has no loops/recursion, so a batch method is unrolled over its bounded length: the conformance pins
the array-keyed observable at the fixed elements arr[0..loop_iter-1] (via _field_pins), the malformed
scalar glue pin for an array key is SKIPPED, and the agent is told the loop_iter so it can unroll the
body. cvlx's array-access / .length builders must also produce exactly the CVL parser's AST.
"""
from smtool.ir import ToolInput, FunctionSpec, Param as P, free_var, CALLER_ARG
from smtool.project import Project
from smtool.agent.refine import _array_guidance
from smtool.cvl_parse import parse_expression
from smtool import cvlx as x
from composer.cvl.pretty_print import pretty_print

PID = "Vault.TokenId"   # a generic user-defined value type (UDVT)


def _bal(glue):
    return FunctionSpec.of("balanceOf", [P("address", "owner"), P("uint256", "id")], ["uint256"], "view",
                           envfree=True, ghost_name="posBalCVL", reader_name="mPosBal",
                           glue_args=glue, frame_args=[free_var("address", "a"), free_var("uint256", "i")])


def _batch_project(loop_iter=3):
    spec = ToolInput(cut="C", alias="c", functions=[
        FunctionSpec.of("batchBurn", [P(PID + "[]", "_positionIds"), P("uint256[]", "_amounts")], [],
                        "nonpayable"),
        _bal([CALLER_ARG, "_positionIds"]),
    ])
    return Project.from_method_specs([spec], None, None, loop_iter=loop_iter)


# ---- cvlx builders match the real parser ------------------------------------
def test_cvlx_index_matches_parser():
    assert x.index("_positionIds", 2).model_dump() == parse_expression("_positionIds[2]").model_dump()


def test_cvlx_length_matches_parser():
    assert x.length("_positionIds").model_dump() == parse_expression("_positionIds.length").model_dump()


def test_array_type_parses():
    assert x.ty("uint256[]").type == "dyn_array"
    assert x.ty("Vault.TokenId[]").base_type.type == "contract_type"


# ---- conformance: element pins + skipped glue -------------------------------
def test_element_pins_at_loop_iter():
    txt = pretty_print(_batch_project(loop_iter=3).conformance["batchBurn"])
    # the array-keyed observable is pinned at each element 0..loop_iter-1
    for k in range(3):
        assert f"_positionIds[{k}]" in txt
    assert "_positionIds[3]" not in txt      # loop_iter is exclusive upper bound


def test_glue_skips_array_key():
    # glue() body is empty — a scalar pin over an array key is malformed, so it is skipped (elements
    # are pinned by _field_pins instead).
    txt = pretty_print(_batch_project().conformance["batchBurn"])
    glue = txt.split("function glue(")[1].split("}")[0]
    assert "mPosBal" not in glue


def test_loop_iter_threads():
    assert _batch_project(loop_iter=2).inp.loop_iter == 2
    txt = pretty_print(_batch_project(loop_iter=2).conformance["batchBurn"])
    assert "_positionIds[1]" in txt and "_positionIds[2]" not in txt


# ---- agent guidance ----------------------------------------------------------
def test_array_guidance_fires_for_batch():
    g = _array_guidance(_batch_project(loop_iter=3))
    assert "loop_iter is 3" in g and "batchBurn" in g and "arr[0..2]" in g


def test_array_guidance_empty_for_scalar():
    spec = ToolInput(cut="C", alias="c", functions=[
        FunctionSpec.of("burn", [P(PID, "_positionId"), P("uint256", "_amount")], [], "nonpayable"),
        _bal([CALLER_ARG, "_positionId"]),
    ])
    assert _array_guidance(Project.from_method_specs([spec], None, None)) == ""
