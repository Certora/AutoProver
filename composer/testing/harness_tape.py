from typing import Any, Callable, Iterator, Sequence, override, cast
import random
import asyncio

from pydantic import Field
from langchain_core.language_models.fake_chat_models import (
    FakeMessagesListChatModel,
)
from composer.llm.provider import ProviderKind
from langchain_core.prompt_values import PromptValue
from langchain_core.tools import BaseTool
from langchain_core.messages import BaseMessage, AIMessage
from langchain_core.runnables import RunnableConfig

from composer.diagnostics.timing import get_current_task_id


def _prompt_preview(model_input: Any) -> str:
    """A short, safe description of the incoming prompt, to make a missing or
    mis-lane'd tape entry easy to locate when authoring."""
    try:
        if isinstance(model_input, PromptValue):
            msgs: list[Any] = list(model_input.to_messages())
        elif isinstance(model_input, (list, tuple)):
            msgs = list(model_input)
        else:
            return repr(model_input)[:160]
        if not msgs:
            return "<empty prompt>"
        last = msgs[-1]
        content = getattr(last, "content", last)
        return f"{type(last).__name__}: {str(content)[:160]}"
    except Exception:
        return "<unpreviewable prompt>"


class HarnessFakeLLM(FakeMessagesListChatModel):
    """``FakeMessagesListChatModel`` tolerant of the specific shape of attribute
    access the codegen workflow performs on the bound LLM, with per-lane tape
    routing.

    Two compatibility shims:

    * ``thinking`` — ``composer.workflow.meta.create_resume_commentary``
      calls ``llm.copy(update={"thinking": None})``. Pydantic v2 tolerates
      unknown keys but prints less predictably; declaring the field makes
      the copy a no-op explicitly.
    * ``betas`` — ``composer.workflow.executor`` does
      ``getattr(llm, "betas")``. An empty list keeps the memory-tool
      beta branch off, so the main codegen agent's tool list matches
      what the tape expects.

    Lane routing: each call is served from the per-lane cursor for the active
    ``run_task`` ``task_id`` (``composer.diagnostics.timing.get_current_task_id``).
    The task_id is read in the async ``ainvoke`` body, where the ContextVar that
    ``run_task`` set is visible (reading it inside the synchronous ``_generate``,
    which the base runs in an executor thread, would not see it). This keeps the
    tape deterministic even though the pipeline runs phases concurrently.
    """

    thinking: Any = None
    betas: list[str] = []
    # The base requires `responses`, but lane routing serves from `lanes` and
    # never reads it; default it so callers construct with `lanes=` alone.
    responses: list[BaseMessage] = Field(default_factory=list)
    # task_id -> ordered scripted responses for that lane.
    lanes: dict[str, list[BaseMessage]] = Field(default_factory=dict)
    # task_id -> next index. Mutated in place; each instance owns its own dict.
    lane_cursors: dict[str, int] = Field(default_factory=dict, exclude=True)

    with_human_delay: bool = Field(default=True)

    @override
    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ):
        return self

    @override
    async def ainvoke(
        self,
        input: PromptValue | str | Sequence[BaseMessage | list[str] | tuple[str, str] | str | dict[str, Any]],
        config: RunnableConfig | None = None,
        *,
        stop: list[str] | None = None,
        **kwargs: Any
    ) -> AIMessage:
        # Simulate LLM latency to keep the TUI from filling all at once to give some ability to judge the "feel" of the UI.
        if self.with_human_delay:
            await asyncio.sleep(random.random() * 1.5 + 1.0)

        task_id = get_current_task_id()
        if task_id is None:
            raise RuntimeError(
                "HarnessFakeLLM: LLM call outside any run_task scope, so it "
                "cannot be routed to a tape lane. "
                f"Prompt -> {_prompt_preview(input)}"
            )
        lane = self.lanes.get(task_id)
        if lane is None:
            raise RuntimeError(
                f"HarnessFakeLLM: no tape lane for task_id {task_id!r}. "
                f"Known lanes: {sorted(self.lanes)}. "
                f"Prompt -> {_prompt_preview(input)}"
            )
        i = self.lane_cursors.get(task_id, 0)
        if i >= len(lane):
            raise RuntimeError(
                f"HarnessFakeLLM: tape lane {task_id!r} exhausted after "
                f"{len(lane)} response(s) — the pipeline issued an extra call in "
                f"this phase. Prompt -> {_prompt_preview(input)}"
            )
        self.lane_cursors[task_id] = i + 1
        return cast(AIMessage, lane[i])


class _DummyUploader:
    """A ``FileUploader`` stand-in that never touches a Files API: every input is
    read from disk and returned as an in-memory text document. Installed under the
    harness so a taped run does no real uploads — the codegen path uploads the spec
    + interface via ``upload_text_file_if_needed``, which would otherwise hit the
    live Files API."""

    async def upload_text_file_if_needed(self, file_path: Any) -> Any:
        return self._inline(file_path)

    async def upload_file_if_needed(self, file_path: Any) -> Any:
        return self._inline(file_path)

    async def get_document(self, path: Any) -> Any:
        import os
        return self._inline(path) if os.path.isfile(str(path)) else None

    @staticmethod
    def _inline(path: Any) -> Any:
        import os
        from pathlib import Path
        from composer.input.files import InMemoryTextFile
        p = str(path)
        return InMemoryTextFile(
            basename=os.path.basename(p),
            string_contents=Path(p).read_text(encoding="utf-8"),
            provider="anthropic"
        )


# The currently-installed tape state for this process. The seam patches below are
# installed once per process as stable dispatcher functions that read these slots
# at call time; re-installing a tape (a later test in the same xdist worker) only
# swaps the slot. Rebinding the module attributes per install instead would strand
# any module that imported a patched name by value under the earlier test's tape.
_active_fake: Any = None
_active_responses: Iterator[str] | None = None
_llm_seams_patched = False
_prompt_seam_patched = False


def _current_fake() -> Any:
    assert _active_fake is not None, "no harness tape installed"
    return _active_fake


def install_fake_llm(fake: Any) -> None:
    """Route every LLM-construction path in the pipeline to ``fake``.

    Pipeline models are minted via
    ``composer.llm.registry.get_provider_for(...).builder_for(...)`` — the tiering
    layer uses the ``tiered=`` overload, the CLIs use ``options=`` — and the
    codegen ``create_llm`` / ``create_llm_base`` seam is the secondary direct path.
    Patching the registry chokepoint plus the services seam covers every path,
    and short-circuits ``get_provider_for`` before it tries to ``_lookup`` a fake
    model name.

    The first install in a process must run BEFORE the entry path imports
    ``get_provider_for`` by name (``composer/bind.py`` is that hook). Eager-import
    callers (the integration tests) additionally rebind their own
    ``get_provider_for`` reference to the patched one. Later installs just swap
    the active fake.
    """
    global _active_fake, _llm_seams_patched
    _active_fake = fake
    if _llm_seams_patched:
        return

    import composer.llm.registry as registry
    import composer.workflow.services as services

    class _FakeProvider:
        provider : ProviderKind = "anthropic"

        def builder_for(self, *, cache_level: Any = None, disable_thinking: bool = False) -> Any:
            return _current_fake()

    fp = _FakeProvider()

    def _fake_get_provider_for(
        *, model_name: Any = None, options: Any = None, tiered: Any = None
    ) -> Any:
        if tiered is not None:
            return registry.TieredProviders(lite=fp, heavy=fp, provider_kind="anthropic")
        return fp

    registry.get_provider_for = _fake_get_provider_for
    registry.uploader_for = lambda _provider: _DummyUploader()
    services.create_llm = lambda args: _current_fake()
    services.create_llm_base = lambda args: _current_fake()
    _llm_seams_patched = True


def install_fake_responses(responses: list[str]) -> None:
    """Replay scripted human replies for console HITL interrupts.

    Patches ``composer.ui.prompt.prompt_input`` (and the binding
    ``composer.ui.console`` imported it under) to return each ``responses`` entry
    in call order, applying the call's own ``filter`` as a sanity check. Raises if
    the tape is exhausted or a scripted reply fails the prompt's filter. Install
    before the entry path imports ``prompt_input`` by name (``composer/bind.py`` is
    that hook). Replayed alongside ``install_fake_llm``.

    Covers only the console HITL path; the autoprove interactive-refinement
    conversation uses a different input path and is not handled here.
    """
    global _active_responses, _prompt_seam_patched
    _active_responses = iter(responses)
    if _prompt_seam_patched:
        return

    import composer.ui.prompt as prompt_mod

    def _fake_prompt_input(
        prompt_str: str,
        debug_thunk: Callable[[], None],
        filter: Callable[[str], str | None] | None = None,
    ) -> str:
        assert _active_responses is not None, "no response tape installed"
        try:
            resp = next(_active_responses)
        except StopIteration:
            raise RuntimeError(
                f"response tape exhausted — no scripted reply for HITL prompt: {prompt_str!r}"
            )
        if filter is not None and (rejection := filter(resp)) is not None:
            raise RuntimeError(
                f"scripted response {resp!r} rejected for prompt {prompt_str!r}: {rejection}"
            )
        return resp

    prompt_mod.prompt_input = _fake_prompt_input
    import composer.ui.console as console_mod
    console_mod.prompt_input = _fake_prompt_input
    _prompt_seam_patched = True
