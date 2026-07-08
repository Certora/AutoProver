from typing import Protocol, AsyncIterator, TYPE_CHECKING
import sys
import pathlib
import enum
from contextlib import asynccontextmanager

import asyncio
from dataclasses import dataclass

from composer.input.types import (
    ExtendedModelOptions,
)

from composer.diagnostics.logging_setup import setup_autoprove_logging
from composer.spec.context import SourceFields, WorkflowContext, SourceCode
from composer.spec.service_host import ServiceHost
from composer.workflow.services import IndexedConnections, standard_connections
from composer.pipeline.ptypes import (
    PipelineRun, BackendResult,
    CorePipelineResult
)
from composer.spec.artifacts import ArtifactIdentifier
from composer.spec.service_host import ModelProvider
from composer.spec.system_analysis import SolidityIdentifier
from .core import PipelineBackend, run_pipeline
from composer.io.multi_job import HandlerFactory, run_task, TaskInfo
from composer.diagnostics.timing import RunSummary, install_run_summary
from composer.llm.registry import get_provider_for
from composer.rag.models import get_model
from composer.io.thread_logging import RunDataLogger, thread_logger, default_logging_ns
from composer.kb.knowledge_base import DefaultEmbedder
from composer.ui.tool_display import async_tool_context
from composer.core.user import user_data_ns, get_uid
from composer.spec.source.design_doc_finder import (
    resolve_design_doc, DESIGN_DOC_DISCOVERY_TASK_ID, discovery_cache_key
)
if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

from composer.spec.util import FS_FORBIDDEN_READ
import hashlib


def root_cache_key(
    project_root: str,
    system_doc_path: pathlib.Path,
    relative_path: str,
    contract_name: str,       
):
    doc_hash = hashlib.sha256(system_doc_path.read_bytes()).hexdigest()
    combined = "|".join([project_root, doc_hash, relative_path, contract_name])
    return hashlib.sha256(combined.encode()).hexdigest()


def user_ns(*parts: str | tuple[str, ...]) -> tuple[str, ...]:
    out: list[str] = []
    for p in parts:
        if isinstance(p, str):
            out.append(p)
        else:
            out.extend(p)
    return user_data_ns() + tuple(out)


class PipelineArgs(ExtendedModelOptions, Protocol):
    @property
    def recursion_limit(self) -> int:
        ...

    @property
    def interactive(self) -> bool:
        ...

    @property
    def max_concurrent(self) -> int:
        ...
    
    @property
    def threat_model(self) -> str | None:
        ...

    @property
    def cache_ns(self) -> str | None:
        ...

    @property
    def memory_ns(self) -> str | None:
        ...

    @property
    def max_bug_rounds(self) -> int:
        ...

    @property
    def project_root(self) -> str:
        ...
    
    @property
    def main_contract(self) -> str:
        ...
    
    @property
    def system_doc(self) -> str | None:
        ...

@dataclass
class StagedPipeline:
    conns: IndexedConnections
    llm_models: ModelProvider
    embed_model: "SentenceTransformer"
    source: SourceCode
    logger: RunDataLogger
    root_key: str

class Continuation[P: enum.Enum, H](Protocol):
    async def __call__[FormT: BackendResult, A: ArtifactIdentifier](
        self,
        env: ServiceHost,
        backend: PipelineBackend[P, FormT, H, A]
    ) -> CorePipelineResult[FormT]:
        ...

class AtExit(Protocol):
    async def __call__(
        self,
        run: SourceFields,
        logger: RunDataLogger
    ) -> None:
        ...

@asynccontextmanager
async def cli_pipeline[P: enum.Enum, H](
    args: PipelineArgs,
    thread_id: str,
    summary: RunSummary,
    task_handler: HandlerFactory[P, H],
    design_doc_phase: P,
    at_exit: AtExit | None = None,
    **metadata
) -> AsyncIterator[tuple[StagedPipeline, Continuation[P, H]]]:
    project_root = pathlib.Path(args.project_root).resolve()
    main_contract_path, contract_name = args.main_contract.split(":", 1)

    full_contract_path = pathlib.Path(main_contract_path).resolve()
    if not full_contract_path.is_relative_to(project_root):
        raise ValueError(f"Invalid path: {full_contract_path} doesn't appear in project root {project_root}")

    relative_path = str(full_contract_path.relative_to(project_root))

    # Set up services
    tiered = get_provider_for(tiered=args)

    semaphore = asyncio.Semaphore(args.max_concurrent)

    model = get_model()
    text_log, events_log = setup_autoprove_logging(project_root, thread_id)
    print(f"autoprove logs: {text_log}\n           events: {events_log}", file=sys.stderr)
    install_run_summary(summary)

    disc_cache_ns: tuple[str, ...] | None = (
        user_ns(args.cache_ns, "discovery",
                 discovery_cache_key(str(project_root), relative_path, contract_name))
        if args.cache_ns is not None else None
    )

    init_source = SourceFields(
        relative_path=relative_path,
        contract_name=SolidityIdentifier(contract_name),
        forbidden_read=FS_FORBIDDEN_READ,
        project_root=str(project_root)
    )

    async with (
        standard_connections(provider=tiered.provider_kind, embedder=DefaultEmbedder(model)) as conns,
        async_tool_context(),
        thread_logger(conns.store, {
            "root_thread_id": thread_id,
            "discovery_cache_root": list(disc_cache_ns) if disc_cache_ns is not None else None,
            "memory_ns": args.memory_ns if args.memory_ns is not None else thread_id,
            **metadata
        }, default_logging_ns(uid=None), run_id=summary.run_id) as data_logger
    ):
        try:
            memory_ns = args.memory_ns
            if memory_ns:
                memory_ns = get_uid() + "/" + memory_ns

            models = ModelProvider(
                heavy_model=tiered.heavy,
                lite_model=tiered.lite,
                checkpointer=conns.checkpointer,
            )

            if args.system_doc is None:
                disc_ctx = WorkflowContext.create(
                    conns.memory,
                    thread_id=thread_id,
                    store=conns.store,
                    recursion_limit=args.recursion_limit,
                    memory_namespace=memory_ns,
                    cache_namespace=disc_cache_ns
                )
                system_doc = await run_task(
                    factory=task_handler,
                    info=TaskInfo(
                        label="Design Doc Discovery",
                        phase=design_doc_phase,
                        task_id=DESIGN_DOC_DISCOVERY_TASK_ID
                    ),
                    semaphore=semaphore,
                    fn=lambda: \
                        resolve_design_doc(
                            source=init_source,
                            disc_ctx=disc_ctx,
                            models=models,
                            uploader=conns.uploader
                        )
                )
            else:
                system_doc = pathlib.Path(args.system_doc)
            
            system_doc_doc = await conns.uploader.get_document(system_doc)
            if system_doc_doc is None:
                raise ValueError(f"Fatal error, failed to upload system doc: {system_doc}")

            root_key = root_cache_key(
                project_root=str(project_root),
                contract_name=contract_name,
                relative_path=relative_path,
                system_doc_path=system_doc
            )
            cache_root = user_ns(args.cache_ns, root_key) if args.cache_ns is not None else None

            threat_model = (
                await conns.uploader.get_document(pathlib.Path(threat_path))
                if (threat_path := args.threat_model) is not None else None
            )
            await data_logger("cache_root", {
                "cache_root": list(cache_root) if cache_root is not None else None,
                "contract_name": str(contract_name),
                "memory_ns": memory_ns,
            })
            full_source = SourceCode(
                content=system_doc_doc,
                contract_name=init_source.contract_name,
                forbidden_read=init_source.forbidden_read,
                project_root=init_source.project_root,
                relative_path=init_source.relative_path
            )

            async def cont[FormT: BackendResult, A: ArtifactIdentifier](
                env: ServiceHost,
                backend: PipelineBackend[P, FormT, H, A]
            ) -> CorePipelineResult[FormT]:
                full_ctx = WorkflowContext.create(
                    services=conns.memory,
                    thread_id=thread_id,
                    cache_namespace=cache_root,
                    memory_namespace=memory_ns,
                    recursion_limit=args.recursion_limit,
                    store=conns.store
                )
                run = PipelineRun(
                    ctx=full_ctx,
                    source=full_source,
                    env=env,
                    _semaphore=semaphore,
                    _handler_factory=task_handler
                )
                return await run_pipeline(
                    backend=backend,
                    run=run,
                    interactive=args.interactive,
                    max_bug_rounds=args.max_bug_rounds,
                    threat_model=threat_model
                )
                ...

            yield (StagedPipeline(
                conns=conns, llm_models=models, logger=data_logger,
                embed_model=model,
                root_key=root_key,
                source=SourceCode(
                    content=system_doc_doc,
                    contract_name=init_source.contract_name,
                    forbidden_read=init_source.forbidden_read,
                    project_root=init_source.project_root,
                    relative_path=init_source.relative_path
                )
            ), cont)
        finally:
            if at_exit is not None:
                try:
                    await at_exit(init_source, data_logger)
                except Exception:
                    pass
