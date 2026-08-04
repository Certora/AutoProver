from typing import AsyncContextManager, Protocol, Callable, Awaitable, Any
from dataclasses import dataclass
from functools import cached_property
from abc import ABC, abstractmethod
from graphcore.graph import TemplateLoader
from jinja2.loaders import BaseLoader, PackageLoader, ChoiceLoader, PrefixLoader

from composer.templates.loader import base_loader, load_jinja_template, _autoescape
from composer.spec.system_model import FeatureUnit
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

class PipelinePlugin[U: FeatureUnit](ABC):
    """A pipeline plugin, parameterized by the unit type its hooks accept."""

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
        comp: U,
        run: PluginContext[PrePropertyInference]
    ) -> AnyPropertyGenerationInput | None:
        return None

    async def post_process_property_inference(
        self,
        comp: U,
        run: PluginContext[PostPropertyInference],
        props: list[PropertyFormulation]
    ) -> list[PropertyFormulation]:
        return props


# ---------------------------------------------------------------------------
# Scope — which runs a plugin applies to
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AnyEcosystem:
    """The plugin's hooks accept :class:`~composer.spec.system_model.FeatureUnit`, so it runs on
    every ecosystem. Carries no unit type: there is nothing to match against."""


@dataclass(frozen=True)
class ForEcosystem[U: FeatureUnit]:
    """The plugin's hooks accept one ecosystem's concrete unit, so it runs only on runs whose
    ``Ecosystem.unit_type`` is that unit (or a subclass of it).

    The unit type is carried as a *value* because narrowing happens at the entry-point boundary,
    where nothing static survives."""

    unit: type[U]


#: What a loader declares to say which runs its plugin applies to. A sum type rather than
#: predicates or flags, so each variant carries exactly the fields its own matching needs —
#: :class:`AnyEcosystem` has no unit to match, :class:`ForEcosystem` has one.
#:
#: Extension point for backend-specific plugins: add a ``ForBackend[U]`` variant here carrying
#: both the ``unit`` and the backend type (``backend: type[PipelineBackend[..., U, ...]]``), and a
#: matching case in ``composer.pipeline.plugins._applies`` that additionally does
#: ``isinstance(backend, scope.backend)``. That needs the backend threaded into ``load_plugins``
#: alongside ``unit_type``; ``run_pipeline`` already holds it, so the plumbing is one parameter.
#: Checking the unit *as well as* the backend would be deliberate redundancy — it turns a
#: mis-paired declaration into a startup no-match rather than a mid-run ``AttributeError``.
type PluginScope[U: FeatureUnit] = AnyEcosystem | ForEcosystem[U]


class PipelinePluginLoader[U: FeatureUnit](ABC):
    """Declares a plugin's :data:`PluginScope` and builds it.

    Scope lives here rather than on the plugin so the host can skip a plugin that doesn't apply
    *without* running ``initialize``"""

    @property
    @abstractmethod
    def scope(self) -> PluginScope[U]:
        ...

    @abstractmethod
    def initialize(
        self
    ) -> AsyncContextManager[PipelinePlugin[U]]:
        ...
