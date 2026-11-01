"""The property-inference round loop (``composer/spec/prop_inference.py``).

The agent graph is replaced by a script: ``bind_standard`` hands back a chain that records the
initial prompt each round was built with, and ``run_to_completion`` pops the next scripted round
result. The restatement check runs for real against a structured-output stub, so the loop, the
dedup, the per-round cache and the prompt the next round sees are all the production code. No
live model and no postgres.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from langchain_core.tools import BaseTool
from langgraph.store.memory import InMemoryStore

import composer.spec.prop_inference as pi
from composer.pipeline.keys import FINAL_PROPERTIES_KEY
from composer.spec.context import CacheKey, WorkflowContext
from composer.spec.prop_inference import (
    AGENT_RESULT_KEY, AGENT_ROUND_KEY, BUG_ANALYSIS_KEY, DEFAULT_MAX_BUG_ROUNDS,
    EXISTING_SPECS_NOTICE, PROPERTY_SYSTEM_TEMPLATE, _AgentRoundResult, _AgentRoundWithHistory,
    _Restatement, _RoundRestatements,
)
from composer.spec.types import PropertyFormulation
from composer.templates.loader import load_jinja_template

pytestmark = pytest.mark.asyncio


def _p(title: str, desc: str | None = None) -> PropertyFormulation:
    return PropertyFormulation(sort="safety_property", title=title, description=desc or f"{title} holds")


def _round(*titles: str, reasoning: str = "r") -> _AgentRoundResult:
    return _AgentRoundResult(items=[_p(t) for t in titles], reasoning=reasoning)


class _StructuredStub(BaseChatModel):
    """Structured-output binding that answers each call from a queue and records the prompts."""
    answers: list[_RoundRestatements]
    seen: list[str] = []

    def with_structured_output(self, schema, **kwargs) -> Runnable:  # type: ignore[override]
        def answer(messages: Any) -> _RoundRestatements:
            self.seen.append(str(messages[-1].content))
            return self.answers.pop(0)
        return RunnableLambda(answer)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        raise NotImplementedError("stub is structured-output only")

    @property
    def _llm_type(self) -> str:
        return "structured-stub"


@dataclass
class _Env:
    lite: _StructuredStub
    sort: str = "existing"
    analysis_tools: tuple = ()

    def llm_lite(self) -> _StructuredStub:
        return self.lite

    def builder_heavy(self) -> None:
        return None


@dataclass
class _Unit:
    display_name: str = "Vault: deposits"


class _Chain:
    """Stands in for the graph builder chain; keeps the initial prompt and validator it got."""
    def __init__(self, script: "_Script", validator: Any) -> None:
        self.script, self.validator = script, validator

    def with_initial_prompt(self, prompt: Any) -> "_Chain":
        self.script.prompts.append(prompt)
        self.script.validators.append(self.validator)
        return self

    def __getattr__(self, _name: str) -> Any:
        return lambda *a, **k: self


@dataclass
class _Script:
    rounds: list[_AgentRoundResult]
    prompts: list[Any] = field(default_factory=list)
    validators: list[Any] = field(default_factory=list)
    runs: int = 0


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch):
    def install(*rounds: _AgentRoundResult) -> _Script:
        s = _Script(list(rounds))

        def bind_standard(_builder, _st, _desc, *, validator=None):
            return _Chain(s, validator)

        async def run_to_completion(*_a, **_k):
            if not s.rounds:
                raise AssertionError("the loop ran more rounds than the script has")
            s.runs += 1
            return {"result": s.rounds.pop(0), "messages": [AIMessage("done")]}

        monkeypatch.setattr(pi, "bind_standard", bind_standard)
        monkeypatch.setattr(pi, "run_to_completion", run_to_completion)
        monkeypatch.setattr(pi, "budget_pressure", lambda: False)
        return s
    return install


def _ctx() -> WorkflowContext[Any]:
    return WorkflowContext.create(
        services=lambda _ns: cast(BaseTool, object()),
        thread_id="t",
        store=InMemoryStore(),
        recursion_limit=10,
        cache_namespace=("test",),
    )


def _prior_block(_unit: Any, _sort: Any, prev: list[_AgentRoundResult]) -> str:
    """The shared prior-properties block every ecosystem's initial prompt includes."""
    return load_jinja_template("shared/prior_properties.j2", prior_properties=prev, unit_noun="component")


def _no_restatements(n: int) -> _StructuredStub:
    return _StructuredStub(answers=[_RoundRestatements(restatements=[]) for _ in range(n)])


async def _run(env: _Env, max_rounds: int, ctx: WorkflowContext[Any] | None = None) -> list[str]:
    root = ctx or _ctx()
    got = await pi.run_property_inference(
        root.child(CacheKey("component")), cast(Any, env), cast(Any, _Unit()),
        max_rounds=max_rounds,
        backend_guidance="g", system_template=PROPERTY_SYSTEM_TEMPLATE, render_initial=_prior_block,
    )
    return [p.title for p in got]


# --- when the loop stops --------------------------------------------------------------

async def test_stops_at_the_first_round_that_adds_nothing(script) -> None:
    s = script(_round("a", "b"), _round("c"), _round())
    titles = await _run(_Env(_no_restatements(2)), max_rounds=5)
    assert titles == ["a", "b", "c"]
    assert s.runs == 3


async def test_stops_at_the_round_limit(script) -> None:
    s = script(_round("a"), _round("b"), _round("c"), _round("d"))
    titles = await _run(_Env(_no_restatements(2)), max_rounds=3)
    assert titles == ["a", "b", "c"]
    assert s.runs == 3


async def test_default_limit_is_five(script) -> None:
    assert DEFAULT_MAX_BUG_ROUNDS == 5
    s = script(*[_round(f"p{i}") for i in range(7)])
    await pi.run_property_inference(
        _ctx().child(CacheKey("component")), cast(Any, _Env(_no_restatements(4))), cast(Any, _Unit()),
        backend_guidance="g", system_template=PROPERTY_SYSTEM_TEMPLATE, render_initial=_prior_block,
    )
    assert s.runs == 5


async def test_budget_pressure_stops_before_a_later_round(script, monkeypatch) -> None:
    s = script(_round("a"), _round("b"))
    monkeypatch.setattr(pi, "budget_pressure", lambda: True)
    assert await _run(_Env(_no_restatements(0)), max_rounds=5) == ["a"]
    assert s.runs == 1


# --- what counts as new ---------------------------------------------------------------

async def test_restatements_are_dropped_and_the_rest_kept(script) -> None:
    lite = _StructuredStub(answers=[
        _RoundRestatements(restatements=[_Restatement(title="a_again", restates="a")]),
        _RoundRestatements(restatements=[]),
    ])
    s = script(_round("a"), _round("a_again", "b"), _round())
    assert await _run(_Env(lite), max_rounds=5) == ["a", "b"]
    # The dedup call saw both lists.
    assert "`a`" in lite.seen[0] and "`a_again`" in lite.seen[0]
    # The third round was told about b but never about the dropped restatement.
    assert "`b`" in s.prompts[2][-1] and "a_again" not in s.prompts[2][-1]


async def test_a_round_of_only_restatements_ends_the_loop(script) -> None:
    lite = _StructuredStub(answers=[
        _RoundRestatements(restatements=[
            _Restatement(title="a2", restates="a"), _Restatement(title="b2", restates="b"),
        ]),
    ])
    s = script(_round("a", "b"), _round("a2", "b2"), _round("never"))
    assert await _run(_Env(lite), max_rounds=5) == ["a", "b"]
    assert s.runs == 2


async def test_restatements_naming_unknown_properties_remove_nothing(script) -> None:
    lite = _StructuredStub(answers=[
        _RoundRestatements(restatements=[
            _Restatement(title="b", restates="no_such_prior"),
            _Restatement(title="no_such_new", restates="a"),
        ]),
        _RoundRestatements(restatements=[]),
    ])
    script(_round("a"), _round("b"), _round())
    assert await _run(_Env(lite), max_rounds=5) == ["a", "b"]


async def test_round_zero_makes_no_dedup_call(script) -> None:
    lite = _no_restatements(0)
    script(_round("a"))
    assert await _run(_Env(lite), max_rounds=1) == ["a"]
    assert lite.seen == []


# --- what later rounds are told -------------------------------------------------------

async def test_each_round_sees_every_earlier_property_and_its_own_angle(script) -> None:
    s = script(_round("a", reasoning="looked at deposits"), _round("b"), _round())
    await _run(_Env(_no_restatements(2)), max_rounds=5)

    first, second, third = (p[-1] for p in s.prompts)
    assert "<prior_properties>" not in first
    assert "`a`" in second and "looked at deposits" in second
    assert "`a`" in third and "`b`" in third
    assert "may be wrong" in second
    # A different angle each round.
    assert "Standards and interfaces" in second and "Sibling entry points" not in second
    assert "Sibling entry points" in third and "Standards and interfaces" not in third


async def test_validator_rejects_titles_of_earlier_rounds(script) -> None:
    s = script(_round("a"), _round("b"), _round())
    await _run(_Env(_no_restatements(2)), max_rounds=5)
    assert s.validators[1](None, _round("a")) is not None
    assert s.validators[1](None, _round("fresh")) is None


async def test_evaluate_runs_tell_every_round_the_specs_may_be_wrong(script) -> None:
    s = script(_round("a"), _round())
    await pi.run_property_inference(
        _ctx().child(CacheKey("component")), cast(Any, _Env(_no_restatements(1))), cast(Any, _Unit()),
        max_rounds=5, backend_guidance="g", system_template=PROPERTY_SYSTEM_TEMPLATE,
        render_initial=_prior_block, existing_specs=True,
    )
    assert all(EXISTING_SPECS_NOTICE in p for p in s.prompts)

    s2 = script(_round("a"), _round())
    await _run(_Env(_no_restatements(1)), max_rounds=5)
    assert not any(EXISTING_SPECS_NOTICE in p for p in s2.prompts)


async def test_logs_how_many_new_properties_each_round_found(script, caplog) -> None:
    lite = _StructuredStub(answers=[
        _RoundRestatements(restatements=[_Restatement(title="a2", restates="a")]),
        _RoundRestatements(restatements=[]),
    ])
    script(_round("a", "b"), _round("a2", "c"), _round())
    with caplog.at_level(logging.INFO, logger="composer.spec.prop_inference"):
        await _run(_Env(lite), max_rounds=5)
    lines = [r.getMessage() for r in caplog.records]
    assert any("round 1/5 found 2 new" in m for m in lines)
    assert any("round 2/5 found 1 new" in m and "dropped 1" in m for m in lines)
    assert any("round 3/5 found 0 new" in m for m in lines)


# --- caching --------------------------------------------------------------------------

async def test_a_round_replays_from_its_cache_without_a_model_call(script) -> None:
    ctx = _ctx().child(CacheKey("agent"))
    prev = [_round("a")]
    s = script(_round("b"))
    env = _Env(_no_restatements(1))
    first = await pi._run_bug_round(cast(Any, env), ctx, 1, lambda p: ["x"], prev, "sys")
    assert s.runs == 1

    # Same index, same earlier properties: replayed, no graph run and no dedup call.
    again = await pi._run_bug_round(cast(Any, env), ctx, 1, lambda p: ["x"], prev, "sys")
    assert again.items == first.items and s.runs == 1

    # Same index, different earlier properties: a different prompt, so a fresh round.
    script(_round("c"))
    other = await pi._run_bug_round(
        cast(Any, _Env(_no_restatements(1))), ctx, 1, lambda p: ["x"], [_round("z")], "sys",
    )
    assert [p.title for p in other.items] == ["c"]


async def test_a_cached_round_keeps_its_dedup_outcome() -> None:
    ctx = _ctx().child(CacheKey("agent"))
    prev = [_round("a")]
    cached = _AgentRoundWithHistory(
        items=[_p("b")], reasoning="r", agent_conversation=[], restated=["a2"],
    )
    await ctx.child(AGENT_ROUND_KEY(1, prev)).cache_put(cached)
    got = await pi._run_bug_round(cast(Any, _Env(_no_restatements(0))), ctx, 1, lambda p: [], prev, "sys")
    assert got.restated == ["a2"] and [p.title for p in got.items] == ["b"]


async def test_whole_loop_replays_without_a_model_call(script) -> None:
    root = _ctx()
    script(_round("a"), _round())
    assert await _run(_Env(_no_restatements(1)), max_rounds=5, ctx=root) == ["a"]
    s = script()
    assert await _run(_Env(_no_restatements(0)), max_rounds=5, ctx=root) == ["a"]
    assert s.runs == 0


async def test_round_key_follows_the_properties_fed_in() -> None:
    assert str(AGENT_ROUND_KEY(0)) == "round-0"
    base = str(AGENT_ROUND_KEY(1, [_round("a")]))
    assert base.startswith("round-1-")
    assert str(AGENT_ROUND_KEY(1, [_round("a")])) == base
    assert str(AGENT_ROUND_KEY(1, [_round("b")])) != base
    assert str(AGENT_ROUND_KEY(1, [_round("a", reasoning="other")])) != base
    assert str(AGENT_ROUND_KEY(2, [_round("a")])) != base


async def test_round_limit_and_evaluate_notice_change_the_keys() -> None:
    assert str(BUG_ANALYSIS_KEY(None, False)) == "bug_analysis"
    assert str(BUG_ANALYSIS_KEY(None, False, max_rounds=5)) == "bug_analysis-r5"
    assert str(BUG_ANALYSIS_KEY(None, False, max_rounds=3)) == "bug_analysis-r3"
    assert str(BUG_ANALYSIS_KEY(None, False, max_rounds=5, existing_specs=True)) == "bug_analysis-es-r5"
    assert str(FINAL_PROPERTIES_KEY(None, False, None, "comprehensive", max_rounds=5)) == "final_props-r5"
    assert str(FINAL_PROPERTIES_KEY(None, False, None, "evaluate", max_rounds=5)) == "final_props-r5-mode-evaluate"
    assert str(AGENT_RESULT_KEY) == "agent_bug_analysis"


# --- driver and cache-walker plumbing -------------------------------------------------

class _Plugins:
    plugin_digest = None
    plugin_manifest: list[str] = []

    def runners(self, **_kw):
        return []


@dataclass
class _DriverUnit:
    display_name: str = "deposits"
    unit_index: int = 0

    def cache_material(self) -> str: return self.display_name
    def context_tag(self) -> dict: return {}


@dataclass
class _Ecosystem:
    property_prompts: Any = field(default_factory=lambda: type("P", (), {"system": None, "render_initial": None})())

    def units(self, _main):
        return [_DriverUnit()]


@pytest.mark.parametrize("mode, expected", [
    ("comprehensive", False), ("prioritized", False), ("evaluate", True),
])
async def test_driver_passes_the_round_limit_and_flags_evaluate_runs(monkeypatch, mode, expected) -> None:
    import composer.pipeline.core as core
    from composer.pipeline.run_mode import RunMode

    seen: dict[str, Any] = {}

    async def fake_inference(*_a, **kw):
        seen.update(kw)
        return [_p("a")]

    class _Run:
        ctx = _ctx()
        env = None
        run_mode = RunMode(mode)
        source = type("S", (), {"content": None})()

        async def runner(self, _info, job):
            return await job(None)

    monkeypatch.setattr(core, "run_property_inference", fake_inference)
    got = await core._extract_all(
        "props", "main", "g", cast(Any, _Run()), cast(Any, 2), False, None, (), 4,
        cast(Any, _Ecosystem()), cast(Any, _Plugins()),
    )
    assert [p.title for b in got.batches for p in b.props] == ["a"]
    assert seen["max_rounds"] == 4
    assert seen["existing_specs"] is expected


async def test_driver_records_final_properties_under_the_round_limit(monkeypatch) -> None:
    from tests import test_pipeline_staged_formalizer as sf
    import composer.pipeline.core as core

    keys: list[str] = []

    class _RecordingCtx(sf._FeatCtx):
        def child(self, key, tags=None):
            keys.append(str(key))
            return super().child(key, tags)

    async def fake_extract_all(*_a, **_kw):
        return core.ExtractionResult(
            [core._Batch(sf._Unit("deposits", 0), [sf._prop("a")], _RecordingCtx())],  # type: ignore[arg-type]
            [],
        )

    async def fake_analysis(*_a, **_kw): return "analyzed"
    async def fake_report(*_a, **_kw): return object()

    monkeypatch.setattr(core, "run_component_analysis", fake_analysis)
    monkeypatch.setattr(core, "_extract_all", fake_extract_all)
    monkeypatch.setattr(core, "build_report", fake_report)
    await core.run_pipeline(
        sf._Backend(sf._Prepared(sf._Formalizer())), sf._Run(), max_bug_rounds=4,  # type: ignore[arg-type]
        ecosystem=sf.EVM,
    )
    assert "final_props-r4" in keys


async def test_cache_walker_rebuilds_the_keys_from_the_run_tags() -> None:
    from composer.cli.cache_autoprove import _resolve_bug_key, _resolve_formalization_key
    from composer.pipeline.ptypes import FinalProperties
    from composer.pipeline.keys import FORMALIZATION_KEY
    from composer.pipeline.run_tags import AutoProveCacheTags
    from composer.spec.cvl_generation import GeneratedCVL

    feat = _ctx().child(CacheKey("component"))
    tags = AutoProveCacheTags(
        cache_root=["ns"], contract_name="C", memory_ns=None, interactive=False,
        max_bug_rounds=4, run_mode="evaluate",
    )
    assert str(await _resolve_bug_key(feat, tags)) == "bug_analysis-es-r4"

    await feat.child(FINAL_PROPERTIES_KEY(None, False, None, "evaluate", max_rounds=4)).cache_put(
        FinalProperties(items=[_p("a")], tool_plugins=[])
    )
    # The recorded batch is found, so the edge comes from it and not from the raw output.
    found = await _resolve_formalization_key(feat, tags, [_p("raw")])
    assert str(found) == str(FORMALIZATION_KEY(GeneratedCVL, [_p("a")], []))
    # Tags written before the round limit was recorded rebuild the old key shape.
    old = AutoProveCacheTags(cache_root=["ns"], contract_name="C", memory_ns=None, interactive=False)
    assert old.max_bug_rounds is None
    assert str(await _resolve_bug_key(feat, old)) == "bug_analysis"
