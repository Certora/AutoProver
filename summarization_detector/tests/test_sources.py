"""URL→inputs plumbing (sources.py) — the pure, offline parts: deriving the main contract from a conf's
`verify` field and locating the run conf in a fetched tree. (fetch/certoraRun/POU paths are live I/O,
exercised end-to-end, not here.)"""
import json
import tempfile
from pathlib import Path

from summarization_detector.sources import (
    cut_from_conf, find_run_conf, find_external_call_graph)


def test_cut_from_conf_reads_verify_then_parametric():
    with tempfile.TemporaryDirectory() as d:
        c = Path(d) / "run.conf"
        c.write_text(json.dumps({"verify": "Router:certora/specs/sanity-Router.spec"}))
        assert cut_from_conf(c) == "Router"
        c.write_text(json.dumps({"parametric_contracts": ["Vault", "Other"]}))   # no verify -> parametric
        assert cut_from_conf(c) == "Vault"
        c.write_text(json.dumps({"files": ["A.sol"]}))                            # neither -> ""
        assert cut_from_conf(c) == ""


def test_find_run_conf_prefers_canonical_and_skips_lib():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "inputs" / ".certora_sources").mkdir(parents=True)
        (root / "inputs" / ".certora_sources" / "run.conf").write_text("{}")
        (root / "lib" / "dep").mkdir(parents=True)
        (root / "lib" / "dep" / "run.conf").write_text("{}")                     # must be ignored
        found = find_run_conf(root)
        assert found == root / "inputs" / ".certora_sources" / "run.conf"


def test_find_external_call_graph_optional():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        assert find_external_call_graph(root) is None                            # absent -> None (no gate)
        rpt = root / "Reports"
        rpt.mkdir()
        (rpt / "externalCallGraph.json").write_text("{}")
        assert find_external_call_graph(root) == rpt / "externalCallGraph.json"


