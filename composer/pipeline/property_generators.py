"""Property generators implemented outside this package.

A property generator replaces the pipeline's own system analysis and per-component property
inference. It returns a :class:`~composer.pipeline.property_set.PropertySet`: the analyzed system
model and the properties of each component of the main contract. The driver rebuilds the units
from that model, matches the set's components to them, runs the post-inference plugin hooks on
each component's properties, and continues as it would after its own extraction. Ranking in a
prioritized run, formalization, and the hand-off to an external mode all run unchanged.

A run uses the native analysis and inference unless it names a generator, with
``--property-generator`` or :data:`PROPERTY_GENERATOR_ENV`. A generator registers a
:class:`PropertyGeneratorLoader` under :data:`PROPERTY_GENERATOR_ENTRY_POINT_GROUP`; the
entry-point name is the generator's name.

See ``PROPERTY_GENERATORS.md`` beside this file for the contract a generator must meet.
"""

import enum
import importlib.metadata
import os
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

from composer.input.files import Document
from composer.pipeline.ecosystem import Ecosystem
from composer.spec.artifacts import ArtifactStore
from composer.spec.system_model import FeatureUnit
from .plugin_api import PluginScope
from .plugins import applies_to, declared_loader
from .property_set import PropertySet
from .ptypes import PipelineRun


#: Entry-point group a property generator registers under. The entry-point name is the
#: generator's name; the target is a :class:`PropertyGeneratorLoader`.
PROPERTY_GENERATOR_ENTRY_POINT_GROUP = "certora.autoprove.property_generators"

PROPERTY_GENERATOR_ENV = "AUTOPROVER_PROPERTY_GENERATOR"

#: The name that selects the pipeline's own analysis and inference. No generator may register it.
NATIVE_PROPERTY_GENERATOR = "native"


def installed_property_generator_names() -> set[str]:
    """The entry-point names registered under :data:`PROPERTY_GENERATOR_ENTRY_POINT_GROUP`.
    Reads metadata only: no generator is imported or constructed."""
    return {
        ep.name
        for ep in importlib.metadata.entry_points(group=PROPERTY_GENERATOR_ENTRY_POINT_GROUP)
    }


def resolve_property_generator(cli: str | None) -> str | None:
    """The run's property generator: an explicit ``--property-generator`` wins, else
    :data:`PROPERTY_GENERATOR_ENV`. ``None`` means the native analysis and inference.

    Resolved before any service starts, so a generator that is not installed fails the run
    up front."""
    raw = cli if cli is not None else os.environ.get(PROPERTY_GENERATOR_ENV)
    if raw is None or not raw.strip():
        return None
    name = raw.strip()
    if name == NATIVE_PROPERTY_GENERATOR:
        return None
    installed = installed_property_generator_names()
    if name not in installed:
        source = "--property-generator" if cli is not None else PROPERTY_GENERATOR_ENV
        choices = ", ".join(sorted({NATIVE_PROPERTY_GENERATOR, *installed}))
        raise ValueError(
            f"Unknown property generator {name!r} from {source}. Installed: {choices}. A "
            f"generator registers under the entry-point group "
            f"{PROPERTY_GENERATOR_ENTRY_POINT_GROUP!r}."
        )
    return name


@dataclass(frozen=True)
class PropertyGenerationContext[P: enum.Enum, H, U: FeatureUnit]:
    """Everything the driver hands a generator.

    ``run`` carries the services (``env``: the run's models), the run's cache root (``ctx``), the
    task runner, the source (``source.content`` is the design document, when there is one) and
    ``run_mode``."""
    run: PipelineRun[P, H]
    #: The run's ecosystem. The returned set's ``ecosystem`` must be its ``name``, and its
    #: ``system_model`` must validate as its ``system_model`` type, whose ``units`` over the main
    #: contract are the components the set's components are matched to.
    ecosystem: Ecosystem[Any, Any, U]
    threat_model: Document | None
    extra_context: Sequence[Document]
    #: The project already carries specifications written by its authors (an evaluate run). The
    #: generated properties must be the full set the contract should satisfy, not only the ones
    #: those specifications already encode.
    existing_specs: bool
    #: Where the run's deliverables live. A generator writes what it delivers beyond the property
    #: set (its own reports, say) through ``write_deliverable``.
    artifact_store: ArtifactStore[Any, Any]
    #: The backend's phase for the generator's own tasks.
    phase: P


class PropertyGeneratorImpl[U: FeatureUnit](ABC):
    """A running generator, built by :meth:`PropertyGeneratorLoader.initialize`."""

    @abstractmethod
    async def generate(self, ctx: PropertyGenerationContext[Any, Any, U]) -> PropertySet:
        """Analyze the system and generate the properties of each component of the main
        contract. Called once per run, in place of the native analysis and extraction.

        Each component of the returned set must name a unit of the main contract, by
        ``display_name`` and ``unit_index``; one that matches no unit fails the run. A unit with
        no component gets no properties. The driver does not cache the result: a generator
        caches its own work under ``ctx.run.ctx``. Exceptions propagate and fail the run."""
        ...


class PropertyGeneratorLoader[U: FeatureUnit](ABC):
    """Registered under :data:`PROPERTY_GENERATOR_ENTRY_POINT_GROUP`.

    Constructed with no arguments. Construction must be trivial: the class is instantiated for
    every run that names the generator, before anything else happens."""

    @property
    @abstractmethod
    def scope(self) -> PluginScope[U]:
        """Which ecosystems this generator accepts, in the same terms a plugin uses."""
        ...

    @abstractmethod
    def initialize(self) -> AbstractAsyncContextManager[PropertyGeneratorImpl[U]]:
        """Acquire the generator's resources and yield it. Entered only for a run that names
        the generator, before the pipeline body starts, and exited when the run returns or
        raises."""
        ...


@asynccontextmanager
async def load_property_generator[U: FeatureUnit](
    name: str | None, unit_type: type[U]
) -> AsyncIterator[PropertyGeneratorImpl[U] | None]:
    """The generator ``name`` for a run over ``unit_type``, initialized for the run's duration,
    or ``None`` for a run that uses the native analysis and inference."""
    if name is None:
        yield None
        return
    loader: PropertyGeneratorLoader[Any] = declared_loader(
        PROPERTY_GENERATOR_ENTRY_POINT_GROUP, name, PropertyGeneratorLoader, "Property generator",
    )
    if not applies_to(loader.scope, unit_type):
        raise RuntimeError(
            f"Property generator {name!r} does not accept {unit_type.__name__} units"
        )
    async with loader.initialize() as impl:
        yield cast(PropertyGeneratorImpl[U], impl)


__all__ = [
    "NATIVE_PROPERTY_GENERATOR",
    "PROPERTY_GENERATOR_ENTRY_POINT_GROUP",
    "PROPERTY_GENERATOR_ENV",
    "PropertyGenerationContext",
    "PropertyGeneratorImpl",
    "PropertyGeneratorLoader",
    "installed_property_generator_names",
    "load_property_generator",
    "resolve_property_generator",
]
