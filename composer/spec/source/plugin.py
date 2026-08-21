"""The prover backend's plugin-extension surface.

What a plugin imports to contribute tools to the prover's CVL author: the
:class:`CertoraProverTools` provider class (deriving it IS the declaration — see
``composer.pipeline.plugin_api``) and the :class:`ProverState` view its hooks can
stage at tool-invocation time. Lives here rather than in the plugin API so the
core/plugin layers stay backend-agnostic; the author hands the provider class to
the driver's binder as a ``ToolExtension`` (``composer.pipeline.core``).
"""

import pathlib
from abc import abstractmethod
from dataclasses import dataclass
from typing import AsyncContextManager, Callable, Sequence, Protocol

from composer.pipeline.plugin_api import (
    FormalizationTool, PipelinePlugin, PluginToolContext, ProvidedTools,
)
from composer.spec.system_model import FeatureUnit
from composer.spec.types import PropertyFormulation
from composer.prover.core import ProverReport, ProverCallbacks, CexHandler
from composer.io.task_host import TaskHost

class ProverRunner(Protocol):
    """One ad-hoc prover run: stages the spec/conf into ``working_dir`` for the
    duration of the call and forwards to ``run_prover``. ``config`` entries
    override the author's current prover config for this run only."""
    async def __call__(
        self,
        *,
        curr_spec: str,
        working_dir: str,
        cex_handler: CexHandler,
        callbacks: ProverCallbacks,
        tool_call_id: str,
        rules: list[str] | None = None,
        **config,
    ) -> ProverReport | str:
        ...

class EditProposer(Protocol):
    """A plugin's narrowed window onto the run's edit store: stage a source
    edit for the CVL author to consider. Proposing only records the edit and
    returns its id — nothing changes until the author applies that id through
    its edit-management tools. The implementation stamps the proposing
    plugin's identity onto the record; attribution is not the proposer's to
    choose.

    ``vfs`` is the proposer's overlay *relative to the staged working copy*
    its :class:`CVLAuthorState` was read with (the only view a plugin has).
    The implementation completes it into the edit store's full-snapshot form —
    merging the overlay the staged copy was materialized from — before
    committing."""

    async def propose(
        self, vfs: dict[str, str], *, executive_summary: str, why_sound: str
    ) -> str:
        ...


@dataclass
class CVLAuthorState:
    # The run root the prover would execute in: the project itself, or a temporary
    # materialization of the author's working copy (lifetime = the read).
    working_dir: pathlib.Path
    curr_spec: str | None
    prover_runner: ProverRunner
    host: TaskHost
    # Propose source edits for the author to apply; records carry the
    # proposing plugin's attribution.
    edit_store: EditProposer

type ProverStateReader[T] = Callable[[T], AsyncContextManager[CVLAuthorState]]


class CertoraProverTools[U: FeatureUnit](PipelinePlugin[U]):
    """Contributes tools to the prover's CVL author. ``st``/``state_reader`` let a
    contributed tool stage the author's live dependencies (:class:`ProverState`) at
    invocation time: bind the reader into the tool, and open it against the injected
    graph state of type ``st``."""

    @abstractmethod
    async def certora_prover_tools[T](
        self,
        comp: U,
        prop: Sequence[PropertyFormulation],
        tool_context: PluginToolContext[FormalizationTool],
        state_reader: ProverStateReader[T],
        st: type[T],
    ) -> ProvidedTools | None:
        ...
