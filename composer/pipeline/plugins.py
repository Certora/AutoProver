
import enum
from dataclasses import dataclass, replace
from typing import Protocol, Callable, Awaitable, Any, Iterable, AsyncIterator, Never
import importlib.metadata
from contextlib import AsyncExitStack, asynccontextmanager
from functools import cached_property


from composer.io.multi_job import TaskInfo
from composer.spec.context import (
    WorkflowContext, SourceCode,
)
from composer.spec.service_host import ServiceHost
from composer.spec.util import string_hash
from .ptypes import PipelineRun
from .plugin_api import PipelinePluginLoader, PipelinePlugin, PluginContext

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
class PluginPhaseRunner[P: enum.Enum]:
    plugin: PipelinePlugin
    _run: PipelineRun[P, Any]
    _phase: tuple[P, str]
    _sub_phase: DisplayStrings
    plugin_id: str

    def bind[C](
        self,
        ctxt: WorkflowContext[C]
    ) -> PluginContext[C]:
        new_loader = self.plugin.load_jinja_template
        env = self._run.env
        env = replace(env, models=replace(env.models, _loader=new_loader))
        async def run[T](
            label: str,
            job: Callable[[], Awaitable[T]]
        ) -> T:
            label = f"({self._sub_phase.display_str}) Plugin {self.plugin.NAME}: {label}"
            return await self._run.runner(
                TaskInfo(f"{self._phase[0]}-{self._sub_phase.id_str}-{self.plugin_id}", label, self._phase[0]),
                job,
            )
        return PluginRunner(
            ctxt,
            env,
            self._run.source,
            run
        )
    

@dataclass
class PluginManager[P: enum.Enum]:
    _plugins: dict[str, PipelinePlugin]
    _run: PipelineRun[P, Any]

    @cached_property
    def plugin_digest(self) -> None | str:
        if not self.plugin_manifest:
            return None
        return string_hash("|".join(self.plugin_manifest))
    
    @cached_property
    def plugin_manifest(self) -> list[str]:
        return sorted(self._plugins.keys())
        
    def bind_phase(
        self, phase: P, label: str
    ) -> "PluginPhaseManager[P]":
        return PluginPhaseManager(
            self._plugins, self._run, (phase, label)
        )

@dataclass
class PluginPhaseManager[P: enum.Enum](PluginManager):
    _phase: tuple[P, str]

    def runners(self, *, sub_phase_id: str, sub_phase_label: str) -> Iterable[PluginPhaseRunner[P]]:
        for (k,v) in self._plugins.items():
            yield PluginPhaseRunner(v, self._run, self._phase, DisplayStrings(sub_phase_id, sub_phase_label), k)

@asynccontextmanager
async def load_plugins[P: enum.Enum](run: PipelineRun[P, Never]) -> AsyncIterator[PluginManager[P]]:
    plugins : dict[str, PipelinePluginLoader] = {}
    for ep in importlib.metadata.entry_points(group="certora.autoprove.plugins"):
        if ep.name in plugins:
            raise RuntimeError(f"Multiple plugins with name: {ep.name}, failing")
        loader = ep.load()
        if not isinstance(loader, type) or not issubclass(loader, PipelinePluginLoader):
            raise RuntimeError(f"Bad plugin declaration: {ep.name}: {ep.module}.{ep.attr} is not a PipelinePluginLoader")
        plugins[ep.name] = loader()
    async with AsyncExitStack() as stack:
        loaded_plugins = {
            k: await stack.enter_async_context(v.initialize()) for (k, v) in plugins.items()
        }
        yield PluginManager(
            loaded_plugins, run
        )
