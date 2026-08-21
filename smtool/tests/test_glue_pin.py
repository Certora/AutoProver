"""add_glue_pin: a sound-by-construction model==real glue pin at an agent-chosen key — for a DERIVED
address key (a credit target from another getter) that the deterministic field-pins miss, whose ghost
cell is otherwise an unconstrained uint256 (-> cast-safety CEX)."""
from smtool.ir import ToolInput, FunctionSpec, Param as P, free_var
from smtool.project import Project
from smtool import mutations as mut
from smtool.cvl_parse import parse_expression
from composer.cvl.pretty_print import pretty_print


def _proj():
    # credit-transfer shape: a state-changer + an address-keyed observable + a fee-receiver getter
    m = FunctionSpec.of("act", [P("uint256", "id"), P("uint256", "shares")], [], "nonpayable")
    bal = FunctionSpec.of("getBalanceOf", [P("uint256", "id"), P("address", "holder")],
                          ["uint256"], "view", envfree=True, frame_args=[free_var("address", "a")])
    fr = FunctionSpec.of("getReceiver", [P("uint256", "id")], ["address"], "view", envfree=True)
    return Project.from_method_specs([ToolInput(cut="C", functions=[m, bal, fr])], None, None)


def test_glue_pin_at_derived_key_is_model_equals_real():
    pr = _proj()
    r = mut.add_glue_pin(pr, method="act", observable="getBalanceOf",
                         key_exprs=[parse_expression("id"), parse_expression("getReceiver(id)")])
    assert r.ok, r.message
    glue = pretty_print(pr.conformance["act"])
    # the pin is EXACTLY reader(keys) == getter(keys) at the derived key — model==real, sound
    assert ("getBalanceOfCVLReader(id, getReceiver(id)) == "
            "getBalanceOf(id, getReceiver(id))") in glue.replace("\n", " ")


def test_glue_pin_idempotent():
    pr = _proj()
    keys = [parse_expression("id"), parse_expression("getReceiver(id)")]
    assert mut.add_glue_pin(pr, method="act", observable="getBalanceOf", key_exprs=keys).ok
    n1 = pretty_print(pr.conformance["act"]).count("glue pin (agent)")
    mut.add_glue_pin(pr, method="act", observable="getBalanceOf", key_exprs=list(keys))
    n2 = pretty_print(pr.conformance["act"]).count("glue pin (agent)")
    assert n1 == n2 == 1, "re-adding the same pin must be idempotent"


def test_glue_pin_rejects_non_observable():
    pr = _proj()
    r = mut.add_glue_pin(pr, method="act", observable="notAnObservable",
                         key_exprs=[parse_expression("id")])
    assert not r.ok and "not a modeled observable" in r.message


def _proj_multi():
    # a MULTI-RETURN getter (address, uint8) with one observable per component (the u/d shape)
    m = FunctionSpec.of("act", [P("uint256", "id"), P("uint256", "shares")], [], "nonpayable")
    ud = [FunctionSpec.of("getUnderlyingAndDecimals", [P("uint256", "id")], ["address", "uint8"], "view",
                          envfree=True, observable=True, component_names=["u", "d"], bind_component=i)
          for i in (0, 1)]
    return Project.from_method_specs([ToolInput(cut="C", functions=[m, *ud])], None, None)


def test_glue_pin_multireturn_destructures_instead_of_tuple_compare():
    pr = _proj_multi()
    r = mut.add_glue_pin(pr, method="act", observable="getUnderlyingAndDecimals",
                         key_exprs=[parse_expression("id")])
    assert r.ok, r.message
    glue = pretty_print(pr.conformance["act"]).replace("\n", " ")
    # the BUG was comparing a scalar reader to the whole tuple (untypeable); the fix loads once + pins
    # each component to its own local — so the buggy `reader == getter(tuple)` shape must NOT appear
    assert "CVLReader(id) == getUnderlyingAndDecimals(id)" not in glue
    # one agent pin per modeled component, each against a fresh destructure local
    assert glue.count("glue pin (agent): model == real for getUnderlyingAndDecimals") == 2
    assert "getUnderlyingAndDecimals_uCVLReader(id) == _gp_getUnderlyingAndDecimals_" in glue
    assert "getUnderlyingAndDecimals_dCVLReader(id) == _gp_getUnderlyingAndDecimals_" in glue


def test_glue_pin_multireturn_idempotent():
    pr = _proj_multi()
    keys = [parse_expression("id")]
    assert mut.add_glue_pin(pr, method="act", observable="getUnderlyingAndDecimals", key_exprs=keys).ok
    n1 = pretty_print(pr.conformance["act"]).count("glue pin (agent)")
    mut.add_glue_pin(pr, method="act", observable="getUnderlyingAndDecimals", key_exprs=list(keys))
    n2 = pretty_print(pr.conformance["act"]).count("glue pin (agent)")
    assert n1 == n2 == 2, "re-adding the same multi-return pin must not re-declare/duplicate"
