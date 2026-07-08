"""Entry point for the auto-prove multi-agent pipeline TUI."""

import argparse
import hashlib
import logging
import pathlib
import sys
import uuid
from contextlib import asynccontextmanager
from typing import cast, AsyncIterator, Protocol, Callable, Awaitable

from composer.diagnostics.logging_setup import setup_autoprove_logging
from composer.diagnostics.timing import RunSummary, install_run_summary
from composer.input.types import DEFAULT_RECURSION_LIMIT, ExtendedModelOptions, RAGDBOptions
from composer.input.parsing import add_protocol_args
from composer.kb.knowledge_base import DefaultEmbedder, DEFAULT_KB_NS
from composer.rag.db import PostgreSQLRAGDatabase
from composer.rag.models import get_model
from composer.workflow.services import llm_factory, standard_connections

from composer.spec.service_host import ModelProvider
from composer.spec.system_model import SolidityIdentifier
from composer.spec.context import (
    WorkflowContext,
)
from composer.llm.registry import get_provider_for
from composer.spec.source.pipeline import run_autoprove_pipeline, AutoProveResult
from composer.spec.source.artifacts import ProverSourceCode, ProverArtifactStore
from composer.spec.source.design_doc_finder import resolve_design_doc, discovery_cache_key
from composer.prover.core import make_prover_options
from composer.spec.source.source_env import build_source_env
from composer.spec.agent_index import agent_index_config_from_env
from composer.core.user import get_uid, user_data_ns
from composer.spec.cvl_research import DEFAULT_CVL_AGENT_INDEX_NS
from composer.ui.autoprove_app import AutoProvePhase
from composer.ui.tool_display import async_tool_context
from composer.io.thread_logging import thread_logger, default_logging_ns

from composer.spec.util import FS_FORBIDDEN_READ
from composer.io.multi_job import HandlerFactory

_logger = logging.getLogger(__name__)

def user_ns(
    *parts: str | tuple[str, ...]
) -> tuple[str,...]:
    to_ret : list[str] = []
    for p in parts:
        if isinstance(p, str):
            to_ret.append(p)
        else:
            to_ret.extend(p)
    return user_data_ns() + tuple(to_ret)

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
# Cache
# ---------------------------------------------------------------------------

def _root_cache_key(
    project_root: str,
    system_doc_path: pathlib.Path,
    relative_path: str,
    contract_name: str,
) -> str:
    """Generate a cache key from all inputs that affect the analysis."""
    doc_hash = hashlib.sha256(system_doc_path.read_bytes()).hexdigest()
    combined = "|".join([project_root, doc_hash, relative_path, contract_name])
    return hashlib.sha256(combined.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

type Executor = Callable[[HandlerFactory[AutoProvePhase, None]], Awaitable[AutoProveResult]]

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
    # Parse main_contract (path:ContractName)
    project_root = pathlib.Path(args.project_root).resolve()
    main_contract_path, contract_name = args.main_contract.split(":", 1)

    full_contract_path = pathlib.Path(main_contract_path).resolve()
    if not full_contract_path.is_relative_to(project_root):
        raise ValueError(f"Invalid path: {full_contract_path} doesn't appear in project root {project_root}")

    relative_path = str(full_contract_path.relative_to(project_root))

    # Set up services
    tiered = get_provider_for(tiered=args)
        
    model = get_model()

    thread_id = f"autoprove_{uuid.uuid4().hex[:12]}"

    text_log, events_log = setup_autoprove_logging(project_root, thread_id)
    print(f"autoprove logs: {text_log}\n           events: {events_log}", file=sys.stderr)
    install_run_summary(summary)

    async with (
        standard_connections(
            provider=tiered.provider_kind, embedder=DefaultEmbedder(model)
        ) as conns,
        PostgreSQLRAGDatabase.rag_context(model, args.rag_db) as rag_db,
        async_tool_context(),
        thread_logger(
            conns.store,
            {"root_thread_id": thread_id},
            default_logging_ns(None),
            run_id=summary.run_id,
        ) as data_logger
    ):
        # ``models``, the discovery context, the threat model and prover options are
        # all doc-independent and built up front. The design doc — supplied or
        # auto-discovered — and everything keyed off its bytes (root cache key,
        # source-agent index ns, source env, workflow context, the ProverSourceCode
        # artifact) are built inside ``runner``: discovery needs an active handler
        # scope, which only ``run_task`` (reachable once ``runner`` has the handler
        # factory) installs.
        models = ModelProvider(
            heavy_model=tiered.heavy,
            lite_model=tiered.lite,
            checkpointer=conns.checkpointer,
        )

        memory_ns = args.memory_ns
        if memory_ns:
            memory_ns = get_uid() + "/" + memory_ns

        # The discovery cache lives under a DOC-INDEPENDENT namespace (the doc is the
        # output of discovery, not an input), so a repeat run on the same project
        # reuses the chosen path without re-running the finder agent.
        disc_cache_ns = (
            user_ns(args.cache_ns, "discovery",
                    discovery_cache_key(str(project_root), relative_path, contract_name))
            if args.cache_ns is not None else None
        )
        disc_ctx = WorkflowContext.create(
            services=conns.memory,
            thread_id=thread_id,
            store=conns.store,
            recursion_limit=args.recursion_limit,
            cache_namespace=disc_cache_ns,
            memory_namespace=memory_ns,
        )

        threat_model = (
            await conns.uploader.get_document(pathlib.Path(threat_path))
            if (threat_path := args.threat_model) is not None else None
        )

        prover_opts = make_prover_options(cloud=args.cloud)

        async def runner(handler: HandlerFactory[AutoProvePhase, None]) -> AutoProveResult:
            # Resolve the design doc first: the supplied path, or auto-discovery under
            # a visible ``DISCOVER_DESIGN_DOC`` phase. Everything below is keyed off the
            # resolved doc's bytes, so it cannot be built until the doc is known.
            doc_path, content = await resolve_design_doc(
                system_doc_arg=args.system_doc,
                project_root=str(project_root),
                contract_name=contract_name,
                relative_path=relative_path,
                forbidden_read=FS_FORBIDDEN_READ,
                uploader=conns.uploader,
                models=models,
                handler=handler,
                discover_phase=AutoProvePhase.DISCOVER_DESIGN_DOC,
                disc_ctx=disc_ctx,
            )

            root_key = _root_cache_key(
                str(project_root), doc_path, relative_path, contract_name,
            )
            cache_root = user_ns(args.cache_ns, root_key) if args.cache_ns is not None else None
            # Source-code agent caches are always per-user — the conventional
            # ``user_data_ns(uid)`` prefix lives directly in the ns we pass so the
            # AgentIndex runs single-pool (no overlay).
            source_data_ns = user_ns("source_agent", "cache", root_key)

            # Record the namespaces this run used in its metadata so the cache explorer
            # can be pointed at the run by id alone. root_key — hence cache_root — is
            # derived from the resolved doc (supplied or discovered), which isn't known
            # until here, so it can't be recorded in the up-front run tags and is logged
            # from inside runner instead.
            await data_logger("cache_root", {
                "cache_root": list(cache_root) if cache_root is not None else None,
                "contract_name": str(contract_name),
                "memory_ns": memory_ns,
            })

            source_env = build_source_env(
                models=models,
                db=rag_db,
                forbidden_read=FS_FORBIDDEN_READ,
                kb_ns=DEFAULT_KB_NS,
                root=args.project_root,
                store=conns.indexed_store,
                source_question_ns=source_data_ns,
                recursion_limit=args.recursion_limit,
                cvl_index_config=agent_index_config_from_env(DEFAULT_CVL_AGENT_INDEX_NS),
            )

            ctx = WorkflowContext.create(
                services=conns.memory,
                thread_id=thread_id,
                store=conns.store,
                recursion_limit=args.recursion_limit,
                cache_namespace=cache_root,
                memory_namespace=memory_ns,
            )

            system_doc = ProverSourceCode(
                content=content,
                project_root=str(project_root),
                contract_name=SolidityIdentifier(contract_name),
                relative_path=relative_path,
                forbidden_read=FS_FORBIDDEN_READ,
            )

            return await run_autoprove_pipeline(
                    ctx=ctx,
                    source_input=system_doc,
                    env=source_env,
                    handler_factory=handler,
                    prover_opts=prover_opts,
                    max_concurrent=args.max_concurrent,
                    interactive=args.interactive,
                    threat_model=threat_model,
                    max_bug_rounds=args.max_bug_rounds,
                )

        try:
            yield runner
        finally:
            # Persist final token + prover usage into the run's data_ns at run close
            # (totals known only once the pipeline is done). Guarded so a data_ns write
            # that fails at teardown can't stop the on-disk dump below.
            try:
                await data_logger("token_usage", summary.token_usage_summary())
                await data_logger("prover_usage", summary.prover_usage_summary())
            except Exception:
                _logger.exception("failed to log usage to run data")
            # Dump the run manifest to disk — always, success or crash. Guarded so a dump
            # failure can't mask the pipeline's own outcome. project_root/contract_name
            # come straight from args (not ProverSourceCode, which may not exist yet if
            # discovery crashed).
            try:
                ProverArtifactStore(str(project_root), contract_name).write_job_info(
                    summary, user_id=get_uid()
                )
            except Exception:
                _logger.exception("failed to dump job info")
