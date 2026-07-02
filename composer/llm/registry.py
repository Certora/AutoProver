"""Provider registry + dispatch.

Single table mapping a model name to its provider. Each :class:`ProviderSpec`
pairs a name predicate, the :data:`ProviderKind`, and a factory that builds the
provider's :class:`~composer.llm.provider.ModelProvider`. ``get_provider_for``
and ``provider_for`` dispatch through it; add a row to teach the system a new
provider.
"""

from dataclasses import dataclass
from typing import Callable, Protocol, TYPE_CHECKING, overload, cast

from composer.input.types import ModelConfiguration, ModelOptionsBase, TieredModelOptions
from composer.input.files import FileUploader
from composer.llm.provider import ProviderKind, CacheLevel, ModelProvider
from composer.llm import anthropic as _anthropic
from composer.llm import openai as _openai

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel


@dataclass(frozen=True)
class ProviderSpec:
    """One row of the provider registry: a name predicate, the provider kind it
    maps to, and the factory that builds the provider's ``ModelProvider``."""
    matches: Callable[[str], bool]
    kind: ProviderKind
    build: Callable[[str, ModelConfiguration], ModelProvider]


_PROVIDERS: list[ProviderSpec] = [
    ProviderSpec(
        matches=_anthropic.matches,
        kind="anthropic",
        build=_anthropic.AnthropicModelProvider.create,
    ),
    ProviderSpec(
        matches=_openai.matches,
        kind="openai",
        build=_openai.OpenAIModelProvider.create,
    ),
]


def _lookup(model: str) -> ProviderSpec:
    lowered = model.lower()
    for spec in _PROVIDERS:
        if spec.matches(lowered):
            return spec
    raise ValueError(
        f"Unrecognized model {model!r}: cannot determine its provider. Add a "
        f"ProviderSpec to composer.llm.registry._PROVIDERS when introducing a "
        f"new model family."
    )


def provider_for(model: str) -> ProviderKind:
    """Map a model identifier to its provider family via the registry."""
    return _lookup(model).kind

@dataclass(kw_only=True)
class TieredProviders:
    lite: ModelProvider
    heavy: ModelProvider
    provider_kind: ProviderKind

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
        if lite_model.provider != heavy_model.provider:
            raise ValueError(f"Cannot use different model providers for heavy and lite models: {tiered.lite_model} vs {tiered.heavy_model}")
        return TieredProviders(lite=lite_model, heavy=heavy_model, provider_kind=lite_model.provider)

def uploader_for(provider: ProviderKind) -> FileUploader:
    """Construct the lazily-seeding Files-API uploader for ``provider``."""
    match provider:
        case "anthropic":
            return _anthropic.AnthropicFileUploader.lazy()
        case "openai":
            return _openai.OpenAIFileUploader.lazy()


class LLMFactory(Protocol):
    def __call__(
        self,
        model_name: str,
        *,
        cache_level: CacheLevel | None = None,
        disable_thinking: bool = False,
    ) -> "BaseChatModel": ...


def llm_factory(options: ModelConfiguration) -> LLMFactory:
    """A model-name → chat-model factory bound to ``options``. The tiering layer
    (``ModelProvider`` in ``spec/service_host.py``) calls this per tier with the
    heavy/lite model name; each call resolves the provider and defers the
    cache/thinking choice to ``builder_for``."""
    def build(
        model_name: str,
        *,
        cache_level: CacheLevel | None = None,
        disable_thinking: bool = False,
    ) -> "BaseChatModel":
        return get_provider_for(model_name=model_name, options=options).builder_for(
            cache_level=cache_level, disable_thinking=disable_thinking
        )
    return build
