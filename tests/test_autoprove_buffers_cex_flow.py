"""Buffers authoring flow with a counterexample: submit → collect (CEX) →
expect_rule_failure → re-collect (stamp) → publish.

Drives the main Counter tape (``install_harness_tape``) — the same one
``test_autoprove_counter_runs_end_to_end`` runs against the real prover — but
mocks the prover core so a violated rule is exercised deterministically and
offline. The main test's real prover can't run in every environment; here the
``incrementOther`` rule is reported VIOLATED, so the tape's counterexample
analysis, its ``expect_rule_failure`` reaction, and the re-collect that stamps
the buffer's ``prover`` validation all run. A fast mock also lands the CEX
analysis sooner than a real prover would, which is a stricter test of the
tape's ordering than the slow path.

``_fake_run_prover`` mirrors the one seam of ``run_prover`` this needs: when any
rule is VIOLATED, ``run_prover`` hands the results to ``cex.analyze`` (the CEX
handler that fires ``analyze_cex_raw``). The stand-in makes that same call, so
the analysis LLM turn is consumed exactly as in the real flow.

Marked ``expensive``: needs the testcontainer Postgres and the real local CVL
toolchain (``put_buffer``'s typecheck gate), so it runs with ``-m expensive``.
"""
from pathlib import Path
from typing import Any

import pytest

from composer.diagnostics.timing import RunSummary
from composer.prover.core import ProverReport
from composer.prover.ptypes import RulePath, RuleResult, StatusCodes
from composer.spec.source.autoprove_common import autoprove_executor
from composer.testing.ui_harness_autoprove_Counter import install_harness_tape
from composer.ui.autoprove_console import AutoProveConsoleHandler

from tests.conftest import (
    SPEC_DECL_RE, conf_of_prover_call, needs_postgres, spec_of_prover_conf,
)
from tests.test_autoprove_integration import _install_mocks, _make_args

pytestmark = [pytest.mark.expensive, needs_postgres, pytest.mark.asyncio]

_SCENARIO_NAME = "autoprove_counter"
_BUGGY_RULE = "incrementOther_credits_target_when_distinct"


async def _fake_run_prover(
    folder: Path, args: list[str], tool_call_id: str,
    prover_opts: Any, callbacks: Any, cex: Any,
) -> ProverReport:
    conf = conf_of_prover_call(folder, args)
    spec_text = spec_of_prover_conf(folder, conf)
    names = SPEC_DECL_RE.findall(spec_text)
    assert names, f"fake prover: no rule declarations in {conf['verify']}"
    all_results = [
        RuleResult(
            path=RulePath(rule=n), counterexample=None,
            status=("VIOLATED" if n == _BUGGY_RULE else "VERIFIED"),
        )
        for n in names
    ]
    # Replicate run_prover's own handoff so the CEX-analysis LLM call fires.
    if any(r.status == "VIOLATED" for r in all_results):
        result_str = await cex.analyze(all_results, tool_call_id, callbacks, folder)
    else:
        result_str = "\n".join(f"{r.name}: {r.status}" for r in all_results)
    statuses: dict[RulePath, StatusCodes] = {r.path: r.status for r in all_results}
    return ProverReport(
        raw_rule_status=statuses, result_str=result_str,
        link="https://prover.example/fake-run", certora_run_stdout="",
    )


async def _fake_declared_rules(folder: Path, args: list[str]) -> list[str]:
    return SPEC_DECL_RE.findall(spec_of_prover_conf(folder, conf_of_prover_call(folder, args)))


async def test_cvl_tape_buffers_flow(scenario_provider, langgraph_db, monkeypatch):
    scenario_dir = scenario_provider.by_name(_SCENARIO_NAME)
    _install_mocks(
        monkeypatch, scenario_dir,
        tape_installer=lambda: install_harness_tape(with_delay=False),
    )
    monkeypatch.setattr("composer.spec.source.prover.run_prover", _fake_run_prover)
    monkeypatch.setattr("composer.spec.source.prover.declared_rules_list", _fake_declared_rules)

    summary = RunSummary()
    async with autoprove_executor(
        _make_args(langgraph_db.rag_db, scenario_dir, str(scenario_dir / "system.md")),
        summary,
    ) as run:
        await run(AutoProveConsoleHandler().make_handler)

    # The run-target buffer must have been PUBLISHED (not given up): its own spec on
    # disk (buffer name "core"), NOT just the shared summaries spec.
    specs = list((scenario_dir / "certora" / "specs").rglob("*.spec"))
    print(f"\nPUBLISHED SPECS: {[str(p.relative_to(scenario_dir)) for p in specs]}")
    run_target = [p for p in specs if p.parent.name != "summaries" and p.name == "core.spec"]
    assert run_target, (
        f"no published run-target core.spec — the component gave up (tape diverged). "
        f"Only found: {[str(p.relative_to(scenario_dir)) for p in specs]}"
    )
