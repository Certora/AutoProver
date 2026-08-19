"""Regression: prune_reachable keeps a candidate invariant only when EVERY prover check for it passed.

An invariant expands into many checks (base case + one preservation per method) sharing the invariant's
rule name. If a passing sibling check could mask a VIOLATED/TIMEOUT one, the tool would keep a refuted
invariant and the conformance would assume a false predicate (unsound). This pins the fix.
"""
import asyncio
from dataclasses import dataclass, field

import pytest

import smtool.verify as V
from smtool.verify import RuleVerdict, VerifyResult


@dataclass
class _FakeProject:
    """Minimal stand-in exercising the prune bookkeeping (reachable_invariant_names / drop_invariants)."""
    candidates: list
    verified_invariants: set = field(default_factory=set)
    dropped: list = field(default_factory=list)

    def reachable_invariant_names(self):
        return list(self.candidates)

    def drop_invariants(self, names):
        self.dropped.extend(sorted(names))


def _res(rules):
    return VerifyResult(conf="r.conf", success=False, job_url=None, rules=rules)


def _prune(project, canned, monkeypatch):
    async def fake_verify(*a, **k):
        return canned
    monkeypatch.setattr(V, "verify", fake_verify)
    return asyncio.run(V.prune_reachable(project, "reachable.conf"))


def test_mixed_pass_fail_invariant_is_dropped(monkeypatch):
    # balanceLeqSupply: some instances pass, one preservation instance VIOLATED -> NOT proven.
    rules = [RuleVerdict("balanceLeqSupply", "VERIFIED", True) for _ in range(49)]
    rules += [RuleVerdict("balanceLeqSupply", "VIOLATED", False) for _ in range(56)]
    rules += [RuleVerdict("balanceLeqSupply", "TIMEOUT", False) for _ in range(2)]
    proj = _FakeProject(candidates=["balanceLeqSupply"])
    kept, dropped, _ = _prune(proj, _res(rules), monkeypatch)
    assert kept == []
    assert dropped == ["balanceLeqSupply"]
    assert "balanceLeqSupply" not in proj.verified_invariants


def test_fully_passing_invariant_is_kept(monkeypatch):
    rules = [RuleVerdict("solventInv", "VERIFIED", True) for _ in range(20)]
    proj = _FakeProject(candidates=["solventInv"])
    kept, dropped, _ = _prune(proj, _res(rules), monkeypatch)
    assert kept == ["solventInv"]
    assert dropped == []
    assert "solventInv" in proj.verified_invariants


def test_one_timeout_instance_drops_invariant(monkeypatch):
    rules = [RuleVerdict("inv", "VERIFIED", True) for _ in range(5)] + [RuleVerdict("inv", "TIMEOUT", False)]
    proj = _FakeProject(candidates=["inv"])
    kept, dropped, _ = _prune(proj, _res(rules), monkeypatch)
    assert kept == [] and dropped == ["inv"]
