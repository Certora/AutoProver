"""Entry point for the auto-prove multi-agent pipeline TUI."""

import argparse
import hashlib
import importlib
import logging
import pathlib
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import cast, AsyncIterator, Protocol, Callable, Awaitable

from composer.diagnostics.timing import RunSummary
from composer.io.mailbox import Mailbox, WarmWait
from composer.input.types import DEFAULT_RECURSION_LIMIT, ExtendedModelOptions, RAGDBOptions
from composer.input.parsing import add_extra_context_args, add_protocol_args
from composer.rag.db import PostgreSQLRAGDatabase
from composer.pipeline.core import CorePipelineResult

from composer.spec.context import (
    SourceFields
)
from composer.pipeline.cli import cli_pipeline, user_ns
from composer.pipeline.ptypes import DEFAULT_MAX_CPU_TASKS
from composer.pipeline.ecosystem import EVM
from composer.spec.source.pipeline import ProverBackend, GeneratedCVL
from composer.spec.source.cex_capture import CexAnalysisStore
from composer.prover.core import make_prover_options
from composer.spec.source.source_env import build_source_env
from composer.spec.source.author import SourceEditing
from composer.spec.source.live_explorer import setup_live_edits
from composer.spec.source.munge.edit_store import EditStore
from composer.spec.source.munge.edit_oracle import mk_oracle
from composer.spec.source.artifacts import ProverArtifactStore
from composer.spec.agent_index import AgentIndex, AgentIndexConfig, agent_index_config_from_env
from composer.core.user import get_uid
from composer.spec.cvl_research import DEFAULT_CVL_AGENT_INDEX_NS
from composer.ui.autoprove_app import AutoProvePhase
from composer.io.thread_logging import RunDataLogger

from composer.spec.util import fs_forbidden_read
from composer.io.multi_job import HandlerFactory

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------

class AutoProveArgs(ExtendedModelOptions, RAGDBOptions, Protocol):
    project_root: str
    main_contract: str
    system_doc: str | None
    max_concurrent: int
    max_cpu_tasks: int
    cache_ns: str | None
    memory_ns: str | None
    cloud: bool
    interactive: bool
    threat_model: str
    extra_context: list[str] | None
    recursion_limit: int
    max_bug_rounds: int
    budget: str | None
    time_budget: float | None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

type Executor = Callable[[HandlerFactory[AutoProvePhase]], Awaitable[CorePipelineResult[GeneratedCVL]]]


@dataclass(frozen=True)
class Headless:
    """A run whose person is not at this terminal: its questions and refinement
    turns go through ``mailbox``, waiting ``warm`` for an answer before the
    execution suspends."""
    mailbox: Mailbox
    warm: WarmWait


@dataclass(frozen=True)
class Launch:
    """What the entry point hands its UI: the pipeline to run under the UI's handler
    factory, and, when the run is headless, the input to build that factory around."""
    run: Executor
    headless: Headless | None = None

    async def __call__(self, handler: HandlerFactory[AutoProvePhase]) -> CorePipelineResult[GeneratedCVL]:
        return await self.run(handler)


def resolve_mailbox(spec: str, run_id: str, execution_id: str) -> Mailbox:
    """``MODULE:FACTORY`` names a callable ``(run_id, execution_id) -> Mailbox``; the
    transport is whichever control plane launched this process, which composer never
    imports itself."""
    module_name, sep, attr = spec.partition(":")
    if not sep or not module_name or not attr:
        raise ValueError(f"--mailbox expects MODULE:FACTORY, got {spec!r}")
    factory = cast(Callable[[str, str], Mailbox], getattr(importlib.import_module(module_name), attr))
    return factory(run_id, execution_id)


@asynccontextmanager
async def _entry_point(summary: RunSummary) -> AsyncIterator[Launch]:
    parser = argparse.ArgumentParser(
        description="Auto-prove multi-agent pipeline TUI"
    )
    add_protocol_args(parser, RAGDBOptions)
    add_protocol_args(parser, ExtendedModelOptions)
    parser.add_argument("--recursion-limit", type=int, default=DEFAULT_RECURSION_LIMIT, help=f"The number of iterations of the graph to allow (default: {DEFAULT_RECURSION_LIMIT})")
    parser.add_argument("project_root", help="Root directory of the Solidity project")
    parser.add_argument("main_contract", help="Main contract as path:ContractName")
    parser.add_argument("system_doc", nargs="?", default=None, help="Path to the design document (text or PDF). Optional — auto-discovered from the project when omitted.")
    parser.add_argument("--max-concurrent", type=int, default=4, help="Max concurrent agents (default: 4)")
    parser.add_argument("--max-cpu-tasks", type=int, default=DEFAULT_MAX_CPU_TASKS, help=f"Max concurrent CPU-bound tasks — toolchain builds and the like (default: {DEFAULT_MAX_CPU_TASKS})")
    parser.add_argument("--cache-ns", default=None, help="Cache namespace (enables cross-run caching)")
    parser.add_argument("--memory-ns", default=None, help="Memory namespace (default: thread id)")
    parser.add_argument("--cloud", action="store_true", help="Run prover jobs in the cloud")
    parser.add_argument("--interactive", action="store_true", help="Interactively refine the security properties after extraction")
    parser.add_argument("--threat-model", type=str, default=None, help="Path to a 'threat' model (text or pdf) with which to seed the property extraction process")
    add_extra_context_args(parser)
    parser.add_argument("--max-bug-rounds", type=int, default=3, help="Maximum number of bug-extraction rounds run per component during property analysis (default: 3)")
    parser.add_argument("--budget", default=None, help="Path to a run-budget file (JSON or YAML): {total: USD, caps: {phase: USD, ...}}. Omit to run unbudgeted.")
    parser.add_argument("--time-budget", default=None, type=float, help="Total wall time to run the entire execution. Omit to run without in process limit")
    parser.add_argument(
        "--mailbox", default=None, metavar="MODULE:FACTORY",
        help="Headless input: a factory `(run_id, execution_id) -> composer.io.mailbox.Mailbox` the run's "
             "questions and refinement turns go through, e.g. `aws_mock.mailbox:mailbox_for`. For "
             "console-autoprove; the TUIs answer at the terminal.",
    )
    parser.add_argument(
        "--warm-seconds", default=60.0, type=float,
        help="With --mailbox: how long to wait for an answer before the execution suspends (default: 60)",
    )

    namespace = parser.parse_args()
    args = cast(AutoProveArgs, namespace)
    headless = (
        Headless(
            resolve_mailbox(namespace.mailbox, summary.run_id, summary.execution_id),
            WarmWait(seconds=namespace.warm_seconds),
        )
        if namespace.mailbox is not None else None
    )
    async with autoprove_executor(args, summary, headless=headless) as launch:
        yield launch


@asynccontextmanager
async def autoprove_executor(
    args: AutoProveArgs, summary: RunSummary, headless: Headless | None = None
) -> AsyncIterator[Launch]:
    """Set up services from already-parsed args and yield the pipeline launch.

    ``_entry_point`` parses argv into ``AutoProveArgs`` then delegates here; tests
    construct ``AutoProveArgs`` directly. With ``headless``, the run's inbox is
    reconciled at startup and the execution posts awaiting-input when it parks.
    """

    # The root of every thread id in the run, and so of every checkpoint, cache
    # and memory namespace under it. Named after the run rather than the
    # process so a later execution of the same run finds the same threads.
    thread_id = f"autoprove_{summary.run_id}"

    async def exit_logger(
        run: SourceFields,
        logger: RunDataLogger
    ):
        try:
            await logger("token_usage", summary.token_usage_summary())
            await logger("prover_usage", summary.prover_usage_summary())
        except Exception:
            _logger.exception("failed to log usage to run data")
        # Dump the run manifest to disk — always, success or crash. Guarded so a dump
        # failure can't mask the pipeline's own outcome. project_root/contract_name
        # come straight from args (not ProverSourceCode, which may not exist yet if
        # discovery crashed).
        try:
            ProverArtifactStore(run.project_root, run.contract_name).write_job_info(
                summary, user_id=get_uid()
            )
        except Exception:
            _logger.exception("failed to dump job info")
    design_phase : AutoProvePhase = cast(AutoProvePhase, AutoProvePhase.DISCOVER_DESIGN_DOC)

    async def callback(
        handler: HandlerFactory[AutoProvePhase]
    ) -> CorePipelineResult[GeneratedCVL]:
        async with (
            cli_pipeline(
                args=args, design_doc_phase=design_phase,
                summary=summary,
                thread_id=thread_id,
                task_handler=handler,
                at_exit=exit_logger,
                mailbox=headless.mailbox if headless is not None else None,
                workflow="autoprove"
            ) as (staged, cont),
            PostgreSQLRAGDatabase.rag_context(staged.embed_model, args.rag_db) as rag_db

        ):
            source_data_ns = user_ns("source_agent", "cache", staged.root_key)

            source_env = build_source_env(
                models=staged.llm_models,
                db=rag_db,
                forbidden_read=fs_forbidden_read,
                root=staged.source.project_root,
                store=staged.conns.indexed_store,
                source_question_ns=source_data_ns,
                recursion_limit=args.recursion_limit,
                cvl_index_config=agent_index_config_from_env(DEFAULT_CVL_AGENT_INDEX_NS),
                ecosystem=EVM,
            )
            # Source-editing kit: the edit snapshot store, the live (vfs-aware)
            # tool suite with its versioned explorer, and the migration oracle
            # that caveats cached explorer findings with the files edited since
            # they were recorded. The base index shares the frozen explorer's
            # namespace, so pre-edit (V0) findings stay visible to the live
            # explorer.
            edit_store = EditStore(
                staged.conns.store, user_ns("edit_snapshots", staged.root_key)
            )
            editing = SourceEditing(
                live=setup_live_edits(
                    builder=staged.llm_models.builder_lite(),
                    sc=staged.source,
                    base_store=AgentIndex(
                        store=staged.conns.indexed_store,
                        config=AgentIndexConfig(base_layer=source_data_ns),
                    ),
                    store=staged.conns.indexed_store,
                    source_key=staged.root_key,
                    oracle=mk_oracle(edit_store, staged.source),
                    recursion_limit=args.recursion_limit,
                    ecosystem=EVM,
                ),
                store=edit_store,
            )
            backend = ProverBackend(
                ProverArtifactStore(staged.source.project_root, staged.source.contract_name),
                make_prover_options(cloud=args.cloud),
                editing,
                CexAnalysisStore(store=staged.conns.store, namespace=("cex_analyses", thread_id)),
            )
            return await cont(source_env, backend, EVM)
    yield Launch(callback, headless)
