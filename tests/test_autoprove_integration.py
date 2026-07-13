"""End-to-end integration test for the autoprove pipeline.

The LLM is mocked — the hand-authored Counter tape, installed via
``install_harness_tape`` (which also disables the agent-index cache) — and so is
AutoSetup, which makes its own LLM calls inside a subprocess and so can't be
taped; ``_fake_autosetup_phase`` returns a canned ``SetupSuccess`` for Counter.
Everything else runs for real: Postgres (checkpoint / store / memory) in a
testcontainer and the live Certora cloud prover. Given the deterministic tape +
fixed spec/code, the prover is reasonably deterministic. Pass/fail is simply: the
pipeline runs start to finish without raising.

Marked ``expensive`` (live cloud prover + containers + the embedding model load)
and skipped without testcontainers. Run with ``-m expensive``.
"""
import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from composer.diagnostics.timing import RunSummary
from composer.spec.source.autoprove_common import autoprove_executor, AutoProveArgs
from composer.spec.source.autosetup import SetupSuccess
from composer.ui.autoprove_console import AutoProveConsoleHandler
from composer.testing.ui_harness_autoprove_Counter import install_harness_tape


from tests.conftest import needs_postgres, MockSentenceTransformer

pytestmark = [pytest.mark.expensive, needs_postgres, pytest.mark.asyncio]

_SCENARIO_NAME = "autoprove_counter"

# The config AutoSetup produced for Counter on a local run (its outputs aren't
# checked in). DummyERC20Impl is dropped — Counter is standalone and the mock
# generated against it doesn't ship. ``verify`` is overlaid per-spec by
# ``prover_config_overlay`` at run time, so it need not name a spec that exists.
_COUNTER_PROVER_CONFIG = {
    "assert_autofinder_success": True,
    "files": ["src/Counter.sol"],
    "global_timeout": "1200",
    "parametric_contracts": "Counter",
    "prover_args": ["-quiet"],
    "run_source": "AUTO_PROVER",
    "solc": "solc",
    "verify": "Counter:certora/specs/sanity-Counter.spec",
    "wait_for_results": "none",
}
# AutoSetup's summaries spec, relative to certora/ (the SetupSuccess contract).
_SUMMARIES_REL = "specs/summaries/Counter_base_summaries.spec"


def _fake_autosetup_phase(scenario_path: Path):

    async def _fake_autosetup_phase_impl(*_args, **_kwargs) -> SetupSuccess:
        """Stand in for the AutoSetup subprocess, which makes its LLM
        calls that we, in autoprover land, aren't going to start taping.
        It is also not an intersting unit of test for this workflow, so just use the
        trivial, precomputed setups.
        Writes out the (trivial, no-op) summaries spec the generated CVL imports
        against, then returns the config AutoSetup would have produced for Counter."""
        summaries = scenario_path / "certora" / _SUMMARIES_REL
        summaries.parent.mkdir(parents=True, exist_ok=True)
        summaries.write_text(
            "// Auto-generated base summaries for Counter\n// No summaries needed for Counter\n"
        )
        return SetupSuccess(
            prover_config=dict(_COUNTER_PROVER_CONFIG),
            summaries_path=_SUMMARIES_REL,
            user_types=[],
        )
    return _fake_autosetup_phase_impl


def _make_args(rag_conn: str, scenario_dir: Path, system_doc: str | None) -> AutoProveArgs:
    """Hand-built ``AutoProveArgs`` (the CLI path builds this via argparse).

    Pass ``system_doc=None`` to exercise the Design Doc Discovery path."""
    return cast(AutoProveArgs, SimpleNamespace(
        project_root=str(scenario_dir),
        main_contract=f"{scenario_dir / "src/Counter.sol"}:Counter",
        system_doc=system_doc,
        max_concurrent=4,
        cache_ns=None,
        memory_ns=None,
        cloud=True,
        interactive=False,
        threat_model=None,
        recursion_limit=100,
        max_bug_rounds=1,
        rag_db=rag_conn,
        # Model-config fields: only read through ``get_provider_for(tiered=args)``,
        # which the tape patches to ignore them, so the values are inert — present
        # to satisfy the AutoProveArgs surface.
        heavy_model="fake-heavy",
        lite_model="fake-lite",
        tokens=128_000,
        thinking_tokens=2048,
        memory_tool=False,
        interleaved_thinking=False,
    ))


def _install_mocks(monkeypatch, scenario_dir: Path) -> None:
    """LLM / AutoSetup / embedding mocks (undone per test by ``monkeypatch``). The
    databases themselves — and the host/port connection redirection — are handled once
    per session by the ``langgraph_db`` fixture."""
    # Mock only the LLM (Counter tape) + disable the agent-index cache.
    install_harness_tape(with_delay=False)
    # pipeline.cli imported `get_provider_for` by name, so install_harness_tape's
    # patch of registry.get_provider_for doesn't reach that binding — rebind it here.
    import composer.llm.registry as registry
    monkeypatch.setattr(
        "composer.pipeline.cli.get_provider_for", registry.get_provider_for
    )
    # Swap the real sentence-transformer for the deterministic mock: no model
    # download, and nothing in this run depends on real embeddings (index cache
    # disabled by the tape, RAG DB empty).
    monkeypatch.setattr(
        "composer.pipeline.cli.get_model", MockSentenceTransformer
    )
    # AutoSetup runs an LLM in a subprocess we can't tape — swap the phase for a
    # canned Counter SetupSuccess. Patch the name the pipeline imported, not the
    # definition in harness.py.
    monkeypatch.setattr(
        "composer.spec.source.pipeline.run_autosetup_phase", _fake_autosetup_phase(scenario_dir)
    )
    # The report phase is best-effort and absorbs failures (grouping degrades to a
    # fallback bucket; the outer guard logs-and-continues). Flip both into re-raise
    # so a broken report lane fails this test instead of passing silently.
    monkeypatch.setattr(
        "composer.spec.source.report.build.RERAISE_REPORT_FAILURES", True
    )


def _read_job_info(scenario_dir: Path) -> dict:
    """The always-written run manifest the entry point's ``finally`` dumps."""
    return json.loads((scenario_dir / "certora" / "ap_report" / "job_info.json").read_text())


async def test_autoprove_counter_runs_end_to_end(scenario_provider, langgraph_db, monkeypatch):
    scenario_dir = scenario_provider.by_name(_SCENARIO_NAME)
    _install_mocks(monkeypatch, scenario_dir)
    monkeypatch.setenv("AUTOPROVER_USER_ID", "e2e-user")
    # Run the whole pipeline. Pass == it completes without raising.
    summary = RunSummary()
    async with autoprove_executor(
        _make_args(langgraph_db.rag_db, scenario_dir, str(scenario_dir / "system.md")),
        summary,
    ) as run:
        await run(AutoProveConsoleHandler().make_handler)

    # The finally dumped the run manifest to ap_report, carrying this run's identity
    # and its (non-empty, since the pipeline ran the prover) usage totals.
    job_info = _read_job_info(scenario_dir)
    assert job_info["user_id"] == "e2e-user"
    assert job_info["run_id"] == summary.run_id
    assert "token_usage" in job_info and "prover_usage" in job_info


async def test_autoprove_dumps_job_info_when_pipeline_crashes(scenario_provider, langgraph_db, monkeypatch):
    """The core guarantee: job_info.json is written even when the run crashes. Patch the
    pipeline to blow up after the executor is set up; the entry-point ``finally`` must
    still land the manifest (with this run's identity) in ap_report."""
    scenario_dir = scenario_provider.by_name(_SCENARIO_NAME)
    _install_mocks(monkeypatch, scenario_dir)
    monkeypatch.setenv("AUTOPROVER_USER_ID", "crash-user")

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("pipeline exploded")

    monkeypatch.setattr(
        "composer.pipeline.cli.run_pipeline", _boom
    )

    summary = RunSummary()
    with pytest.raises(RuntimeError, match="pipeline exploded"):
        async with autoprove_executor(
            _make_args(langgraph_db.rag_db, scenario_dir, str(scenario_dir / "system.md")),
            summary,
        ) as run:
            await run(AutoProveConsoleHandler().make_handler)

    job_info = _read_job_info(scenario_dir)
    assert job_info["user_id"] == "crash-user"
    assert job_info["run_id"] == summary.run_id


async def test_autoprove_counter_no_doc_runs_end_to_end(scenario_provider, langgraph_db, monkeypatch):
    """Same pipeline, design doc OMITTED: the Design Doc Discovery phase runs the
    finder (its tape lane selects ``system.md``) and the run completes via the
    discovered doc."""
    scenario_dir = scenario_provider.by_name(_SCENARIO_NAME)
    _install_mocks(monkeypatch, scenario_dir)
    summary = RunSummary()
    async with autoprove_executor(
        _make_args(langgraph_db.rag_db, scenario_dir, None),
        summary,
    ) as run:
        await run(AutoProveConsoleHandler().make_handler)
