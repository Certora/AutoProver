from typing import AsyncContextManager, Protocol, Callable, Awaitable
from abc import ABC, abstractmethod

from composer.spec.system_model import ContractComponentInstance
from composer.pipeline.ptypes import PipelineRun
from composer.spec.context import WorkflowContext, SourceCode
from composer.spec.service_host import ServiceHost
from composer.spec.types import PropertyFormulation
from composer.spec.prop_inference import AnyPropertyGenerationInput

class PluginContext[C](Protocol):
    @property
    def ctx(self) -> WorkflowContext[C]:
        ...
    
    @property
    def env(self) -> ServiceHost:
        ...

    @property
    def source(self) -> SourceCode:
        ...

    async def runner[T](
        self,
        label: str,
        job: Callable[[], Awaitable[T]]
    ) -> T:
        ...

class PrePropertyInference:
    pass

class PostPropertyInference:
    pass

class PipelinePlugin(ABC):
    NAME: str

    async def property_inference_input_hook(
        self,
        comp: ContractComponentInstance,
        run: PluginContext[PrePropertyInference]
    ) -> AnyPropertyGenerationInput | None:
        return None

    async def post_process_property_inference(
        self,
        comp: ContractComponentInstance,
        run: PluginContext[PostPropertyInference],
        props: list[PropertyFormulation]
    ) -> list[PropertyFormulation]:
        return props


class PipelinePluginLoader(ABC):
    @abstractmethod
    def initialize(
        self
    ) -> AsyncContextManager[PipelinePlugin]:
        ...
