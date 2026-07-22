"""End-to-end integration test for the codegen pipeline.

The LLM is mocked — the hand-authored CappedVault tape, installed via
``install_harness_tape`` (which also disables the agent-index cache) — and the
console HITL replies are scripted via ``install_response_tape``. Everything else
runs for real: Postgres (checkpoint / store / memory) in a testcontainer, solc,
the VFS + working-spec machinery, and the live Certora cloud prover.

The scenario (``test_scenarios/codegen_capped_vault``) is built so the first
draft fails BOTH spec rules for different reasons — an implementation bug
(``depositIncreasesTotal``) and an over-strong assertion (``depositRaisesBalance``)
— which drives the two parallel per-rule CEX analyses, the cross-rule aggregator,
the spec-side ``cex_remediation`` sub-agent, the working-spec commit, and a
contradictory-requirement relaxation. Prover verdicts are real, so given the
deterministic tape + fixed spec/code the run is reasonably deterministic.
Pass/fail is simply: the workflow reaches ``WorkflowSuccess``.

Marked ``expensive`` (live cloud prover + containers + the embedding model load)
and skipped without testcontainers. Run with ``-m expensive``.
"""
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import composer.llm.registry as registry
from composer.input.parsing import upload_input
from composer.input.types import CommandLineArgs
from composer.workflow.executor import execute_ai_composer_workflow
from composer.workflow.types import WorkflowSuccess
from composer.ui.console import ConsoleHandler
from composer.ui.tool_display import tool_context
from composer.testing.ui_harness_codegen_capped_vault import (
    install_harness_tape,
    install_response_tape,
)

from tests.conftest import needs_postgres, MockSentenceTransformer

pytestmark = [pytest.mark.expensive, needs_postgres, pytest.mark.asyncio]

_SCENARIO_NAME = "codegen_capped_vault"


def _make_args(rag_conn: str, scenario_dir: Path) -> CommandLineArgs:
    """Hand-built args (the CLI path builds this via argparse). The three input
    documents are the scenario's spec / interface / system doc."""
    return cast(CommandLineArgs, SimpleNamespace(
        spec_file=str(scenario_dir / "vault.spec"),
        interface_file=str(scenario_dir / "ICappedVault.sol"),
        system_doc=str(scenario_dir / "system.md"),
        rag_db=rag_conn,
        thread_id=None,
        checkpoint_id=None,
        recursion_limit=100,
        prover_capture_output=True,
        prover_keep_folders=False,
        local_prover=False,
        debug_prompt_override=None,
        skip_reqs=False,
        # Model-config fields: only read through ``get_provider_for``, which the
        # tape patches to ignore them, so the values are inert — present to
        # satisfy the args surface.
        model="fake-model",
        tokens=128_000,
        thinking_tokens=2048,
        memory_tool=False,
        interleaved_thinking=False,
    ))


def _install_mocks(monkeypatch) -> None:
    """LLM / HITL / embedding mocks. The databases themselves — and the host/port
    connection redirection — are handled once per session by ``langgraph_db``."""
    # The CappedVault tape (patches registry.get_provider_for + uploader_for and
    # disables the agent-index cache) plus the scripted console HITL replies.
    install_harness_tape(with_delay=False)
    install_response_tape()
    # Swap the real sentence-transformer for the deterministic mock: no model
    # download (and no "Sentence transformers not available" throw), and nothing
    # in this run depends on real embeddings (index cache disabled by the tape,
    # RAG DB empty). Each module imported ``get_model`` by name, so patch every
    # binding the run reaches: the executor (aliased ``get_rag_model``) and the
    # requirements extractor.
    monkeypatch.setattr(
        "composer.workflow.executor.get_rag_model", MockSentenceTransformer
    )
    monkeypatch.setattr(
        "composer.natreq.extractor.get_model", MockSentenceTransformer
    )


async def test_codegen_capped_vault_runs_end_to_end(scenario_provider, langgraph_db, monkeypatch):
    scenario_dir = scenario_provider.by_name(_SCENARIO_NAME)
    _install_mocks(monkeypatch)

    args = _make_args(langgraph_db.rag_db, scenario_dir)
    # Go through the registry module (not a name imported at module load) so these
    # pick up the tape's patched ``get_provider_for`` / ``uploader_for``.
    llm = registry.get_provider_for(options=args)
    input_data = await upload_input(registry.uploader_for(llm.provider), args)

    # Run the whole workflow. Pass == it reaches WorkflowSuccess: the impl bug was
    # fixed, the spec over-constraint remediated + committed, and the contradictory
    # requirement relaxed, so the final master prover + requirements validations
    # both stamp and check_completion lets the author deliver.
    handler = ConsoleHandler(capture_prover_output=True)
    with tool_context():
        result = await execute_ai_composer_workflow(
            handler=handler,
            llm=llm,
            input=input_data,
            workflow_options=args,
        )

    assert isinstance(result, WorkflowSuccess)
