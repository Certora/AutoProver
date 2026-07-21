"""Provider registry + dispatch.

Single table mapping a model name to its provider. Each :class:`ProviderSpec`
pairs a name predicate, the :data:`ProviderKind`, and a factory that builds the
provider's :class:`~composer.llm.provider.ModelProvider`. ``get_provider_for``
and ``provider_for`` dispatch through it; add a row to teach the system a new
provider.
"""

from dataclasses import dataclass
from typing import overload, cast
from functools import cache
import importlib.metadata

from composer.input.types import ModelConfiguration, ModelOptionsBase, TieredModelOptions
from composer.llm.provider import ProviderService, ModelProvider
from .provider import ProviderSpec

LLM_PROVIDER_GROUP = "certora.autoprove.llm_provider"

@cache
def _loader_providers() -> list[ProviderSpec]:
    to_ret : list[ProviderSpec] = []
    for ep in importlib.metadata.entry_points(
        group=LLM_PROVIDER_GROUP
    ):
        prov = ep.load()
        if not isinstance(prov, ProviderSpec):
            raise ValueError(f"Could not load provider backend: {ep.name} with {ep.module}.{ep.value}")
        to_ret.append(prov)
    return to_ret

def _lookup(model: str) -> ProviderSpec:
    lowered = model.lower()
    for spec in _loader_providers():
        if spec.matches(lowered):
            return spec
    raise ValueError(
        f"Unrecognized model {model!r}: cannot determine its provider. Add a "
        f"ProviderSpec to the `{LLM_PROVIDER_GROUP}` importlib entrypoint when introducing a "
        f"new model family."
    )


@dataclass(kw_only=True)
class TieredProviders:
    lite: ModelProvider
    heavy: ModelProvider
    provider_service: ProviderService

@overload
def get_provider_for(*, model_name: str, options: ModelConfiguration) -> ModelProvider:
    ...

@overload
def get_provider_for(*, options: ModelOptionsBase) -> ModelProvider:
    ...

@overload
def get_provider_for(*, tiered: TieredModelOptions) -> TieredProviders:
    ...


def get_provider_for(
    *,
    model_name: str | None = None,
    options: ModelConfiguration | None = None,
    tiered : TieredModelOptions | None = None
) -> ModelProvider | TieredProviders:
    if model_name is not None:
        assert options is not None
        return _lookup(model_name).build(model_name, options)
    elif options is not None:
        down = cast(ModelOptionsBase, options)
        return _lookup(down.model).build(down.model, options)
    else:
        assert tiered is not None
        lite_model = _lookup(tiered.lite_model).build(tiered.lite_model, tiered)
        heavy_model = _lookup(tiered.heavy_model).build(tiered.heavy_model, tiered)
        if type(lite_model.provider) is not type(heavy_model.provider):
            raise ValueError(f"Cannot use different model providers for heavy and lite models: {tiered.lite_model} vs {tiered.heavy_model}")
        return TieredProviders(lite=lite_model, heavy=heavy_model, provider_service=lite_model.provider)
