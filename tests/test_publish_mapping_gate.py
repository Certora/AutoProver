"""``PublishResultTool``'s mapping gate, driven through the tool itself.

The report keeps only rules that some property's ``property_rules`` names and counts the rest as
orphans (``report/collect.py``). So an invariant the author proved to support one of its own rules
is invisible in the report unless the author maps it. The gate closes that: the tool reads what the
typechecker declared out of the run that satisfied the publish stamp, and checks the mapping
against it in both directions.

The state is assembled by hand rather than by running the author graph — these tests are about the
gate's decision, not about how the state got there.
"""

import pytest

from composer.authoring.state import spec_digest
from composer.spec.source.author import PublishResultTool
from composer.spec.cvl_generation import PropertyRuleMapping
from composer.spec.types import PropertyTitle
from composer.spec.source.report.schema import RuleName

pytestmark = pytest.mark.asyncio

SPEC = "rule a() { assert true; }\ninvariant b() true;\n"
TITLES = [PropertyTitle("p1")]


def _state(declared: list[str] | None, *, spec: str = SPEC) -> dict:
    """An authoring state whose publish gate is already satisfied. ``declared`` None means no
    prover run covered this spec — the shape a budget wrap-up publishes in, since it lifts the
    prover requirement."""
    history = []
    if declared is not None:
        history.append({
            "sort": "run",
            "tool_call_id": "t0",
            "prover_results": [],
            "spec_digest": "irrelevant-here",
            "rules": None,
            "declared_rules": declared,
            "state_digest": spec_digest(spec, [], []),
        })
    return {
        "curr_spec": spec,
        "skipped": [],
        "validations": {},
        "required_validations": [],   # the stamp check is not what these tests exercise
        "version_history": [],
        "prover_history": history,
        # The rest of SourceCVLGenerationState, which the injected-state validator requires.
        "messages": [],
        "config": {},
        "rule_skips": {},
        "property_rules": [],
        "reminders_channel": [],
        "budget_curtailed": False,
        "failed": None,
    }


async def _publish(declared: list[str] | None, *mapped: str) -> str:
    inst = PublishResultTool(
        commentary="done",
        property_rules=[PropertyRuleMapping(
            property_title=PropertyTitle("p1"), rules=[RuleName(m) for m in mapped],
        )],
        state=_state(declared),
        tool_call_id="t",
    )
    tok = PublishResultTool._dep_ctx.set(TITLES)
    try:
        out = await inst.run()
    finally:
        PublishResultTool._dep_ctx.reset(tok)
    # A rejection is a plain string; an accepted publish writes state.
    return out if isinstance(out, str) else "ACCEPTED"


async def test_a_mapped_supporting_invariant_publishes():
    assert await _publish(["a", "b"], "a", "b") == "ACCEPTED"


async def test_an_unmapped_proved_invariant_is_refused():
    """The case the gate exists for: `b` verified but no property names it, so the report would
    drop it as an orphan."""
    out = await _publish(["a", "b"], "a")
    assert out != "ACCEPTED" and "b" in out


async def test_a_claim_on_a_rule_that_was_never_declared_is_refused():
    out = await _publish(["a"], "a", "imaginary")
    assert out != "ACCEPTED" and "imaginary" in out


async def test_no_prover_run_leaves_the_names_uncross_checked():
    """Nothing to check against, so the gate falls back to coverage only rather than refusing a
    wrap-up publish it has no ground truth for."""
    assert await _publish(None, "anything_goes") == "ACCEPTED"


async def test_an_empty_declaration_is_not_the_same_as_no_declaration():
    """A run that declared nothing is ground truth, not absence of it. Treating the empty set as
    "unknown" would accept a mapping naming rules that do not exist."""
    out = await _publish([], "ghost_rule")
    assert out != "ACCEPTED" and "ghost_rule" in out
