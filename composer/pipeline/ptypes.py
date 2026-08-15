
import asyncio
import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Callable, Awaitable, TypedDict

from pydantic import BaseModel

from composer.io.multi_job import HandlerFactory, TaskInfo, run_task, ConversationContextProvider
from composer.spec.context import (
    WorkflowContext, SourceCode, SourceFields
)
from composer.spec.service_host import ServiceHost
from composer.spec.system_model import (
    FeatureUnit,
)
from composer.spec.types import Curtailed, PropertyFormulation, FormalResult
from composer.spec.source.report.collect import ReportableResult


class BackendResult(FormalResult, ReportableResult, Protocol):
    ...


class GaveUp(BaseModel):
    """The single, unified give-up signal (replaces the two structurally-identical copies in
    spec.source.author and foundry.author)."""
    reason: str

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
    """The backend's contribution to the shared analysis call. The analyzed type is the
    ecosystem's ``App`` (SourceApplication for EVM; the prover's harnessed lift is its
    prepare_system, not analysis)."""
    analysis_key: str
    properties_key: str
    extra_input: list[str | dict] = field(default_factory=list)


@dataclass
class BackendJob[U: FeatureUnit]:
    # ``U`` is the *formalized unit* type the paired backend consumes (EVM's
    # ``ContractComponentInstance``, Solana's invariant unit, …) — see ``FeatureUnit``. The shared
    # driver is generic over it; a backend narrows it to its concrete unit and reads its members
    # without casts.
    feat: U
    props: list[PropertyFormulation]

class FinalProperties(BaseModel):
    """A component's property batch as it left the property pipeline — after
    every post-inference plugin rewrite. This is exactly the input
    ``FORMALIZATION_KEY`` is derived from, so an offline walker
    can reconstruct the formalization edge without
    sniffing the store. Written by the driver at formalization time; the
    pre-rewrite batch remains ``_BugAnalysisCache``."""
    items: list[PropertyFormulation]


@dataclass(frozen=True)
class Delivered[FormT: BackendResult]:
    """A formalization result and the project-relative path it was persisted to. The path exists
    only because the result does, so the two travel together rather than as independent fields.
    On its own this means a *successful* formalization; wrapped in a `Curtailed` it is the
    quarantined partial a budget-cut author managed to publish."""
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
class ComponentOutcome[FormT: BackendResult, U: FeatureUnit](BackendJob[U]):
    result: Delivered[FormT] | GaveUp | BaseException | Curtailed[Delivered[FormT]]

@dataclass
class CorePipelineResult[FormT: BackendResult]:
    # The result rollup is unit-agnostic — it only reads ``feat.display_name`` (a ``FeatureUnit``
    # member) — so it stays mono in the unit type and widens the outcomes to the protocol.
    n_components: int
    n_properties: int
    outcomes: list[ComponentOutcome[FormT, FeatureUnit]]
    failures: list[str]

    @property
    def n_delivered(self) -> int:
        """Components that produced a deliverable — a successful formalization. Everything else
        (a ``GaveUp``, a budget ``Curtailed``, or a crash) is a component with no reliable
        result."""
        return sum(1 for o in self.outcomes if isinstance(o.result, Delivered))

    @property
    def all_failed(self) -> bool:
        """Every attempted component ended without a reliable deliverable (gave up, crashed, or
        was budget-curtailed) — the run is a total failure. Guarded on a non-empty outcome set so
        "all of nothing" is never reported as failure (the driver raises before returning in the
        no-outcomes case anyway)."""
        return bool(self.outcomes) and self.n_delivered == 0

class PhaseBudget(TypedDict):
    """Per-phase spending *caps* (USD). Ceilings, not allotments: they bound
    how much a single phase may hog, and need not sum to the run total —
    unspent phase money stays in the run pool for later phases."""
    formalization_preparation: float
    system_analysis: float
    system_preparation: float
    property_extraction: float
    formalization: float


@dataclass(frozen=True)
class RunBudget:
    """The run's token-cost budget: ``total`` is the pool (the real bound on
    overall spend); ``caps`` are the per-phase ceilings drawn against it."""
    total: float
    caps: PhaseBudget

__all__ = [
    "CorePipelineResult",
    "ComponentOutcome",
    "Curtailed",
    "Delivered",
    "BackendJob",
    "SystemAnalysisSpec",
    "CorePhases",
    "GaveUp",
    "BackendResult",
    "PipelineRun"
]
