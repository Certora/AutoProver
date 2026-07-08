"""Entry point for the auto-prove multi-agent pipeline TUI."""

import argparse
import hashlib
import logging
import pathlib
import uuid
from contextlib import asynccontextmanager
from typing import cast, AsyncIterator, Protocol, Callable, Awaitable

from composer.diagnostics.timing import RunSummary
from composer.input.types import DEFAULT_RECURSION_LIMIT, ExtendedModelOptions, RAGDBOptions
from composer.input.parsing import add_protocol_args
from composer.kb.knowledge_base import DEFAULT_KB_NS
from composer.rag.db import PostgreSQLRAGDatabase
from composer.pipeline.core import CorePipelineResult

from composer.spec.context import (
    SourceFields
)
from composer.pipeline.cli import cli_pipeline, user_ns
from composer.spec.source.pipeline import ProverBackend, GeneratedCVL
from composer.prover.core import make_prover_options
from composer.spec.source.source_env import build_source_env
from composer.spec.source.artifacts import ProverArtifactStore
from composer.spec.agent_index import agent_index_config_from_env
from composer.core.user import get_uid
from composer.spec.cvl_research import DEFAULT_CVL_AGENT_INDEX_NS
from composer.ui.autoprove_app import AutoProvePhase
from composer.io.thread_logging import RunDataLogger

from composer.spec.util import FS_FORBIDDEN_READ
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
    cache_ns: str | None
    memory_ns: str | None
    cloud: bool
    interactive: bool
    threat_model: str
    recursion_limit: int
    max_bug_rounds: int


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

type Executor = Callable[[HandlerFactory[AutoProvePhase, None]], Awaitable[CorePipelineResult[GeneratedCVL]]]

@asynccontextmanager
async def _entry_point(summary: RunSummary) -> AsyncIterator[Executor]:
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
    parser.add_argument("--cache-ns", default=None, help="Cache namespace (enables cross-run caching)")
    parser.add_argument("--memory-ns", default=None, help="Memory namespace (default: thread id)")
    parser.add_argument("--cloud", action="store_true", help="Run prover jobs in the cloud")
    parser.add_argument("--interactive", action="store_true", help="Interactively refine the security properties after extraction")
    parser.add_argument("--threat-model", type=str, default=None, help="Path to a 'thread' model (text or pdf) with which to seed the property extraction process")
    parser.add_argument("--max-bug-rounds", type=int, default=3, help="Maximum number of bug-extraction rounds run per component during property analysis (default: 3)")

    args = cast(AutoProveArgs, parser.parse_args())
    async with autoprove_executor(args, summary) as runner:
        yield runner


@asynccontextmanager
async def autoprove_executor(args: AutoProveArgs, summary: RunSummary) -> AsyncIterator[Executor]:
    """Set up services from already-parsed args and yield the pipeline runner.

    ``_entry_point`` parses argv into ``AutoProveArgs`` then delegates here; tests
    construct ``AutoProveArgs`` directly.
    """

    thread_id = f"autoprove_{uuid.uuid4().hex[:12]}"

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
        handler: HandlerFactory[AutoProvePhase, None]
    ) -> CorePipelineResult[GeneratedCVL]:    
        async with (
            cli_pipeline(
                args=args, design_doc_phase=design_phase,
                summary=summary,
                thread_id=thread_id,
                task_handler=handler,
                at_exit=exit_logger,

                worfklow="autoprove"
            ) as (staged, cont),
            PostgreSQLRAGDatabase.rag_context(staged.embed_model, args.rag_db) as rag_db

        ):
            source_data_ns = user_ns("source_agent", "cache", staged.root_key)

            source_env = build_source_env(
                models=staged.llm_models,
                db=rag_db,
                forbidden_read=FS_FORBIDDEN_READ,
                kb_ns=DEFAULT_KB_NS,
                root=staged.source.project_root,
                store=staged.conns.indexed_store,
                source_question_ns=source_data_ns,
                recursion_limit=args.recursion_limit,
                cvl_index_config=agent_index_config_from_env(DEFAULT_CVL_AGENT_INDEX_NS),
            )
            backend = ProverBackend(
                ProverArtifactStore(staged.source.project_root, staged.source.contract_name),
                make_prover_options(cloud=args.cloud)
            )
            return await cont(source_env, backend)
    yield callback
