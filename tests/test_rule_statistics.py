"""Saying where a rule that timed out stood, from the Prover's per-rule statistics.

The fixture is shaped like a halted job's tree view: each rule's ``Assertions`` node names its
``rule_live_statistics_*.json`` in ``LiveCheckInfo``, and a rule the job cut off is still
``RUNNING``. The section labels and value formats are the Prover's. The function names are made
up.
"""

import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from composer.prover import core
from composer.prover.core import ProverCallbacks, ProverOptions, ProverReport
from composer.prover.results import (
    AlertReport,
    FetchedResults,
    Hotspot,
    MemoryUpdates,
    Nonlinearity,
    get_final_treeview,
    parse_rule_statistics,
    read_and_format_run_result,
    read_rule_statistics,
    unfinished_statistics_files,
)

_ROOT = Path(__file__).parent / "data" / "prover_statistics"
_TREE_VIEW = _ROOT / "Reports" / "treeView"
_LINK = "https://prover.certora.com/jobStatus/1/2?anonymousKey=k"


def test_only_the_unfinished_rules_statistics_are_wanted() -> None:
    tree, _ = get_final_treeview(_ROOT)
    assert unfinished_statistics_files(tree) == {
        "rule_live_statistics_2.json",
        "rule_live_statistics_3.json",
    }


def test_a_rule_stuck_before_the_solver_says_so() -> None:
    stats = read_rule_statistics(_TREE_VIEW, "rule_live_statistics_2.json")
    assert stats is not None
    assert stats.path_count is None
    assert stats.render() == "static analysis (the rule did not reach the solver)"


def test_a_rule_stopped_while_solving_names_its_hotspots_largest_first() -> None:
    stats = read_rule_statistics(_TREE_VIEW, "rule_live_statistics_3.json")
    assert stats is not None
    assert stats.branching == (
        Hotspot("amm::math::mul_div_wide", 86),
        Hotspot("amm::pool::swap_output", 10),
        Hotspot("amm::math::fee_tier", 4),
    )
    assert stats.render() == (
        "solving (splits attempted 8, 0 % proved, splitting depth 7 of 10); path count approx. 2^19; "
        "23 nonlinear ops of degree up to 12; "
        "branching: amm::math::mul_div_wide 86 %, amm::pool::swap_output 10 %; "
        "nonlinear ops: amm::math::mul_div_wide 96 %"
    )


_LOW, _MEDIUM, _HIGH = 1, 2, 3


def _no_hotspots(nonlinearity: tuple[str, int], memory: tuple[str, int]) -> str:
    """A rule the Prover could not attribute to functions, as it reports one: rated totals only."""
    def total(label: str, rated: tuple[str, int]) -> dict:
        value, severity = rated
        return {"label": label, "value": value, "children": [], "severity": severity}

    return json.dumps([
        {"label": _SOLVING, "value": None, "children": []},
        {"label": "path count", "value": "approx. 2^55", "children": []},
        total("nonlinearity", nonlinearity),
        total("memory complexity", memory),
        {"label": "path count hotspots", "value": None, "children": []},
        {"label": "per call stats", "value": None,
         "children": [{"label": "failed to compute per-call stats", "value": None}]},
    ])


_SOLVING = (
    "verification phase: \n splits attempted (solved or split again): 1 \n total weight of splits "
    "proved: 0 % \n splitting depth reached: 0 \n configured max. splitting depth: 10"
)
_NO_NONLINEARITY = ("nonlinear ops: 0\nmax polyn. degree: 1", _LOW)
_FEW_WRITES = ("memory updates: 3\nlongest update sequence: 0", _LOW)


def test_with_no_hotspots_the_totals_say_what_kind_of_cost_it_is() -> None:
    memory = parse_rule_statistics(_no_hotspots(
        _NO_NONLINEARITY, ("memory updates: 205\nlongest update sequence: 202", _MEDIUM)
    ))
    arithmetic = parse_rule_statistics(_no_hotspots(
        ("nonlinear ops: 719\nmax polyn. degree: 336", _HIGH), _FEW_WRITES
    ))
    assert memory is not None and arithmetic is not None
    assert memory.top_share is None and arithmetic.top_share is None
    assert (memory.nonlinearity, memory.memory) == (None, MemoryUpdates(205, 202))
    assert (arithmetic.nonlinearity, arithmetic.memory) == (Nonlinearity(719, 336), None)
    assert memory.render().endswith(
        "path count approx. 2^55; 205 memory updates, the longest sequence 202"
    )
    assert arithmetic.render().endswith("path count approx. 2^55; 719 nonlinear ops of degree up to 336")


def test_a_total_the_prover_rates_low_is_left_out() -> None:
    stats = parse_rule_statistics(_no_hotspots(_NO_NONLINEARITY, _FEW_WRITES))
    assert stats is not None
    assert (stats.nonlinearity, stats.memory) == (None, None)
    assert stats.render().endswith("path count approx. 2^55")


@pytest.mark.parametrize("text", ["not json", "{}", "[]", '[{"label": "path count", "value": "3"}]'])
def test_statistics_without_a_phase_are_not_read(text: str) -> None:
    assert parse_rule_statistics(text) is None


def test_a_missing_statistics_file_is_not_read(tmp_path: Path) -> None:
    assert read_rule_statistics(tmp_path, "rule_live_statistics_9.json") is None


def test_each_timed_out_rule_carries_its_statistics_file() -> None:
    parsed = read_and_format_run_result(_ROOT, "solana")
    assert isinstance(parsed, dict)
    assert {r.name: r.live_check_info for r in parsed.values() if r.status == "TIMEOUT"} == {
        "rule_swap_output_rounds_down": "rule_live_statistics_2.json",
        "rule_swap_preserves_invariant": "rule_live_statistics_3.json",
    }


async def _report(monkeypatch: pytest.MonkeyPatch, root: Path) -> ProverReport:
    async def submitted(*_args, **_kwargs):
        return {"sort": "success", "link": _LINK, "is_local_link": False}, ""

    @asynccontextmanager
    async def fetched(*_args, **_kwargs):
        yield FetchedResults(root, None, AlertReport())

    monkeypatch.setattr(core, "run_prover_inner", submitted)
    monkeypatch.setattr(core, "cloud_results", fetched)
    report = await core.run_prover(
        Path("."), [], "tc", ProverOptions(app="solana", server="prod"), ProverCallbacks(), None
    )
    assert isinstance(report, ProverReport)
    return report


@pytest.mark.asyncio
async def test_the_report_says_where_each_timed_out_rule_stood(monkeypatch) -> None:
    report = await _report(monkeypatch, _ROOT)

    stuck = report.result_str.split(core._STUCK_HEADING, 1)[1].strip().splitlines()
    assert stuck[0] == (
        "* rule_swap_output_rounds_down: static analysis (the rule did not reach the solver)"
    )
    assert stuck[1].startswith("* rule_swap_preserves_invariant: solving (")
    assert len(stuck) == 2


@pytest.mark.asyncio
async def test_a_report_with_no_statistics_has_no_section(monkeypatch, tmp_path: Path) -> None:
    tree_view = tmp_path / "Reports" / "treeView"
    tree_view.mkdir(parents=True)
    (tree_view / "treeViewStatus_1.json").write_text((_TREE_VIEW / "treeViewStatus_1.json").read_text())

    report = await _report(monkeypatch, tmp_path)

    assert core._STUCK_HEADING not in report.result_str
    assert "rule_swap_preserves_invariant" in report.result_str
