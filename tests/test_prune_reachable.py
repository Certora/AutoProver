"""Regression: prune_reachable claims an invariant proved ONLY when its top-level (ROOT) node is
VERIFIED.

An invariant expands into many prover checks (base case + one preservation per method + sub-checks); POU
aggregates them into a single ROOT node whose status is the authoritative verdict. Reading the verdict
from the ROOT — not from leaf aggregation — is what keeps prune sound on any partially-finished run
(our own stop-on-first-violation cancel, a prover-side timeout, a halt): an absent/RUNNING leaf is not a
passing one. The leaf verdicts remain available (for the agent to see WHICH assert violated) but never
decide the proof verdict.
"""
import asyncio
from dataclasses import dataclass, field

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


def _root(rule, status):
    return RuleVerdict(rule, status, status in ("VERIFIED", "SUCCESS"), node_type="ROOT")


def _leaf(rule, status, node_type="INVARIANT_SUBCHECK"):
    return RuleVerdict(rule, status, status in ("VERIFIED", "SUCCESS"), node_type=node_type)


def _res(rules):
    return VerifyResult(conf="r.conf", success=False, job_url=None, rules=rules)


def _prune(project, canned, monkeypatch):
    async def fake_verify(*a, **k):
        return canned
    monkeypatch.setattr(V, "verify", fake_verify)
    return asyncio.run(V.prune_reachable(project, "reachable.conf"))


def test_violated_root_drops_invariant(monkeypatch):
    # balanceLeqSupply: many leaves pass, but the ROOT verdict is VIOLATED -> NOT proven. The passing
    # leaves must NOT mask it (the original masking bug: 49 verified / 56 violated leaves, kept).
    rules = [_leaf("balanceLeqSupply", "VERIFIED") for _ in range(49)]
    rules += [_leaf("balanceLeqSupply", "VIOLATED", "VIOLATED_ASSERT") for _ in range(56)]
    rules += [_root("balanceLeqSupply", "VIOLATED")]
    proj = _FakeProject(candidates=["balanceLeqSupply"])
    kept, dropped, _ = _prune(proj, _res(rules), monkeypatch)
    assert kept == []
    assert dropped == ["balanceLeqSupply"]
    assert "balanceLeqSupply" not in proj.verified_invariants


def test_verified_root_keeps_invariant(monkeypatch):
    rules = [_leaf("solventInv", "VERIFIED") for _ in range(20)] + [_root("solventInv", "VERIFIED")]
    proj = _FakeProject(candidates=["solventInv"])
    kept, dropped, _ = _prune(proj, _res(rules), monkeypatch)
    assert kept == ["solventInv"]
    assert dropped == []
    assert "solventInv" in proj.verified_invariants


def test_passing_leaves_without_verified_root_not_kept(monkeypatch):
    # Partial/halted run: every leaf seen so far passed, but the ROOT never reached VERIFIED (still
    # RUNNING). Absence of a failing leaf is NOT a proof -> must NOT be kept.
    rules = [_leaf("inv", "VERIFIED") for _ in range(5)] + [_root("inv", "RUNNING")]
    proj = _FakeProject(candidates=["inv"])
    kept, dropped, _ = _prune(proj, _res(rules), monkeypatch)
    assert kept == [] and dropped == ["inv"]


def test_no_root_node_not_kept(monkeypatch):
    # Job died before the ROOT node was emitted: only leaves present, all passing. Still not proven.
    rules = [_leaf("inv", "VERIFIED") for _ in range(3)]
    proj = _FakeProject(candidates=["inv"])
    kept, dropped, _ = _prune(proj, _res(rules), monkeypatch)
    assert kept == [] and dropped == ["inv"]


def test_timeout_root_drops_invariant(monkeypatch):
    rules = [_leaf("inv", "VERIFIED") for _ in range(5)] + [_root("inv", "TIMEOUT")]
    proj = _FakeProject(candidates=["inv"])
    kept, dropped, _ = _prune(proj, _res(rules), monkeypatch)
    assert kept == [] and dropped == ["inv"]
