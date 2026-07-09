"""Generic async entry point for a Rust application.

Mirrors ``composer/foundry/entry.py``'s shape — parse args → open DB / store /
checkpointer / logging → yield a closure the caller drives with a handler factory
— but is *descriptor-driven*: the CLI flags, precondition validation, and report
tag all come from the Rust wheel's ``AppDescriptor`` instead of being hard-coded.

The imperative service wiring (Postgres pools, the async tool context, the thread
logger, ``WorkflowContext``) stays Python and is essentially identical to the
foundry entry point — that shell is irreducibly async and is not something Rust
owns (see ``docs/rust-applications.md`` §4.2). What Rust contributes here is only
declarative: the arg schema and the ``validate_preconditions`` hook.

The env built here is *neutral*: the standard source-navigation toolset
(``code_explorer`` + fs tools) with no RAG surface. A backend that wants a RAG
database can supply its own env builder via ``env_builder=``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable, cast

from langgraph.store.base import BaseStore

from composer.core.user import user_data_ns
from composer.diagnostics.logging_setup import setup_autoprove_logging
from composer.diagnostics.timing import RunSummary, install_run_summary
from composer.input.parsing import add_protocol_args
from composer.input.types import (
    DEFAULT_RECURSION_LIMIT,
    ExtendedModelOptions,
)
from composer.io.multi_job import HandlerFactory
from composer.io.thread_logging import default_logging_ns, thread_logger
from composer.kb.knowledge_base import DefaultEmbedder
from composer.pipeline.core import CorePipelineResult
from composer.rag.models import get_model
from composer.rustapp.descriptor import ArgDefault, ArgSpec
from composer.rustapp.host import RustApplication, build_application, run_application
from composer.rustapp.result import RustFormalResult
from composer.spec.context import SourceCode, WorkflowContext
from composer.spec.service_host import ModelProvider, PureServiceHost, ServiceHost
from composer.spec.source.source_env import (
    build_basic_source_tools,
    build_source_tools,
)
from composer.spec.system_model import SolidityIdentifier
from composer.spec.util import FS_FORBIDDEN_READ
from composer.ui.tool_display import async_tool_context
from composer.workflow.services import llm_factory, standard_connections

# A caller-supplied env builder, for backends that want a custom tool/RAG surface.
EnvBuilder = Callable[..., ServiceHost]

# The Executor a frontend drives.
RustRunner = Callable[
    [HandlerFactory], Awaitable[CorePipelineResult[RustFormalResult]]
]


def build_neutral_env(
    *,
    model_provider: ModelProvider,
    project_root: str,
    store: BaseStore,
    source_question_ns: tuple[str, ...],
    recursion_limit: int,
    forbidden_read: str = FS_FORBIDDEN_READ,
) -> ServiceHost:
    """A source-navigation env with no RAG surface — the same ``code_explorer`` +
    fs tools the built-in backends use for analysis/authoring. ``forbidden_read``
    is the ecosystem's fs-exclusion default (Cargo layout for Rust, Foundry for EVM)."""
    basic = build_basic_source_tools(root=project_root, forbidden_read=forbidden_read)
    full = build_source_tools(
        basic, model_provider, store, source_question_ns, recursion_limit=recursion_limit
    )
    return PureServiceHost(models=model_provider, rag_tools=(), sort="existing").bind_source_tools(
        full
    )


def _user_ns(*parts: str | None) -> tuple[str, ...]:
    return user_data_ns() + tuple(p for p in parts if p)


def _root_cache_key(
    project_root: str, system_doc_path: pathlib.Path, relative_path: str, contract_name: str
) -> str:
    doc_hash = hashlib.sha256(system_doc_path.read_bytes()).hexdigest()
    combined = "|".join([project_root, doc_hash, relative_path, contract_name])
    return hashlib.sha256(combined.encode()).hexdigest()[:16]


def _add_declared_args(parser: argparse.ArgumentParser, specs: list[ArgSpec]) -> list[str]:
    """Add the descriptor's declared flags; return their argparse dests."""
    dests: list[str] = []
    for spec in specs:
        dest = spec.flag.lstrip("-").replace("-", "_")
        dests.append(dest)
        d: ArgDefault = spec.default
        if d.kind == "bool":
            parser.add_argument(
                spec.flag, dest=dest, action="store_true",
                default=bool(d.value), help=spec.help,
            )
        elif d.kind == "int":
            parser.add_argument(
                spec.flag, dest=dest, type=int, default=d.value,
                required=spec.required, help=spec.help,
            )
        else:  # str
            parser.add_argument(
                spec.flag, dest=dest, type=str, default=d.value,
                required=spec.required, help=spec.help,
            )
    return dests


@asynccontextmanager
async def rust_entry_point(
    app: RustApplication,
    summary: RunSummary,
    *,
    argv: list[str] | None = None,
    env_builder: EnvBuilder | None = None,
    run_pipeline_fn: Callable[..., Awaitable[CorePipelineResult[RustFormalResult]]] | None = None,
) -> AsyncIterator[RustRunner]:
    """Parse args, open services, and yield the Executor for ``app``.

    Pass a pre-built :class:`RustApplication` (from :func:`build_application`) so the
    backend and the frontend share one phase enum. ``argv`` overrides ``sys.argv``
    (useful in tests); ``env_builder`` overrides :func:`build_neutral_env`."""
    descriptor = app.descriptor
    parser = argparse.ArgumentParser(description=f"{descriptor.name} — AutoProver (Rust backend)")
    add_protocol_args(parser, ExtendedModelOptions)
    parser.add_argument(
        "--recursion-limit", type=int, default=DEFAULT_RECURSION_LIMIT,
        help=f"Max graph iterations (default: {DEFAULT_RECURSION_LIMIT})",
    )
    parser.add_argument("project_root", help="Project root")
    parser.add_argument("main_contract", help="Main contract as path:ContractName")
    parser.add_argument("system_doc", help="Path to the design document (text or PDF)")
    parser.add_argument("--max-concurrent", type=int, default=4, help="Max concurrent agents (default: 4)")
    parser.add_argument("--cache-ns", default=None, help="Cache namespace (enables cross-run caching)")
    parser.add_argument("--memory-ns", default=None, help="Memory namespace (default: thread id)")
    parser.add_argument("--interactive", action="store_true", help="Interactively refine extracted properties")
    parser.add_argument("--max-bug-rounds", type=int, default=3, help="Max bug-extraction rounds per component (default: 3)")
    declared_dests = _add_declared_args(parser, descriptor.args)

    args = parser.parse_args(argv)

    project_root = pathlib.Path(args.project_root).resolve()
    main_path, contract_name = args.main_contract.split(":", 1)
    contract_name = SolidityIdentifier(contract_name)
    full_path = pathlib.Path(main_path).resolve()
    if not full_path.is_relative_to(project_root):
        parser.error(f"Invalid path: {full_path} not under project root {project_root}")
    relative_path = str(full_path.relative_to(project_root))
    sys_path = pathlib.Path(args.system_doc)

    # Rust-owned precondition validation (cf. foundry's foundry.toml check).
    declared_args = {d: getattr(args, d) for d in declared_dests}
    precondition_input = json.dumps(
        {
            "project_root": str(project_root),
            "main_contract": args.main_contract,
            "system_doc": str(sys_path),
            **declared_args,
        }
    )
    err = app.validate_preconditions(json.loads(precondition_input))
    if err:
        parser.error(err)

    model = get_model()
    root_key = _root_cache_key(str(project_root), sys_path, relative_path, contract_name)
    cache_root: tuple[str, ...] | None = (
        _user_ns(args.cache_ns, root_key) if args.cache_ns is not None else None
    )

    thread_id = f"{descriptor.name}_{uuid.uuid4().hex[:12]}"
    text_log, events_log = setup_autoprove_logging(str(project_root), thread_id)
    print(f"{descriptor.name} logs: {text_log}\n         events: {events_log}", file=sys.stderr)
    install_run_summary(summary)

    # argparse Namespace duck-types the ModelConfiguration protocol (the model flags come from
    # ExtendedModelOptions); the built-in entries cast their args the same way.
    model_fact = llm_factory(cast(Any, args))

    async with (
        standard_connections(embedder=DefaultEmbedder(model)) as conns,
        async_tool_context(),
        thread_logger(
            conns.store,
            {
                "root_thread_id": thread_id,
                "workflow": descriptor.name,
                "cache_root": list(cache_root) if cache_root is not None else None,
                "memory_ns": args.memory_ns if args.memory_ns is not None else thread_id,
            },
            default_logging_ns(uid=None),
            run_id=summary.run_id,
        ),
    ):
        content = await conns.uploader.get_document(sys_path)
        if content is None:
            parser.error(f"cannot read {sys_path}")
        # The ecosystem's fs-exclusion default (Cargo layout for Rust, Foundry for EVM).
        forbidden_read = getattr(
            app.ecosystem.language, "default_forbidden_read", FS_FORBIDDEN_READ
        )
        source_input = SourceCode(
            content=content,
            project_root=str(project_root),
            contract_name=contract_name,
            relative_path=relative_path,
            forbidden_read=forbidden_read,
        )

        model_provider = ModelProvider(
            checkpointer=conns.checkpointer,
            factory=model_fact,
            heavy_model=args.heavy_model,
            lite_model=args.lite_model,
        )
        source_question_ns = _user_ns("source_agent", "cache", root_key)

        builder = env_builder or build_neutral_env
        env = builder(
            model_provider=model_provider,
            project_root=str(project_root),
            store=conns.indexed_store,
            source_question_ns=source_question_ns,
            recursion_limit=args.recursion_limit,
            forbidden_read=forbidden_read,
        )

        ctx = WorkflowContext.create(
            services=conns.memory,
            thread_id=thread_id,
            store=conns.store,
            recursion_limit=args.recursion_limit,
            cache_namespace=cache_root,
            memory_namespace=args.memory_ns,
        )

        async def runner(handler: HandlerFactory) -> CorePipelineResult[RustFormalResult]:
            # A backend that needs a bespoke store/pipeline (e.g. Crucible's crate
            # store) supplies run_pipeline_fn; everything else uses the generic host.
            if run_pipeline_fn is not None:
                return await run_pipeline_fn(
                    source_input=source_input, ctx=ctx, handler_factory=handler,
                    env=env, args=args,
                )
            return await run_application(
                app,
                source_input=source_input,
                ctx=ctx,
                handler_factory=handler,
                env=env,
                max_concurrent=args.max_concurrent,
                max_bug_rounds=args.max_bug_rounds,
                interactive=args.interactive,
            )

        yield runner


def build_arg_parser(app: RustApplication) -> argparse.ArgumentParser:
    """Build (but do not run) the descriptor-driven argument parser — exposed for
    tests and ``--help`` introspection without opening any service."""
    parser = argparse.ArgumentParser(description=f"{app.descriptor.name} — AutoProver (Rust backend)")
    add_protocol_args(parser, ExtendedModelOptions)
    parser.add_argument("--recursion-limit", type=int, default=DEFAULT_RECURSION_LIMIT)
    parser.add_argument("project_root")
    parser.add_argument("main_contract")
    parser.add_argument("system_doc")
    parser.add_argument("--max-concurrent", type=int, default=4)
    parser.add_argument("--cache-ns", default=None)
    parser.add_argument("--memory-ns", default=None)
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--max-bug-rounds", type=int, default=3)
    _add_declared_args(parser, app.descriptor.args)
    return parser
