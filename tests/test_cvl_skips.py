"""
Tests for CVL generation skip/completion machinery wired through a ReAct graph.

Uses a minimal test tool to exercise the _merge_skips reducer and
check_completion / _compute_digest validation logic end-to-end.
"""
import pytest

from typing import NotRequired, override, Iterable, Callable, Protocol


from dataclasses import dataclass


from langgraph.types import Command
from langgraph.graph import MessagesState


from composer.spec.cvl_generation import (
    CVLGenerationExtra,
    SkippedProperty,
    _compute_digest,
    check_completion,
    _FeedbackSchema,
    _RecordSkipSchema,
    _UnskipSchema,
    FEEDBACK_VALIDATION_KEY,
    FeedbackToolContext,
    FeedbackToolImpl
)
from composer.spec.feedback import Rebuttal

from graphcore.tools.results import result_tool_generator
from graphcore.tools.schemas import WithAsyncImplementation, WithInjectedId
from graphcore.graph import tool_state_update

from graphcore.testing import Scenario, tool_call_raw, ToolCallDict

pytestmark = pytest.mark.asyncio

_RECORD_SKIP_NAME = "record_skip"
_UNSKIP_NAME = "unskip_property"
_FEEDBACK_NAME = "feedback_tool"
_RESULT_NAME = "result"
_PUT_CVL = "put_cvl"

def _skip(
    property_title: str,
    reason: str,
    alternatives: list[str] | None = None,
    category: str = "other",
) -> ToolCallDict:
    # reason_category and alternatives_considered are required tool fields; the gate only
    # fires for category "storage_access", so the "other" default plus an empty list
    # suffices unless a test exercises the gate.
    return tool_call_raw(
        name=_RECORD_SKIP_NAME,
        property_title=property_title,
        reason=reason,
        reason_category=category,
        alternatives_considered=alternatives if alternatives is not None else [],
    )

def _unskip(property_title: str) -> ToolCallDict:
    return tool_call_raw(_UNSKIP_NAME, property_title=property_title)

def _feedback() -> ToolCallDict:
    return tool_call_raw(_FEEDBACK_NAME)

def _result(commentary: str) -> ToolCallDict:
    return tool_call_raw(_RESULT_NAME, value=commentary)

def _cvl(the_cvl: str) -> ToolCallDict:
    return tool_call_raw(_PUT_CVL, cvl=the_cvl)

# ---------------------------------------------------------------------------
# Test state and test tools
# ---------------------------------------------------------------------------


class CVLTestState(MessagesState, CVLGenerationExtra):
    result: NotRequired[str]

result_tool = result_tool_generator(
    "result",
    (str, "The commentary"),
    "When you're done with the generation",
    validator=(CVLTestState, lambda st, _res, _id: check_completion(st))
)

class DummyPutCVL(WithAsyncImplementation[Command], WithInjectedId):
    """
    put some cvl
    """
    cvl: str

    @override
    async def run(self) -> Command:
        return tool_state_update(
            tool_call_id=self.tool_call_id, content="Success",
            curr_spec=self.cvl
        )

TOOLS = [
    _RecordSkipSchema.as_tool(_RECORD_SKIP_NAME),
    _UnskipSchema.as_tool(_UNSKIP_NAME),
    _FeedbackSchema.as_tool(_FEEDBACK_NAME),
    result_tool,
    DummyPutCVL.as_tool(_PUT_CVL)
]

@dataclass
class Feedback:
    good: bool
    feedback: str

async def dummy_feedback(
    spec: str,
    s: list[SkippedProperty],
    rebuttals: list[Rebuttal],
    within_tool: str
) -> Feedback:
    return Feedback(good=True, feedback="")

def any_reason(
    title: str
):
    return (title, None)

def with_reason(
    title: str, reason: str
):
    return (title, reason)

def feedback_builder(
    *reason_specs: tuple[str, str | None],
    accepted_cvl: str | Iterable[str] | Callable[[str], bool] | None = None,
) -> FeedbackToolImpl:
    skippable = {
        k: v for (k,v) in reason_specs
    }

    async def impl(
        spec: str,
        skipped: list[SkippedProperty],
        rebuttals: list[Rebuttal],
        within_tool: str,
    ) -> Feedback:
        for sk in skipped:
            if sk.property_title not in skippable:
                return Feedback(good=False, feedback="I don't like your skips")
            reason = skippable[sk.property_title]
            if reason is None:
                continue
            if reason != sk.reason:
                return Feedback(good=False, feedback="I don't like your skip reason")
        if accepted_cvl is None:
            return Feedback(good=True, feedback="")
        elif callable(accepted_cvl):
            return Feedback(good=accepted_cvl(spec), feedback="")
        else:
            return Feedback(good=spec in accepted_cvl, feedback="")

    return impl

def scenario(
    num_props: int,
    feedback_impl: FeedbackToolImpl = dummy_feedback,
    *,
    curr_spec: str | None = None,
    skips: list[SkippedProperty] | None = None,
    required: list[str] | None = None
):
    return Scenario(CVLTestState, *TOOLS).init(
        curr_spec=curr_spec,
        validations={},
        skips=skips if skips else [],
        property_rules=[],
        required_validations=required if required else [FEEDBACK_VALIDATION_KEY]
    ).with_context(FeedbackToolContext(
        feedback_impl, titles=[f"p{i}" for i in range(num_props)]
    ))


# =========================================================================
# _merge_skips reducer via graph
# =========================================================================


class TestMergeSkipsViaGraph:
    async def test_adds_skip(self):
        result = await scenario(1).turn(
            _skip(property_title="p0", reason="too complex")
        ).run()
        assert len(result["skipped"]) == 1
        assert result["skipped"][0].property_title == "p0"
        assert result["skipped"][0].reason == "too complex"

    async def test_merges_two_skips(self):
        result = await scenario(4).turn(
            _skip(property_title="p0", reason="complex"),
            _skip(property_title="p2", reason="out of scope")
        ).run()
        assert len(result["skipped"]) == 2
        titles = sorted([s.property_title for s in result["skipped"]])
        assert titles == ["p0", "p2"]  # sorted by title

    async def test_overwrites_reason(self):
        result = await scenario(1).turn(
            _skip("p0", "old reason")
        ).turn(
            _skip("p0", "new reason")
        ).run()
        assert len(result["skipped"]) == 1
        assert result["skipped"][0].reason == "new reason"

    async def test_unskip(self):
        result = await scenario(1).turn(
            _skip("p0", "no real reason")
        ).turn(
            _unskip("p0")
        ).run()
        assert len(result["skipped"]) == 0

    async def test_skip_is_merge(self):
        result = await scenario(2).turn(
            _skip("p0", "because")
        ).turn(
            _skip("p1", "it's hard")
        ).run()
        assert len(result["skipped"]) == 2
        assert result["skipped"][0].property_title == "p0"
        assert result["skipped"][1].property_title == "p1"

    def skip_error_mapper(self, st: CVLTestState) -> tuple[str, list[SkippedProperty]]:
        return (
            Scenario.last_single_tool(_RECORD_SKIP_NAME, st),
            st["skipped"]
        )

    async def test_invalid_skip_reason(self):
        (msg, st) = await scenario(1).turn(
            _skip("p0", "too hard")
        ).turn(
            _skip("p0", "")
        ).map(self.skip_error_mapper).run()
        assert len(st) == 1 and st[0].property_title == "p0"
        assert "A non-empty justification is" in msg


# =========================================================================
# Skip gate: storage_access skips require non-empty alternatives_considered
# =========================================================================

class TestSkipGate:
    def gate_mapper(self, st: CVLTestState) -> tuple[str, list[SkippedProperty]]:
        return (
            Scenario.last_single_tool(_RECORD_SKIP_NAME, st),
            st["skipped"],
        )

    async def test_storage_access_without_alternatives_rejected(self):
        (msg, skipped) = await scenario(1).turn(
            _skip(
                "p0",
                "the balance lives in a keccak-derived storage slot with no way to read it",
                category="storage_access",
            )
        ).map(self.gate_mapper).run()
        assert skipped == []
        assert msg.startswith("Skip REJECTED:")
        assert "keccak storage-slot constant" in msg
        assert "Sload/Sstore storage hooks" in msg
        assert "harness getter/helper" in msg
        assert "ghost state mirroring" in msg

    async def test_storage_access_with_blank_alternatives_rejected(self):
        # Whitespace-only entries must not satisfy the non-empty requirement.
        (msg, skipped) = await scenario(1).turn(
            _skip(
                "p0",
                "the accumulator is private with no getter",
                alternatives=["   ", ""],
                category="storage_access",
            )
        ).map(self.gate_mapper).run()
        assert skipped == []
        assert msg.startswith("Skip REJECTED:")

    async def test_storage_access_with_alternatives_accepted(self):
        (msg, skipped) = await scenario(1).turn(
            _skip(
                "p0",
                "the balance lives in a keccak-derived storage slot",
                alternatives=[
                    "precomputed the keccak slot constant, but the slot value depends on a runtime salt",
                    "an Sstore hook on the slot cannot fire because writes go through delegatecall",
                ],
                category="storage_access",
            )
        ).map(self.gate_mapper).run()
        assert msg.startswith("Recorded skip")
        assert len(skipped) == 1 and skipped[0].property_title == "p0"

    async def test_hash_collision_passes_with_empty_alternatives(self):
        # Collision/inversion/preimage reasoning is the one keccak-related skip that stays
        # legitimate; it is not access-shaped, so no alternatives are structurally required
        # (the judge still audits the classification's honesty).
        (msg, skipped) = await scenario(1).turn(
            _skip(
                "p0",
                "requires reasoning about keccak collisions to forge a valid signature",
                category="hash_collision",
            )
        ).map(self.gate_mapper).run()
        assert msg.startswith("Recorded skip")
        assert len(skipped) == 1

    @pytest.mark.parametrize("category", ["prover_limitation", "environment", "other"])
    async def test_non_access_categories_pass_with_empty_alternatives(self, category: str):
        (msg, skipped) = await scenario(1).turn(
            _skip("p0", "requires quantifying over arbitrary-length arrays", category=category)
        ).map(self.gate_mapper).run()
        assert msg.startswith("Recorded skip")
        assert len(skipped) == 1

    async def test_category_is_recorded_on_the_skip(self):
        # The judge audits reason_category, so the recorded SkippedProperty must carry it
        # (not just gate on it and drop it).
        (msg, skipped) = await scenario(1).turn(
            _skip(
                "p0",
                "the balance lives in a keccak-derived storage slot",
                alternatives=["the slot constant depends on a runtime salt"],
                category="storage_access",
            )
        ).map(self.gate_mapper).run()
        assert msg.startswith("Recorded skip")
        assert skipped[0].reason_category == "storage_access"

    async def test_alternatives_are_recorded_on_the_skip(self):
        # The judge audits alternatives_considered, so the recorded SkippedProperty must
        # carry them (not just gate on them and drop them).
        alts = [
            "precomputed the keccak slot constant, but the slot depends on a runtime salt",
            "an Sstore hook cannot fire because writes go through delegatecall",
        ]
        (msg, skipped) = await scenario(1).turn(
            _skip(
                "p0",
                "the balance lives in a keccak-derived storage slot",
                alternatives=alts,
                category="storage_access",
            )
        ).map(self.gate_mapper).run()
        assert msg.startswith("Recorded skip")
        assert skipped[0].alternatives_considered == alts

    async def test_skipped_property_backcompat_without_new_fields(self):
        # Cached SkippedProperty instances predate alternatives_considered and
        # reason_category; deserialization must default them rather than fail validation.
        s = SkippedProperty.model_validate({"property_title": "p0", "reason": "too hard"})
        assert s.alternatives_considered == []
        assert s.reason_category == "other"


# =========================================================================
# Digest coverage: alternatives_considered is part of the skip's audited identity
# =========================================================================

class TestDigestCoversAlternatives:
    def test_digest_changes_with_alternatives(self):
        bare = SkippedProperty(property_title="p0", reason="r")
        with_alts = SkippedProperty(
            property_title="p0", reason="r", alternatives_considered=["tried ghost mirroring"]
        )
        assert _compute_digest("spec", [bare]) != _compute_digest("spec", [with_alts])

    def test_digest_changes_with_alternative_content(self):
        a = SkippedProperty(
            property_title="p0", reason="r", alternatives_considered=["tried ghost mirroring"]
        )
        b = SkippedProperty(
            property_title="p0", reason="r", alternatives_considered=["tried Sstore hooks"]
        )
        assert _compute_digest("spec", [a]) != _compute_digest("spec", [b])

    def test_empty_alternatives_preserve_legacy_digest(self):
        # Cached pre-field skips deserialize with the default empty list; their digest
        # must equal an explicitly-empty one so cached validation stamps stay fresh.
        legacy = SkippedProperty.model_validate({"property_title": "p0", "reason": "r"})
        explicit = SkippedProperty(property_title="p0", reason="r", alternatives_considered=[])
        assert _compute_digest("spec", [legacy]) == _compute_digest("spec", [explicit])


# =========================================================================
# Digest coverage: reason_category is part of the skip's audited identity
# =========================================================================

class TestDigestCoversCategory:
    def test_digest_changes_with_category(self):
        a = SkippedProperty(property_title="p0", reason="r", reason_category="storage_access",
                            alternatives_considered=["tried ghost mirroring"])
        b = SkippedProperty(property_title="p0", reason="r", reason_category="hash_collision",
                            alternatives_considered=["tried ghost mirroring"])
        assert _compute_digest("spec", [a]) != _compute_digest("spec", [b])

    def test_non_default_category_changes_digest(self):
        default = SkippedProperty(property_title="p0", reason="r")
        classified = SkippedProperty(property_title="p0", reason="r", reason_category="environment")
        assert _compute_digest("spec", [default]) != _compute_digest("spec", [classified])

    def test_default_category_preserves_legacy_digest(self):
        # Cached pre-field skips deserialize with the default "other"; their digest must
        # equal an explicitly-"other" one so cached validation stamps stay fresh.
        legacy = SkippedProperty.model_validate({"property_title": "p0", "reason": "r"})
        explicit = SkippedProperty(property_title="p0", reason="r", reason_category="other")
        assert _compute_digest("spec", [legacy]) == _compute_digest("spec", [explicit])


# =========================================================================
# Validation stamping + check_completion via graph
# =========================================================================

class RejectionTest(Protocol):
    def __call__(self, st: CVLTestState, cb: Callable[[str], bool] | None = None) -> bool:
        ...

class StagedRejection(RejectionTest, Protocol):
    def check_reason(
        self, reason: str | Callable[[str], bool]
    ) -> Callable[[CVLTestState], bool]:
        ...

def _curry_fn(
    f: RejectionTest
) -> StagedRejection:
    class Wrapped:
        def __call__(self, st: CVLTestState, cb: Callable[[str], bool] | None = None) -> bool:
            return f(st, cb)
        
        def check_reason(self, reason: str | Callable[[str], bool]) -> Callable[[CVLTestState], bool]:
            if isinstance(reason, str):
                return (lambda st: f(st, lambda msg: reason in msg))
            else:
                return (lambda st: f(st, reason))
    return Wrapped()

@_curry_fn
def is_result_rejection(
    st: CVLTestState, cb: Callable[[str], bool] | None = None
) -> bool:
    return "result" not in st and (msg := Scenario.last_single_tool(
        _RESULT_NAME, st
    )).startswith("Completion REJECTED:") and (cb is None or cb(msg))

def result(
    st: CVLTestState
) -> str:
    assert "result" in st
    return st["result"]

unsat_feedback_message = f"{FEEDBACK_VALIDATION_KEY} validation not satisfied or stale"

class TestValidationLogic:
    async def test_basic_completion(
        self
    ):
        assert await scenario(1, curr_spec="anything").turn(
            _feedback()
        ).turn(
            _result("We did it")
        ).map_run(result) == "We did it"

    async def test_no_spec_no_result(self):
        assert await scenario(1).turn(
            _result("nope")
        ).map_run(is_result_rejection.check_reason("no spec written yet"))

    async def test_no_feedback_no_result(self):
        assert await scenario(1, curr_spec="anything").turn(
            _result("nope")
        ).map_run(is_result_rejection.check_reason(unsat_feedback_message))

    async def test_changed_reason_no_result(self):
        assert await scenario(1, curr_spec="whatever").turns(
            _skip("p0", "reason 1"),
            _feedback(),
            _skip("p0", "new reason"),
            _result("I'm done")
        ).map_run(is_result_rejection.check_reason(unsat_feedback_message))

    async def test_changed_alternatives_no_result(self):
        # Editing the judge-audited alternatives_considered after validation must stale
        # the stamp just like editing the reason does.
        reason = "the balance lives in a keccak-derived storage slot"
        assert await scenario(1, curr_spec="whatever").turns(
            _skip("p0", reason, alternatives=[
                "the keccak slot constant depends on a runtime salt",
                "an Sstore hook cannot fire through delegatecall",
            ], category="storage_access"),
            _feedback(),
            _skip("p0", reason, alternatives=[
                "the slot constant is not computable at compile time here",
                "storage hooks do not fire for delegatecall writes",
            ], category="storage_access"),
            _result("I'm done")
        ).map_run(is_result_rejection.check_reason(unsat_feedback_message))

    async def test_changed_category_no_result(self):
        # Re-classifying a skip after validation must stale the stamp just like
        # editing the reason does — the category is judge-audited.
        reason = "requires reasoning about the hash function itself"
        assert await scenario(1, curr_spec="whatever").turns(
            _skip("p0", reason, category="hash_collision"),
            _feedback(),
            _skip("p0", reason, category="prover_limitation"),
            _result("I'm done")
        ).map_run(is_result_rejection.check_reason(unsat_feedback_message))

    async def test_other_validation_key_unsat(self):
        assert await scenario(1, curr_spec="whatever", required=[FEEDBACK_VALIDATION_KEY, "something_else"]).turn(
            _feedback()
        ).turn(
            _result("I'm done")
        ).map_run(is_result_rejection.check_reason("something_else validation not satisfied or stale"))

    async def test_no_validation_rollbacks(self):
        assert await scenario(1, curr_spec="whatever").turns(
            _skip("p0", "reason 1"),
            _feedback(), # stamp reason 1
            _skip("p0", "reason 2"),
            _feedback(), # stamp reason 2,
            _skip("p0", "reason 1"), # back to reason 1 digest
            _result("I'm done") # should fail, current accepted version is reason 2 digest
        ).map_run(is_result_rejection.check_reason(unsat_feedback_message))

    async def test_different_spec_no_result(self):
        assert await scenario(1, curr_spec="version 1").turns(
            _feedback(),
            _cvl("version 2"),
            _result("I'm done")
        ).map_run(is_result_rejection.check_reason(unsat_feedback_message))

    async def test_unskip_rollsback(self):
        assert await scenario(2, feedback_builder(
            any_reason("p0")
        ), curr_spec="the spec").turns(
            _skip("p0", "doesn't matter"),
            _feedback(), # should be accepted
            _skip("p1", "don't like this"),
            _feedback(), # rejection
            _unskip("p1"),
            _result("I'm done")
        ).map_run(result) == "I'm done"

    async def test_cvl_rollback(self):
        assert await scenario(1, feedback_impl=feedback_builder(accepted_cvl="version 1"), curr_spec="version 1").turns(
            _feedback(),
            _cvl("version 2"),
            _feedback(),
            _cvl("version 1"),
            _result("I'm done")
        ).map_run(result) == "I'm done"

class TestFeedbackIntegration:
    async def test_picky_skips(self):
        assert await scenario(1, feedback_impl=feedback_builder(
            with_reason("p0", "good reason")
        ), curr_spec="version 1").turns(
            _skip("p0", "good reason"),
            _feedback(),
            _skip("p0", "bad reason"),
            _result("complete")
        ).map_run(is_result_rejection.check_reason(unsat_feedback_message))
