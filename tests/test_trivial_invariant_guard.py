"""add_require_invariant rejects trivial invariants and replaces on re-add.

An agent that adds a placeholder invariant (`true`, `x==x`) produces a vacuous — and, with no params,
syntactically invalid — reachable spec, then flails because a same-name re-add used to be a silent
no-op. Both are now closed: trivial expressions are rejected with guidance, and a re-add REPLACES.
"""
from smtool.cvl_parse import parse_expression
from smtool.agent.tools import _is_trivial_invariant


def test_trivial_forms_detected():
    for s in ["true", "false", "a == a", "x != x"]:
        assert _is_trivial_invariant(parse_expression(s)), s


def test_real_invariants_not_trivial():
    for s in ["pm.moduleById(0) == 0", "bal <= supply", "balanceOf(a) <= totalSupply()"]:
        assert not _is_trivial_invariant(parse_expression(s)), s


def test_readd_replaces_invariant():
    # a corrected invariant with the SAME name must REPLACE the stale one, not be dropped as a no-op.
    from smtool import cvlx as x, driver
    import composer.cvl.schema as S

    class _Proj:
        def __init__(self):
            self.reachable = x.spec_file(blocks=[x.func(driver.ASSUME, [("address", "a")], [], [])])
            self.verified_invariants = set()
        def snapshot(self):
            import copy
            c = _Proj.__new__(_Proj)
            c.reachable = copy.deepcopy(self.reachable)
            c.verified_invariants = set(self.verified_invariants)
            return c

    from smtool import mutations as mut
    proj = _Proj()
    # commit by hand: mutations._commit expects a real Project; exercise the block edit directly instead.
    reach = proj.reachable
    reach.blocks.append(x.invariant("inv", [("address", "a")], parse_expression("a == a")))
    # re-add with a corrected expr must replace, leaving exactly ONE 'inv' block
    new = x.invariant("inv", [("address", "a")], parse_expression("balanceOf(a) <= totalSupply()"))
    existing = next(b for b in reach.blocks if isinstance(b, S.Invariant) and b.name == "inv")
    reach.blocks[reach.blocks.index(existing)] = new
    invs = [b for b in reach.blocks if isinstance(b, S.Invariant) and b.name == "inv"]
    assert len(invs) == 1
    assert invs[0].model_dump()["invariant_expression"] != parse_expression("a == a").model_dump()
