"""Difficulty profiler (difficulty_profile.py) — the pure, offline parts: source classification
(cut / library / external / cvl-model) and the difficulty-tree parse + hotspot roll-up. The live POU
fetch (profile_job/profile_jobs) is exercised end-to-end against real jobs, not here."""
from summarization_detector.difficulty_profile import (
    _classify, _parse_difficulty_tree, _iter_slow_leaves,
)

CUT = "Vault"
SCENE = {"Vault", "OracleHarness", "TokenHarness"}


def test_classify_cut_external_library_cvl():
    assert _classify("Vault.computeAccountData", CUT, SCENE) == ("Vault", "cut")
    # a linked scene contract that is NOT the CUT -> external dependency
    assert _classify("OracleHarness.getPrice", CUT, SCENE)[1] == "external"
    # not a scene contract and not the CUT -> an inlined library
    assert _classify("ValuationLib.toValue", CUT, SCENE) == ("ValuationLib", "library")
    # an applied CVL summary contributes nothing to model
    assert _classify("CVL/Ghost Function 'cvlPrice(id)'", CUT, SCENE)[1] == "cvl-model"


def test_parse_tree_extracts_metrics_and_ranked_hotspots():
    tree = {"label": "verification phase", "children": [
        {"label": "path count", "value": "approx. 2^127"},
        {"label": "nonlinearity", "value": "nonlinear ops: 286\nmax polyn. degree: 6"},
        {"label": "nonlinearity hotspots", "children": [
            {"label": "function: Vault.withdraw",
             "value": "contrib. to nonlinear ops: 68 %",
             "jumpToDefinition": {"file": "src/Vault.sol", "start": {"line": 244}}},
            {"label": "function: OracleHarness.getPrice",
             "value": "contrib. to nonlinear ops: 12 %",
             "jumpToDefinition": {"file": "src/Oracle.sol", "start": {"line": 471}}},
        ]},
    ]}
    metrics, hs = _parse_difficulty_tree(tree, CUT, SCENE)
    assert metrics["path_count"] == "approx. 2^127"
    assert "286" in metrics["nonlinearity"]
    nl = sorted([h for h in hs if h.kind == "nl"], key=lambda h: h.pct, reverse=True)
    assert (nl[0].function, nl[0].pct, nl[0].klass, nl[0].location) == (
        "Vault.withdraw", 68, "cut", "src/Vault.sol:244")
    assert nl[1].klass == "external" and nl[1].pct == 12


def test_iter_slow_leaves_recurses_children_and_thresholds():
    rules = [{"name": "inv", "status": "VIOLATED", "duration": 1862, "LiveCheckInfo": None, "children": [
        {"name": "Induction step", "status": "TIMEOUT", "duration": 1837, "LiveCheckInfo": None, "children": [
            {"name": "methodA", "status": "TIMEOUT", "duration": 1808,
             "LiveCheckInfo": "rule_live_statistics_86.json", "children": []},
            {"name": "fast", "status": "VERIFIED", "duration": 9,
             "LiveCheckInfo": "rule_live_statistics_75.json", "children": []},
        ]},
    ]}]
    leaves = list(_iter_slow_leaves(rules, min_seconds=1200))
    # only the slow leaf with a live-stats file; the induction-scaffolding name is not prefixed in
    names = [l["name"] for l in leaves]
    assert len(leaves) == 1 and names[0].endswith("methodA") and "Induction" not in names[0]
    assert leaves[0]["lci"] == "rule_live_statistics_86.json"
