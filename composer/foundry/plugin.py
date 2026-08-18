"""The foundry backend's plugin-extension surface.

What a plugin imports to contribute tools to the foundry test author: the
:class:`FoundryTools` provider class (deriving it IS the declaration — see
``composer.pipeline.plugin_api``) and the :class:`FoundryState` view its hooks can
stage at tool-invocation time. The prover counterpart is
``composer.spec.source.plugin``.
"""

import pathlib
from abc import abstractmethod
from dataclasses import dataclass
from typing import AsyncContextManager, Callable, Sequence

from composer.pipeline.plugin_api import (
    FormalizationTool, PipelinePlugin, PluginToolContext, ProvidedTools,
)
from composer.spec.system_model import FeatureUnit
from composer.spec.types import PropertyFormulation


@dataclass
class FoundryState:
    # The foundry project root the run executes in.
    working_dir: pathlib.Path
    # The project's configured test directory (absolute; from foundry.toml's
    # default profile). May not exist until a draft is first staged into it.
    test_dir: pathlib.Path
    # The author's current .t.sol draft buffer; None until a draft is written.
    curr_test: str | None

type FoundryStateReader[T] = Callable[[T], AsyncContextManager[FoundryState]]


class FoundryTools[U: FeatureUnit](PipelinePlugin[U]):
    """Contributes tools to the foundry test author; the staged view is
    :class:`FoundryState`. See ``composer.spec.source.plugin.CertoraProverTools``
    for the reader contract."""

    @abstractmethod
    async def foundry_tools[T](
        self,
        comp: U,
        prop: Sequence[PropertyFormulation],
        tool_context: PluginToolContext[FormalizationTool],
        st: type[T],
        state_reader: FoundryStateReader[T]
    ) -> ProvidedTools | None:
        ...
