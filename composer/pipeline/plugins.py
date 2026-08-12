
import enum
from dataclasses import dataclass, replace
from typing import Protocol, Callable, Awaitable, Any, Iterable, AsyncIterator, Never, cast
import importlib.metadata
from contextlib import AsyncExitStack, asynccontextmanager
from functools import cached_property


from composer.io.multi_job import TaskInfo
from composer.spec.context import (
    WorkflowContext, SourceCode,
)
from composer.spec.service_host import ServiceHost
from composer.spec.system_model import FeatureUnit
from composer.spec.util import string_hash
from .ptypes import PipelineRun
from .plugin_api import (
    AnyEcosystem, ForEcosystem, PipelinePluginLoader, PipelinePlugin, PluginContext, PluginScope,
)

class _RunnerFun(Protocol):
    async def __call__[T](self, label: str, job: Callable[[], Awaitable[T]]) -> T: ...

@dataclass
class PluginRunner[C]:
    ctx: WorkflowContext[C]
    env: ServiceHost
    source: SourceCode

    runner: _RunnerFun

class DisplayStrings(tuple[str, str]):
    __slots__ = ()

    @property
    def display_str(self) -> str:
        return self[1]
    
    @property
    def id_str(self) -> str:
        return self[0]
    
    def __new__(
        cls, id: str, display: str
    ):
        return super().__new__(cls, (id, display))

@dataclass
class PluginPhaseRunner[P: enum.Enum, U: FeatureUnit]:
    plugin: PipelinePlugin[U]
    _run: PipelineRun[P, Any]
    _phase: tuple[P, str]
    _sub_phase: DisplayStrings
    plugin_id: str

    def bind[C](
        self,
        uid: str,
        ctxt: WorkflowContext[C]
    ) -> PluginContext[C]:
        new_loader = self.plugin.load_jinja_template
        env = self._run.env
        env = replace(env, models=replace(env.models, _loader=new_loader))
        async def run[T](
            label: str,
            job: Callable[[], Awaitable[T]]
        ) -> T:
            label = f"(Plugin {self._sub_phase.display_str}) {self.plugin.NAME}: {label}"
            return await self._run.runner(
                TaskInfo(f"{self._phase[0].name}-{self._sub_phase.id_str}-{self.plugin_id}-{uid}", label, self._phase[0]),
                job,
            )
        return PluginRunner(
            ctxt,
            env,
            self._run.source,
            run
        )
    

PLUGIN_ENTRY_POINT_GROUP = "certora.autoprove.plugins"


def _applies(scope: PluginScope[Any], unit_type: type) -> bool:
    """Whether a plugin declaring ``scope`` applies to a run over ``unit_type``."""
    match scope:
        case AnyEcosystem():
            return True
        case ForEcosystem(unit=unit):
            return issubclass(unit_type, unit)


def _declared_loaders() -> Iterable[tuple[str, PipelinePluginLoader[Any]]]:
    """Every installed plugin's (entry-point name, loader), scope not yet consulted. Constructing
    a loader is documented to be trivial — resources belong in ``initialize`` — so this is safe to
    do for plugins that turn out not to apply."""
    seen: set[str] = set()
    for ep in importlib.metadata.entry_points(group=PLUGIN_ENTRY_POINT_GROUP):
        if ep.name in seen:
            raise RuntimeError(f"Multiple plugins with name: {ep.name}, failing")
        seen.add(ep.name)
        loader = ep.load()
        if not isinstance(loader, type) or not issubclass(loader, PipelinePluginLoader):
            raise RuntimeError(f"Bad plugin declaration: {ep.name}: {ep.module}.{ep.attr} is not a PipelinePluginLoader")
        yield ep.name, loader()


def applicable_plugin_manifest[U: FeatureUnit](unit_type: type[U]) -> list[str]:
    """Sorted names of the installed plugins that apply to a run over ``unit_type`` — the manifest
    ``load_plugins`` will end up initializing, computable without initializing anything.

    Scoped rather than merely *installed*: this manifest is what ``manifest_digest`` hashes into
    every per-component cache key, so an installed-but-inapplicable plugin (a Solana plugin during
    an EVM run) must not perturb those keys."""
    return sorted(
        name for name, loader in _declared_loaders() if _applies(loader.scope, unit_type)
    )


def manifest_digest(manifest: list[str]) -> str | None:
    """Digest of a sorted plugin manifest as suffixed onto per-component
    cache keys; ``None`` when no plugins are active."""
    if not manifest:
        return None
    return string_hash("|".join(manifest))


@dataclass
class PluginManager[P: enum.Enum, U: FeatureUnit]:
    """The plugins that apply to this run, already narrowed to ones whose hooks accept ``U``."""

    _plugins: dict[str, PipelinePlugin[U]]
    _run: PipelineRun[P, Any]

    @cached_property
    def plugin_digest(self) -> None | str:
        return manifest_digest(self.plugin_manifest)

    @cached_property
    def plugin_manifest(self) -> list[str]:
        return sorted(self._plugins.keys())

    def bind_phase(
        self, phase: P, label: str
    ) -> "PluginPhaseManager[P, U]":
        return PluginPhaseManager(
            self._plugins, self._run, (phase, label)
        )

@dataclass
class PluginPhaseManager[P: enum.Enum, U: FeatureUnit](PluginManager[P, U]):
    _phase: tuple[P, str]

    def runners(self, *, sub_phase_id: str, sub_phase_label: str, sorted_run: bool = False) -> Iterable[PluginPhaseRunner[P, U]]:
        to_iter : Iterable[tuple[str, PipelinePlugin[U]]] = sorted(
            self._plugins.items(), key=lambda r: r[0]
        ) if sorted_run else self._plugins.items()
        for (k,v) in to_iter:
            yield PluginPhaseRunner(v, self._run, self._phase, DisplayStrings(sub_phase_id, sub_phase_label), k)

@asynccontextmanager
async def load_plugins[P: enum.Enum, U: FeatureUnit](
    run: PipelineRun[P, Never], unit_type: type[U]
) -> AsyncIterator[PluginManager[P, U]]:
    """Initialize the installed plugins that apply to a run over ``unit_type``.

    Inapplicable plugins are dropped *before* ``initialize``, so a plugin for another ecosystem
    never acquires its resources during this run."""
    loaders: dict[str, PipelinePluginLoader[U]] = {}
    for name, loader in _declared_loaders():
        if not _applies(loader.scope, unit_type):
            continue
        # The one cast in this design. Entry points are resolved dynamically, so nothing static
        # survives to here; the `_applies` check above is the runtime evidence that this loader's
        # hooks accept `unit_type`. Everything downstream of this line is honestly typed.
        loaders[name] = cast(PipelinePluginLoader[U], loader)
    async with AsyncExitStack() as stack:
        loaded_plugins = {
            k: await stack.enter_async_context(v.initialize()) for (k, v) in loaders.items()
        }
        yield PluginManager(
            loaded_plugins, run
        )
