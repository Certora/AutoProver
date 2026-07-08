
import asyncio
import enum
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Callable, Awaitable, TypedDict, AsyncContextManager, AsyncIterator
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager

from pydantic import BaseModel

from composer.workflow.services import IndexedConnections
from composer.io.multi_job import HandlerFactory, TaskInfo, run_task, ConversationContextProvider
from composer.spec.artifacts import ArtifactStore
from composer.spec.context import (
    WorkflowContext, CacheKey, Properties, ComponentGroup, SourceCode, SourceFields
)
from composer.spec.service_host import ServiceHost
from composer.spec.system_model import (
    SourceApplication, ContractInstance, ContractComponentInstance, AnyApplication
)
from composer.spec.types import PropertyFormulation, FormalResult, ArtifactIdentifier
from composer.spec.system_analysis import run_component_analysis
from composer.spec.prop_inference import run_property_inference
from composer.spec.util import string_hash
from composer.input.files import Document
from composer.spec.source.report.build import build_report
from composer.spec.source.report.collect import ReportableResult, ReportComponentInput, Verdict
from composer.spec.source.report.schema import RuleName, ReportBackend
from composer.spec.source.report import build as report_build
from composer.spec.source.task_ids import SYSTEM_ANALYSIS_TASK_ID, REPORT_TASK_ID


class BackendResult(FormalResult, ReportableResult, Protocol):
    ...


class GaveUp(BaseModel):
    """The single, unified give-up signal (replaces the two structurally-identical copies in
    spec.source.author and foundry.author)."""
    reason: str

class ServiceHostBuilder(Protocol):
    def __call__(self, cache_root: str | None, conns: IndexedConnections) -> AsyncContextManager[ServiceHost]:
        ...

@dataclass
class TaskRunnerHost[P: enum.Enum, H, S: SourceFields, C]:
    ctx: WorkflowContext[C]
    source: S
    _handler_factory: HandlerFactory[P, H]
    _semaphore: asyncio.Semaphore

    async def runner[T](
        self,
        task_info: TaskInfo[P],
        job: Callable[[], Awaitable[T]] | Callable[[ConversationContextProvider], Awaitable[T]],
    ) -> T:
        return await run_task(
            factory=self._handler_factory,
            fn=job,
            info=task_info,
            semaphore=self._semaphore
        )
    

@dataclass
class PipelineInitContext[P: enum.Enum, H](TaskRunnerHost[P, H, SourceFields, None]):
    _builder: Callable[[str], AsyncContextManager[ServiceHost]]

    async def to_run(
        self,
        root_key: str,
        ctxt: WorkflowContext[None],
        design_doc: Document
    ) -> AsyncIterator["PipelineRun[P, H]"]:
        async with self._builder(root_key) as svc:
            yield PipelineRun(
                ctx=ctxt,
                env=svc,
                source=SourceCode(
                    content=design_doc,
                    contract_name=self.source.contract_name,
                    forbidden_read=self.source.forbidden_read,
                    project_root=self.source.project_root,
                    relative_path=self.source.relative_path
                ),
                _handler_factory = self._handler_factory,
                _semaphore = self._semaphore
            )


# ---- run-scoped shared infra, handed to every hook ---------------------------
@dataclass
class PipelineRun[P: enum.Enum, H](TaskRunnerHost[P, H, SourceCode, None]):
    env: ServiceHost


class CorePhases[P: enum.Enum](TypedDict):
    """The backend maps its own phase enum onto the three core phases the driver tags."""
    analysis: P
    extraction: P
    formalization: P
    report: P


@dataclass(frozen=True)
class SystemAnalysisSpec:
    """The backend's contribution to the shared analysis call. The analyzed type is always
    SourceApplication (the prover's harnessed lift is its prepare_system, not analysis)."""
    analysis_key: str
    extra_input: list[str | dict] = field(default_factory=list)


@dataclass
class BackendJob:
    feat: ContractComponentInstance
    props: list[PropertyFormulation]

@dataclass(frozen=True)
class Delivered[FormT: BackendResult]:
    """A successful formalization and the project-relative path it was persisted to. The path exists
    only because the result does, so the two travel together rather than as independent fields."""
    result: FormT
    deliverable: Path

    @property
    def unit_file(self) -> str:
        # The verdict-disambiguation key (file, unit_name), never displayed; must match what the
        # verdict fetchers emit — the prover's is `Path(loc.file).name` (basename) — so basename,
        # not the full project-relative path.
        return self.deliverable.name

    @property
    def run_link(self) -> str | None:
        return self.result.output_link

@dataclass
class ComponentOutcome[FormT: BackendResult](BackendJob):
    result: Delivered[FormT] | GaveUp | BaseException

@dataclass
class CorePipelineResult[FormT: BackendResult]:
    n_components: int
    n_properties: int
    outcomes: list[ComponentOutcome[FormT]]
    failures: list[str]

__all__ = [
    "CorePipelineResult",
    "ComponentOutcome",
    "Delivered",
    "BackendJob",
    "SystemAnalysisSpec",
    "CorePhases",
    "PipelineInitContext",
    "GaveUp",
    "BackendResult",
    "PipelineRun"
]