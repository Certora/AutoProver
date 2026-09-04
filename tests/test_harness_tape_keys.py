"""Prefix-keyed tape routing: a lane served by the prompt's AI-message prefix
instead of a process-local cursor, so a resumed process finds its place.

The lane below is what a recording produces when a phase agent (conversation A)
and a sub-agent it spawned (conversation B) interleave: their turns land in one
lane in call order. Positional replay only works if calls repeat in exactly that
order from the start of the process; prefix routing works from any point.
"""

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from composer.diagnostics.timing import set_current_task_id
from composer.testing.harness_tape import (
    HarnessFakeLLM,
    TapeKey,
    dump_keys,
    load_keys,
    prefix_key,
)

pytestmark = pytest.mark.asyncio

LANE = "phase-0"


def ai(text: str, *calls: tuple[str, dict]) -> AIMessage:
    return AIMessage(
        content=text,
        tool_calls=[{"name": name, "args": args, "id": f"call_{i}"} for i, (name, args) in enumerate(calls)],
    )


A0 = ai("a0", ("code_explorer", {"question": "how does increment work?"}))
B0 = ai("b0", ("read_file", {"path": "src/Counter.sol"}))
A1 = ai("a1", ("put_cvl", {"spec": "rule r {}"}))
B1 = ai("b1")
TAPE = [A0, B0, A1, B1]


def conv_a(*turns: AIMessage) -> list:
    return [SystemMessage("phase agent"), HumanMessage("write the spec"), *turns]


def conv_b(*turns: AIMessage) -> list:
    return [SystemMessage("explorer"), HumanMessage("how does increment work?"), *turns]


def fresh(keys: dict[str, list[TapeKey]] | None = None) -> HarnessFakeLLM:
    return HarnessFakeLLM(lanes={LANE: list(TAPE)}, with_human_delay=False, keys=keys or {})


async def call(fake: HarnessFakeLLM, prompt: list) -> AIMessage:
    with set_current_task_id(LANE):
        return await fake.ainvoke(prompt)


async def learn(lane: list[AIMessage], *prompts: list) -> dict[str, list[TapeKey]]:
    """Keys from one positional replay of ``prompts`` against ``lane``."""
    learner = HarnessFakeLLM(lanes={LANE: lane}, with_human_delay=False)
    learner.learning = True
    for prompt in prompts:
        await call(learner, prompt)
    return learner.learned


async def test_learned_keys_let_a_fresh_process_serve_by_prefix_in_any_order() -> None:
    keys = await learn(TAPE, conv_a(), conv_b(), conv_a(A0), conv_b(B0))
    assert [k.index for k in keys[LANE]] == [0, 1, 2, 3]
    assert keys[LANE][2].prefix == prefix_key([A0])

    # A "resumed" process: new fake, same tape, the sidecar's keys, and the
    # continuation calls arriving in a different order than they were recorded.
    resumed = fresh(keys=keys)
    assert await call(resumed, conv_b(B0)) is B1
    assert await call(resumed, conv_a(A0)) is A1
    assert await call(resumed, conv_a()) is A0  # empty prefix: the opening decides
    assert await call(resumed, conv_b()) is B0


async def test_an_in_flight_call_reissued_after_a_crash_gets_the_same_answer() -> None:
    keyed = fresh(keys=await learn(TAPE, conv_a(), conv_b(), conv_a(A0), conv_b(B0)))
    assert await call(keyed, conv_a(A0)) is A1
    assert await call(keyed, conv_a(A0)) is A1  # same prompt, same answer, no cursor moved


async def test_same_first_question_under_different_system_prompts_is_disambiguated() -> None:
    lane = [ai("explorer says"), ai("feedback says")]
    explorer = [SystemMessage("you explore code"), HumanMessage("look at increment")]
    feedback = [SystemMessage("you critique specs"), HumanMessage("look at increment")]
    keyed = HarnessFakeLLM(lanes={LANE: lane}, with_human_delay=False, keys=await learn(lane, explorer, feedback))
    assert await call(keyed, feedback) is lane[1]
    assert await call(keyed, explorer) is lane[0]


async def test_identical_openings_fall_back_to_recorded_order() -> None:
    lane = [ai("x0"), ai("y0")]
    same = [SystemMessage("cex analyst"), HumanMessage("analyze this counter-example")]
    keyed = HarnessFakeLLM(lanes={LANE: lane}, with_human_delay=False, keys=await learn(lane, same, same))
    assert await call(keyed, same) is lane[0]
    assert await call(keyed, same) is lane[1]
    with pytest.raises(RuntimeError, match="exhausted"):
        await call(keyed, same)


async def test_a_diverged_conversation_is_a_clear_error() -> None:
    keyed = fresh(keys={LANE: [TapeKey(index=0, prefix=(), opening=None)]})
    with pytest.raises(RuntimeError, match="AI prefix of length 1"):
        await call(keyed, conv_a(A0))


async def test_a_lane_without_keys_stays_positional() -> None:
    fake = fresh()
    assert await call(fake, conv_b()) is A0  # cursor order, not content


def test_keys_round_trip_through_the_sidecar(tmp_path: Path) -> None:
    keys = {LANE: [TapeKey(index=0, prefix=(), opening="h"), TapeKey(index=1, prefix=("abc",), opening=None)]}
    dump_keys(tmp_path / "k.json", keys)
    assert load_keys(tmp_path / "k.json") == keys
