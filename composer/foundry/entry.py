"""CLI entry-point wiring for the foundry test author.

Mirrors ``composer/spec/source/autoprove_common.py``'s ``_entry_point``
shape (parse args → set up DB / RAG / store / checkpointer / logging /
thread logger → yield a closure the caller drives with a handler factory)
but plugs in foundry-specific pieces: the foundry RAG database connection
(distinct from the CVL manual RAG), the foundry env builder, and the
foundry pipeline.

Deliberately self-contained — does NOT import the source *pipeline* under
``composer/spec/source/`` (the shared design-doc finder utility,
``design_doc_finder.resolve_design_doc``, is the one exception). Reuses the
cross-workflow infrastructure (``standard_connections``, ``thread_logger``,
``WorkflowContext``, ``run_component_analysis`` / ``run_property_inference``
from the non-source spec modules).
"""

import argparse
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, AsyncIterator, Awaitable, Callable, Protocol, cast

from composer.diagnostics.timing import RunSummary
from composer.input.parsing import Arg, add_protocol_args
from composer.input.types import DEFAULT_RECURSION_LIMIT, RAGDBOptions, ExtendedModelOptions
from composer.io.multi_job import HandlerFactory
from composer.rag.db import FOUNDRY_DEFAULT_CONNECTION, PostgreSQLRAGDatabase
from composer.spec.util import FS_FORBIDDEN_READ

from composer.foundry.env import build_foundry_env
from composer.foundry.pipeline import (
    FoundryPhase, FoundryPipelineResult, backend
)
from composer.pipeline.cli import cli_pipeline, user_ns


# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------


class FoundryRAGDBOptions(RAGDBOptions, Protocol):
    """Overrides ``RAGDBOptions.rag_db``'s default so ``--rag-db`` points
    at the foundry cheatcodes database (not the CVL manual one). The
    foundry workflow only talks to a single RAG DB; we reuse the existing
    ``--rag-db`` flag rather than introducing a separate one."""
    rag_db: Annotated[str, Arg(
        help="Database connection string for the foundry cheatcodes RAG",
        default=FOUNDRY_DEFAULT_CONNECTION,
    )]


class FoundryArgs(ExtendedModelOptions, FoundryRAGDBOptions, Protocol):
    project_root: str
    main_contract: str
    system_doc: str | None
    max_concurrent: int
    cache_ns: str | None
    memory_ns: str | None
    interactive: bool
    max_bug_rounds: int
    recursion_limit: int
    forge_binary: str
    forge_timeout_s: int
    max_forge_runners: int

    @property
    def threat_model(self) -> None:
         ...


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


type FoundryRunner = Callable[
    [HandlerFactory[FoundryPhase, None]], Awaitable[FoundryPipelineResult],
]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Foundry-test author for a property-extraction pipeline",
    )
    add_protocol_args(parser, FoundryRAGDBOptions)
    add_protocol_args(parser, ExtendedModelOptions)
    parser.add_argument(
        "--recursion-limit", type=int, default=DEFAULT_RECURSION_LIMIT,
        help=f"Max graph iterations (default: {DEFAULT_RECURSION_LIMIT})",
    )
    parser.add_argument("project_root", help="Foundry project root (contains foundry.toml).")
    parser.add_argument("main_contract", help="Main contract as path:ContractName")
    parser.add_argument("system_doc", nargs="?", default=None, help="Path to the design document (text or PDF). Optional — auto-discovered from the project when omitted.")
    parser.add_argument("--max-concurrent", type=int, default=4, help="Max concurrent agents (default: 4)")
    parser.add_argument("--max-forge-runners", default=1, type=int, help="Max concurrent forge runners (default: 1)")
    parser.add_argument("--cache-ns", default=None, help="Cache namespace (enables cross-run caching)")
    parser.add_argument("--memory-ns", default=None, help="Memory namespace (default: thread id)")
    parser.add_argument("--interactive", action="store_true", help="Interactively refine extracted properties")
    parser.add_argument("--max-bug-rounds", type=int, default=3, help="Max bug-extraction rounds per component (default: 3)")
    parser.add_argument("--forge-binary", default="forge", help="`forge` executable on PATH (default: forge)")
    parser.add_argument("--forge-timeout-s", type=int, default=600, help="Per-`forge test` invocation timeout in seconds (default: 600)")
    parser.set_defaults(threat_model=None)
    return parser


@asynccontextmanager
async def _entry_point(summary: RunSummary) -> AsyncIterator[FoundryRunner]:
    parser = _build_parser()
    args = cast(FoundryArgs, parser.parse_args())

    thread_id = f"foundry_{uuid.uuid4().hex[:12]}"


    async def runner(fact: HandlerFactory[FoundryPhase, None]) -> FoundryPipelineResult:
        async with (
            cli_pipeline(
                  args=args,
                  thread_id=thread_id,
                  summary=summary,
                  task_handler=fact,
                  design_doc_phase=cast(FoundryPhase, FoundryPhase.DISCOVER_DESIGN_DOC),
                  at_exit=None,
                  workflow="foundry"
            ) as (staged, cont),
            PostgreSQLRAGDatabase.rag_context(staged.embed_model, args.rag_db) as foundry_rag_db,
        ):
            source_question_ns = user_ns("source_agent", "cache", staged.root_key)

            env = build_foundry_env(
                model_provider=staged.llm_models,
                project_root=staged.source.project_root,
                forbidden_read=FS_FORBIDDEN_READ,
                rag_db=foundry_rag_db,
                store=staged.conns.indexed_store,
                source_question_ns=source_question_ns,
                recursion_limit=args.recursion_limit,
            )
            f_backend = backend(
                forge_binary=args.forge_binary,
                forge_timeout_s=args.forge_timeout_s,
                source_input=staged.source,
                forge_concurrency=args.max_forge_runners
            )
            return await cont(env, f_backend)

    yield runner