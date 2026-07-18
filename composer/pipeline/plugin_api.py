from typing import AsyncContextManager, Protocol, Callable, Awaitable, Any
from functools import cached_property
from abc import ABC, abstractmethod
from graphcore.graph import TemplateLoader
from jinja2.loaders import BaseLoader, PackageLoader, ChoiceLoader, PrefixLoader

from composer.templates.loader import base_loader, load_jinja_template, _autoescape
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

import jinja2
from jinja2 import Environment, ChoiceLoader, PrefixLoader

class _NonStrippingPrefixLoader(PrefixLoader):
    """Like PrefixLoader, but the compiled template keeps its prefix in .name,
    so join_path can see which namespace a template came from."""
    def load(self, environment, name, globals=None):
        loader, local_name = self.get_loader(name)
        if globals is None:
            globals = {}
        try:
            source, filename, uptodate = loader.get_source(environment, local_name)
        except jinja2.TemplateNotFound as e:
            raise jinja2.TemplateNotFound(name) from e
        code = environment.compile(source, name, filename)   # full name, not local_name
        return environment.template_class.from_code(environment, code, globals, uptodate)

class _PluginEnvironment(Environment):
    namespace_prefixes = ("autoprover/",)
    def join_path(self, template, parent):
        # already namespaced -> leave alone
        if template.startswith(self.namespace_prefixes):
            return template
        # inherit the parent's namespace for bare references
        if parent and parent.startswith(self.namespace_prefixes):
            prefix = next(p for p in self.namespace_prefixes if parent.startswith(p))
            return prefix + template
        return template

class PipelinePlugin(ABC):
    NAME: str

    def plugin_loader(self) -> BaseLoader | None:
        t = type(self).__module__
        try:
            return PackageLoader(t)
        except ValueError:
            return None
    
    @cached_property
    def load_jinja_template(self) -> TemplateLoader:
        loader = self.plugin_loader()
        if loader is None:
            return load_jinja_template
        
    
        new_jinja_loader = ChoiceLoader([
            loader,
            _NonStrippingPrefixLoader({
                "autoprover": base_loader
            })
        ])
        compilation_env = _PluginEnvironment(loader=new_jinja_loader, autoescape=_autoescape)
        def _load_jinja_template(template_name: str, **kwargs: Any) -> str:
            """Load and render a Jinja template from the script directory"""
            template = compilation_env.get_template(template_name)
            return template.render(**kwargs)
        return _load_jinja_template


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
