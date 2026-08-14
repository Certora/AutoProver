"""Unit tests for the mutual_recursion signal — no AST-dump fixture required.

The graph/scoring helpers take plain dicts, and the signal itself is exercised
over ASTs built inline as dicts (the same idiom as the solidity_ast lenient-parsing
tests), so nothing here needs solc or a committed dump. Detection over a real
compiled project is covered by the fixture-backed tests in the private corpus.
"""

import itertools
from pathlib import Path

import pytest
import yaml

from certora_autosetup.amenability.callgraph import (
    recursive_clusters,
    strongly_connected_components,
)
from certora_autosetup.amenability.context import AnalysisContext
from certora_autosetup.amenability.scoring import DEFAULT_WEIGHTS, ScoringConfig
from certora_autosetup.amenability.signals import ALL_SIGNALS
from certora_autosetup.amenability.signals.recursion import (
    DEFAULT_PARAMS,
    EVIDENCE_CAP,
    Finding,
    cluster_cost,
    mutual_recursion,
    score_findings,
)
from certora_autosetup.solidity_ast import AstDump

SOLC = "0.8.30"


# ---- inline AST builders --------------------------------------------------

def _identifier(node_id: int, name: str, referenced: int | None = None) -> dict:
    return {"id": node_id, "src": "0:1:0", "nodeType": "Identifier", "name": name,
            "overloadedDeclarations": [], "referencedDeclaration": referenced,
            "typeDescriptions": {}}


def _call(node_id: int, callee: dict) -> dict:
    return {"id": node_id, "src": "0:1:0", "nodeType": "FunctionCall",
            "kind": "functionCall", "names": [], "tryCall": False,
            "isConstant": False, "isLValue": False, "isPure": False,
            "lValueRequested": False, "typeDescriptions": {}, "arguments": [],
            "expression": callee}


def _params(node_id: int) -> dict:
    return {"id": node_id, "src": "0:0:0", "nodeType": "ParameterList", "parameters": []}


def _fn(fid: int, name: str, calls: list[int], *, in_loop: bool = False,
        visibility: str = "internal", kind: str = "function") -> dict:
    statements = [
        {"id": fid * 100 + i, "src": "0:1:0", "nodeType": "ExpressionStatement",
         "expression": _call(fid * 100 + 50 + i,
                             _identifier(fid * 100 + 70 + i, f"f{callee}", callee))}
        for i, callee in enumerate(calls)
    ]
    if in_loop:
        statements = [{
            "id": fid + 800, "src": "0:1:0", "nodeType": "WhileStatement",
            "condition": _identifier(fid + 803, "keepGoing"),
            "body": {"id": fid + 801, "src": "0:1:0", "nodeType": "Block",
                     "statements": statements},
        }]
    return {"id": fid, "src": "0:1:0", "nodeType": "FunctionDefinition", "name": name,
            "implemented": True, "kind": kind, "modifiers": [], "scope": 1,
            "stateMutability": "nonpayable", "virtual": False, "visibility": visibility,
            "parameters": _params(fid + 900), "returnParameters": _params(fid + 901),
            "body": {"id": fid + 902, "src": "0:1:0", "nodeType": "Block",
                     "statements": statements}}


def _ctx(project_root: Path, source_path: str, functions: list[dict], *,
         contract_kind: str = "library", params: dict | None = None) -> AnalysisContext:
    contract = {"id": 1, "src": "0:10:0", "nodeType": "ContractDefinition", "name": "C",
                "abstract": False, "baseContracts": [], "contractDependencies": [],
                "contractKind": contract_kind, "fullyImplemented": True,
                "linearizedBaseContracts": [1], "nodes": functions, "scope": 0}
    unit = {"id": 2, "src": "0:10:0", "nodeType": "SourceUnit", "absolutePath": source_path,
            "exportedSymbols": {}, "nodes": [contract]}
    dump = AstDump.from_dict({source_path: {source_path: {"2": unit}}},
                             on_error="raise", solc_version=SOLC)
    return AnalysisContext(project_root=project_root, dumps=[dump],
                           params={"mutual_recursion": params} if params else {})


def _finding(size: int, *, loop: bool = False, reachable: bool = True,
             entry_sites: int = 1) -> Finding:
    return Finding(source_path="lib/dep/A.sol", byte_offset=0,
                   members=tuple(f"C.f{i}" for i in range(size)),
                   loop_entered=loop, reachable=reachable, entry_sites=entry_sites)


# ---- pure graph tests -----------------------------------------------------

class TestCycleDetection:
    def test_linear_chain_has_no_cycle(self):
        assert recursive_clusters({1: {2}, 2: {3}, 3: set()}) == []

    def test_diamond_without_back_edge_has_no_cycle(self):
        # a shared callee is not recursion
        assert recursive_clusters({1: {2, 3}, 2: {4}, 3: {4}, 4: set()}) == []

    def test_self_call_is_a_cycle(self):
        clusters = recursive_clusters({1: {1}})
        assert [c.members for c in clusters] == [(1,)]

    def test_two_cycle_detected(self):
        clusters = recursive_clusters({1: {2}, 2: {1}})
        assert [c.members for c in clusters] == [(1, 2)]

    def test_cycle_with_chord_is_one_cluster(self):
        clusters = recursive_clusters({1: {2, 3}, 2: {3}, 3: {1}})
        assert [c.members for c in clusters] == [(1, 2, 3)]

    def test_disjoint_clusters_reported_separately(self):
        clusters = recursive_clusters({1: {2, 3}, 2: {3}, 3: {1}, 4: {5}, 5: {4, 5}})
        assert sorted(c.size for c in clusters) == [2, 3]

    def test_unresolved_callee_id_is_ignored(self):
        # an unresolvable callee (external call, builtin) is a node with no successors
        assert recursive_clusters({1: {99}}) == []

    def test_entry_sites_counts_callers_outside_the_cluster(self):
        (cluster,) = recursive_clusters({1: {3}, 2: {3}, 3: {4}, 4: {3}})
        assert cluster.members == (3, 4) and cluster.entry_sites == 2

    def test_loop_entered_from_an_outside_caller(self):
        (cluster,) = recursive_clusters({1: {2}, 2: {3}, 3: {2}}, loop_edges={(1, 2)})
        assert cluster.loop_entered

    def test_loop_entered_from_inside_the_cluster(self):
        (cluster,) = recursive_clusters({1: {2}, 2: {1}}, loop_edges={(2, 1)})
        assert cluster.loop_entered

    def test_loop_elsewhere_does_not_mark_the_cluster(self):
        (cluster,) = recursive_clusters({1: {2}, 2: {1}, 3: {1}}, loop_edges={(3, 4)})
        assert not cluster.loop_entered

    def test_reachability_from_entry_points(self):
        callees = {1: {2}, 2: {3}, 3: {2}, 7: {8}, 8: {7}}
        live, dead = sorted(recursive_clusters(callees, entry_points={1}),
                            key=lambda c: not c.reachable)
        assert live.members == (2, 3) and dead.members == (7, 8) and not dead.reachable

    def test_no_entry_points_means_nothing_is_provably_reachable(self):
        (cluster,) = recursive_clusters({1: {2}, 2: {1}})
        assert not cluster.reachable

    def test_scc_of_a_large_chain_does_not_recurse(self):
        # iterative Tarjan: a chain far deeper than the interpreter limit is fine
        depth = 5000
        callees = {i: {i + 1} for i in range(depth)}
        callees[depth] = {0}
        (component,) = [c for c in strongly_connected_components(callees) if len(c) > 1]
        assert len(component) == depth + 1


# ---- score curve ----------------------------------------------------------

class TestScoreCurve:
    def test_no_cluster_scores_one(self):
        assert score_findings([], {}) == 1.0

    def test_self_recursion_off_loop_is_friction_not_severe(self):
        config = ScoringConfig.load()
        score = score_findings([_finding(1)], {})
        assert config.killer_severe_score < score < 1.0

    def test_mutual_recursion_in_a_loop_is_severe(self):
        config = ScoringConfig.load()
        assert score_findings([_finding(2, loop=True)], {}) <= config.killer_severe_score

    def test_mutual_recursion_off_loop_is_not_severe(self):
        config = ScoringConfig.load()
        assert score_findings([_finding(2)], {}) > config.killer_severe_score

    def test_loop_entry_lowers_the_score(self):
        assert score_findings([_finding(2, loop=True)], {}) < score_findings([_finding(2)], {})

    def test_unreachable_cluster_is_damped(self):
        assert (score_findings([_finding(3, loop=True, reachable=False)], {})
                > score_findings([_finding(3, loop=True)], {}))

    def test_monotone_in_cluster_size(self):
        scores = [score_findings([_finding(n, loop=True)], {}) for n in (1, 2, 3, 5)]
        assert scores == sorted(scores, reverse=True)

    def test_monotone_in_cluster_count(self):
        one = score_findings([_finding(2, loop=True)], {})
        two = score_findings([_finding(2, loop=True)] * 2, {})
        assert two < one

    def test_monotone_in_entry_sites(self):
        assert (score_findings([_finding(2, loop=True, entry_sites=4)], {})
                < score_findings([_finding(2, loop=True, entry_sites=1)], {}))

    def test_single_cluster_never_zeroes_the_score(self):
        assert score_findings([_finding(20, loop=True, entry_sites=50)], {}) > 0.0

    def test_score_stays_in_the_unit_interval(self):
        assert 0.0 <= score_findings([_finding(9, loop=True)] * 20, {}) <= 1.0

    def test_yaml_overrides_the_module_defaults(self):
        gentler = score_findings([_finding(2, loop=True)], {"loop_entry_multiplier": 1.0})
        assert gentler > score_findings([_finding(2, loop=True)], {})

    def test_cost_is_capped(self):
        cost = cluster_cost(_finding(12, loop=True, entry_sites=30), {})
        assert cost == DEFAULT_PARAMS["max_cycle_cost"]


# ---- the signal over inline ASTs -----------------------------------------

class TestSignalOverInlineAst:
    def test_no_recursion_scores_one(self, tmp_path):
        ctx = _ctx(tmp_path, "src/Clean.sol", [_fn(11, "a", [12]), _fn(12, "b", [13]),
                                               _fn(13, "c", [])])
        result = mutual_recursion(ctx)
        assert result.score == 1.0 and result.evidence == []

    def test_mutual_recursion_in_a_dependency_is_detected(self, tmp_path):
        # The motivating shape: the project's own code is clean, the hazard is
        # vendored. Every other signal iterates project code only and sees nothing.
        ctx = _ctx(tmp_path, "lib/dep/Codec.sol", [_fn(11, "a", [12]), _fn(12, "b", [11])])
        assert list(ctx.iter_functions()) == []
        result = mutual_recursion(ctx)
        assert result.raw["recursive_clusters"] == 1
        assert result.raw["clusters_in_dependencies"] == 1
        assert result.score < 1.0

    def test_recursion_reached_from_a_loop_is_severe(self, tmp_path):
        config = ScoringConfig.load()
        ctx = _ctx(
            tmp_path, "lib/dep/Codec.sol",
            [_fn(10, "run", [11], in_loop=True, visibility="external"),
             _fn(11, "a", [12]), _fn(12, "b", [11])],
            contract_kind="contract", params=config.signal_params["mutual_recursion"],
        )
        result = mutual_recursion(ctx)
        assert result.raw["loop_entered_clusters"] == 1
        assert result.raw["reachable_clusters"] == 1
        assert result.score <= config.killer_severe_score

    def test_same_cluster_off_loop_scores_higher(self, tmp_path):
        in_loop = _ctx(tmp_path, "lib/dep/Codec.sol",
                       [_fn(10, "run", [11], in_loop=True, visibility="external"),
                        _fn(11, "a", [12]), _fn(12, "b", [11])], contract_kind="contract")
        off_loop = _ctx(tmp_path, "lib/dep/Codec.sol",
                        [_fn(10, "run", [11], visibility="external"),
                         _fn(11, "a", [12]), _fn(12, "b", [11])], contract_kind="contract")
        assert mutual_recursion(in_loop).score < mutual_recursion(off_loop).score

    def test_evidence_names_file_and_function(self, tmp_path):
        ctx = _ctx(tmp_path, "lib/dep/Codec.sol", [_fn(11, "a", [12]), _fn(12, "b", [11])])
        (evidence,) = mutual_recursion(ctx).evidence
        assert evidence.signal == "mutual_recursion"
        assert evidence.file == "lib/dep/Codec.sol"
        assert evidence.function in ("C.a", "C.b")
        assert evidence.line >= 0  # 0 when the source text is not on disk
        assert "C.a" in evidence.detail and "C.b" in evidence.detail

    def test_evidence_is_capped(self, tmp_path):
        functions = []
        for i in range(15):
            first, second = 100 + 2 * i, 101 + 2 * i
            functions += [_fn(first, f"a{i}", [second]), _fn(second, f"b{i}", [first])]
        result = mutual_recursion(_ctx(tmp_path, "src/Many.sol", functions))
        assert result.raw["recursive_clusters"] == 15
        assert len(result.evidence) == EVIDENCE_CAP

    def test_clusters_are_deduplicated_across_compilation_units(self, tmp_path):
        # the same vendored source compiled into two units must report one cluster
        source = "lib/dep/Codec.sol"
        functions = [_fn(11, "a", [12]), _fn(12, "b", [11])]
        contract = {"id": 1, "src": "0:10:0", "nodeType": "ContractDefinition", "name": "C",
                    "abstract": False, "baseContracts": [], "contractDependencies": [],
                    "contractKind": "library", "fullyImplemented": True,
                    "linearizedBaseContracts": [1], "nodes": functions, "scope": 0}
        unit = {"id": 2, "src": "0:10:0", "nodeType": "SourceUnit", "absolutePath": source,
                "exportedSymbols": {}, "nodes": [contract]}
        dump = AstDump.from_dict({"src/One.sol": {source: {"2": unit}},
                                  "src/Two.sol": {source: {"2": unit}}},
                                 on_error="raise", solc_version=SOLC)
        ctx = AnalysisContext(project_root=tmp_path, dumps=[dump])
        assert mutual_recursion(ctx).raw["recursive_clusters"] == 1

    def test_free_function_recursion_is_detected(self, tmp_path):
        # free functions live on the SourceUnit, not in a contract — contract-member
        # iteration misses them, the graph walk does not
        source = "src/Free.sol"
        unit = {"id": 2, "src": "0:10:0", "nodeType": "SourceUnit", "absolutePath": source,
                "exportedSymbols": {},
                "nodes": [_fn(11, "a", [12], kind="freeFunction"),
                          _fn(12, "b", [11], kind="freeFunction")]}
        dump = AstDump.from_dict({source: {source: {"2": unit}}},
                                 on_error="raise", solc_version=SOLC)
        ctx = AnalysisContext(project_root=tmp_path, dumps=[dump])
        assert mutual_recursion(ctx).raw["recursive_clusters"] == 1

    def test_external_member_calls_are_not_internal_edges(self, tmp_path):
        # a call through a public/external declaration is a separate program to the
        # decompiler, so it must not close a cycle
        source = "src/Ext.sol"
        callee = _fn(11, "a", [], visibility="external")
        caller = _fn(12, "b", [])
        member = {"id": 1200, "src": "0:1:0", "nodeType": "MemberAccess",
                  "memberName": "a", "referencedDeclaration": 11,
                  "isConstant": False, "isLValue": False, "isPure": False,
                  "lValueRequested": False, "typeDescriptions": {},
                  "expression": _identifier(1201, "other")}
        caller["body"]["statements"] = [
            {"id": 1202, "src": "0:1:0", "nodeType": "ExpressionStatement",
             "expression": _call(1203, member)}]
        callee["body"]["statements"] = [
            {"id": 1204, "src": "0:1:0", "nodeType": "ExpressionStatement",
             "expression": _call(1205, _identifier(1206, "f12", 12))}]
        contract = {"id": 1, "src": "0:10:0", "nodeType": "ContractDefinition", "name": "C",
                    "abstract": False, "baseContracts": [], "contractDependencies": [],
                    "contractKind": "contract", "fullyImplemented": True,
                    "linearizedBaseContracts": [1], "nodes": [callee, caller], "scope": 0}
        unit = {"id": 2, "src": "0:10:0", "nodeType": "SourceUnit", "absolutePath": source,
                "exportedSymbols": {}, "nodes": [contract]}
        dump = AstDump.from_dict({source: {source: {"2": unit}}},
                                 on_error="raise", solc_version=SOLC)
        result = mutual_recursion(AnalysisContext(project_root=tmp_path, dumps=[dump]))
        assert result.raw["recursive_clusters"] == 0

    def test_type_conversion_is_not_a_call(self, tmp_path):
        source = "src/Cast.sol"
        fn = _fn(11, "a", [])
        cast = _call(1300, _identifier(1301, "f11", 11))
        cast["kind"] = "typeConversion"
        fn["body"]["statements"] = [{"id": 1302, "src": "0:1:0",
                                     "nodeType": "ExpressionStatement", "expression": cast}]
        contract = {"id": 1, "src": "0:10:0", "nodeType": "ContractDefinition", "name": "C",
                    "abstract": False, "baseContracts": [], "contractDependencies": [],
                    "contractKind": "library", "fullyImplemented": True,
                    "linearizedBaseContracts": [1], "nodes": [fn], "scope": 0}
        unit = {"id": 2, "src": "0:10:0", "nodeType": "SourceUnit", "absolutePath": source,
                "exportedSymbols": {}, "nodes": [contract]}
        dump = AstDump.from_dict({source: {source: {"2": unit}}},
                                 on_error="raise", solc_version=SOLC)
        result = mutual_recursion(AnalysisContext(project_root=tmp_path, dumps=[dump]))
        assert result.raw["recursive_clusters"] == 0

    def test_empty_project_scores_one(self, tmp_path):
        ctx = AnalysisContext(project_root=tmp_path, dumps=[])
        assert mutual_recursion(ctx).score == 1.0


# ---- configuration wiring -------------------------------------------------

class TestConfiguration:
    def test_signal_is_registered(self):
        assert mutual_recursion in ALL_SIGNALS

    def test_every_signal_has_a_weight(self):
        config = ScoringConfig.load()
        assert {s.signal_id for s in ALL_SIGNALS} <= set(config.weights)

    def test_signal_is_a_structural_killer(self):
        assert "mutual_recursion" in ScoringConfig.load().structural_killers

    def test_yaml_params_match_the_module_defaults(self):
        # weights.yaml is the tuning surface; DEFAULT_PARAMS only serves contexts
        # built without a config, so the two must not drift apart.
        yaml_params = yaml.safe_load(Path(DEFAULT_WEIGHTS).read_text())["signal_params"]
        assert yaml_params["mutual_recursion"] == DEFAULT_PARAMS

    def test_killer_cooccurrence_holds_for_every_killer_subset(self):
        # structural_killers is a set, so the existing low-verdict test picks an
        # arbitrary subset; every subset must reach the same verdict.
        config = ScoringConfig.load()
        from certora_autosetup.amenability.report import Level
        from certora_autosetup.amenability.scoring import aggregate
        from certora_autosetup.amenability.signals.base import SignalResult

        for killers in itertools.combinations(sorted(config.structural_killers),
                                              config.killers_for_low):
            scores = {s: 0.2 for s in config.weights if s not in config.structural_killers}
            scores.update({k: 0.05 for k in killers})
            results = [SignalResult(s, scores.get(s, 1.0)) for s in config.weights]
            assert aggregate(results, config).provisional_level is Level.LOW, killers


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
