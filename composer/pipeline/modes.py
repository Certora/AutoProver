"""Run modes implemented outside this package.

A run mode in :data:`~composer.pipeline.run_mode.EXTERNAL_MODES` shares the pipeline's front half
with every other mode (system analysis, ``prepare_system``, property extraction with plugins) and
then hands what it extracted to an implementation registered under the entry-point group
:data:`~composer.pipeline.run_mode.MODE_ENTRY_POINT_GROUP`. The driver never runs
``prepare_formalization`` or the per-component formalization for such a run. The implementation
returns the report and its verdict on the run; the driver writes the report and derives the exit
status from that verdict.

See ``MODES.md`` beside this file for the contract an implementation must meet.
"""

import enum
import importlib.metadata
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, ClassVar, cast

from pydantic import BaseModel, Field

from composer.input.files import Document
from composer.spec.artifacts import ArtifactStore
from composer.spec.source.report.schema import AutoProverReport
from composer.spec.system_model import FeatureUnit
from .plugin_api import PluginScope
from .plugins import applies_to
from .ptypes import ExtractedBatch, ExtractionFailure, PipelineRun
from .run_mode import EXTERNAL_MODES, MODE_ENTRY_POINT_GROUP, RunMode


@dataclass(frozen=True)
class ModeContext[P: enum.Enum, H, U: FeatureUnit, Main]:
    """Everything the driver has when it hands a run to a mode implementation.

    ``run`` carries the services (``env``), the run's cache root (``ctx``), the task runner
    (``runner`` / ``cpu_runner``), the source, and ``run_mode``. ``batches`` and
    ``extraction_failures`` partition the components the ecosystem found in ``main``: a component
    is in at most one of the two, and a component whose inference produced no properties is in
    neither."""
    run: PipelineRun[P, H]
    #: The located main of the analyzed program (``prepared.main``): EVM's ``ContractInstance``.
    main: Main
    #: Per component: the unit, its post-plugin properties, and its cache namespace.
    batches: Sequence[ExtractedBatch[U]]
    extraction_failures: Sequence[ExtractionFailure[U]]
    threat_model: Document | None
    extra_context: Sequence[Document]
    #: Where the run's deliverables live. The driver writes the returned report through it.
    artifact_store: ArtifactStore[Any, Any]
    #: The plugin manifest the extraction ran with (the same list the report records).
    active_plugins: list[str] = field(default_factory=list)


class ModeResult(BaseModel):
    """What a mode implementation returns."""
    report: AutoProverReport
    #: The mode's verdict on the run. ``False`` makes the run exit non-zero.
    succeeded: bool
    #: One line per problem worth printing in the end-of-run summary. Not part of the report.
    failures: list[str] = Field(default_factory=list)


class RunModeImpl[U: FeatureUnit, Main](ABC):
    """A running mode implementation, built by :meth:`RunModeLoader.initialize`."""

    @abstractmethod
    async def run(self, ctx: ModeContext[Any, Any, U, Main]) -> ModeResult:
        """Do the mode's work and return its report. Called once per run, after extraction.

        Exceptions propagate and fail the run, as a crash in formalization would not: the
        report is this call's whole deliverable."""
        ...


class RunModeLoader[U: FeatureUnit, Main](ABC):
    """Registered under :data:`~composer.pipeline.run_mode.MODE_ENTRY_POINT_GROUP`, with the
    entry-point name equal to ``name.value``.

    Constructed with no arguments. Construction must be trivial: the class is instantiated for
    every run in its mode, before anything else happens."""

    #: The mode this loader implements.
    name: ClassVar[RunMode]

    @property
    @abstractmethod
    def scope(self) -> PluginScope[U]:
        """Which ecosystems this implementation accepts, in the same terms a plugin uses."""
        ...

    @abstractmethod
    def initialize(self) -> AbstractAsyncContextManager[RunModeImpl[U, Main]]:
        """Acquire the implementation's resources and yield it. Entered only for a run in
        ``name``, before the pipeline body starts, and exited when the run returns or raises."""
        ...


def _declared_loader(mode: RunMode) -> RunModeLoader[Any, Any]:
    """The installed loader for ``mode``. Raises when there is none, more than one, or the entry
    point names something that is not a :class:`RunModeLoader` for ``mode``."""
    found = [
        ep for ep in importlib.metadata.entry_points(group=MODE_ENTRY_POINT_GROUP)
        if ep.name == mode.value
    ]
    if not found:
        raise RuntimeError(
            f"Run mode {mode.value!r} has no implementation installed: register one under the "
            f"entry-point group {MODE_ENTRY_POINT_GROUP!r} with the name {mode.value!r}."
        )
    if len(found) > 1:
        raise RuntimeError(
            f"Multiple implementations of run mode {mode.value!r} under "
            f"{MODE_ENTRY_POINT_GROUP!r}, failing"
        )
    ep = found[0]
    loader = ep.load()
    if not isinstance(loader, type) or not issubclass(loader, RunModeLoader):
        raise RuntimeError(
            f"Bad run-mode declaration: {ep.name}: {ep.module}.{ep.attr} is not a RunModeLoader"
        )
    if loader.name is not mode:
        raise RuntimeError(
            f"Bad run-mode declaration: {ep.name}: {ep.module}.{ep.attr} implements "
            f"{loader.name.value!r}, not {mode.value!r}"
        )
    return cast(RunModeLoader[Any, Any], loader())


@asynccontextmanager
async def load_run_mode[U: FeatureUnit](
    mode: RunMode, unit_type: type[U]
) -> AsyncIterator[RunModeImpl[U, Any] | None]:
    """The implementation of ``mode`` for a run over ``unit_type``, initialized for the run's
    duration, or ``None`` for a mode this package implements itself.

    Nothing is looked up for the built-in modes, so an installed external mode costs those runs
    nothing. The mode is not in the plugin manifest and does not enter any cache key."""
    if mode not in EXTERNAL_MODES:
        yield None
        return
    loader = _declared_loader(mode)
    if not applies_to(loader.scope, unit_type):
        raise RuntimeError(
            f"Run mode {mode.value!r}: the installed implementation does not accept "
            f"{unit_type.__name__} units"
        )
    async with loader.initialize() as impl:
        # Narrowed by the scope check above, as ``load_plugins`` narrows a plugin loader.
        yield cast(RunModeImpl[U, Any], impl)


__all__ = [
    "ModeContext",
    "ModeResult",
    "RunModeImpl",
    "RunModeLoader",
    "load_run_mode",
    "MODE_ENTRY_POINT_GROUP",
]
