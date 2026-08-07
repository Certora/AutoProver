"""add_nondet soundness gates — the invariants the PROVER cannot catch, so they must hold at the tool
boundary: NONDET is refused for state-changing targets and for any function of the CUT (concrete or a
`_.f` wildcard whose name collides with a CUT function); it is allowed for an off-path OTHER-contract
view."""
from smtool import mutations as M


def _proj(project, spec):
    return project(spec("f", [("uint256", "x")], ["uint256"], "nonpayable"),
                   spec("getX", [("uint256", "x")], ["uint256"], "view", envfree=True))


def test_nondet_refuses_cut_function_concrete(project, spec):
    pr = _proj(project, spec)
    r = M.add_nondet(pr, method="f", contract="C", name="getX",
                     param_types=["uint256"], return_types=["uint256"], mutability="view")
    assert not r.ok
    assert "contract-under-test" in r.message


def test_nondet_refuses_cut_function_via_wildcard(project, spec):
    pr = _proj(project, spec)
    # `_.getX` collides with a CUT function name -> must also be refused
    r = M.add_nondet(pr, method="f", contract="_", name="getX",
                     param_types=["uint256"], return_types=["uint256"], mutability="view")
    assert not r.ok


def test_nondet_refuses_state_changing_target(project, spec):
    pr = _proj(project, spec)
    r = M.add_nondet(pr, method="f", contract="Other", name="doThing",
                     param_types=[], return_types=["uint256"], mutability="nonpayable")
    assert not r.ok


def test_nondet_allows_offpath_other_contract_view(project, spec):
    pr = _proj(project, spec)
    # a view on ANOTHER in-scene contract, off the checked output -> legitimate NONDET target
    r = M.add_nondet(pr, method="f", contract="Oracle", name="latestPrice",
                     param_types=["uint256"], return_types=["uint256"], mutability="view")
    assert r.ok
    # ... and it can be retracted
    back = M.remove_nondet(pr, "f", name="latestPrice", contract="Oracle")
    assert back.ok
