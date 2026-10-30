"""State types + completion gate for the foundry test author.

The generic authoring state (:mod:`composer.authoring.state`) supplies the buffer, the skip list,
the expected-failure markings, the validation stamps and the digest gate. ``expect_test_failure``
and ``expect_test_passage`` write the markings, and the ``forge_test`` runner excludes the marked
tests from the all-green check. What is foundry's own:

* ``last_test_names`` — the test-function names reported by the most
  recent ``forge_test`` run (parsed from forge's JSON output). The runner
  records this unconditionally on every run that produced parseable
  results; the publish gate uses it as the ground truth
  :func:`~composer.authoring.state.validate_check_mapping` checks the declared property→test
  mapping against, rather than trusting the agent's transcription.
* ``property_tests`` — that mapping.
"""

from typing import NotRequired

from langgraph.graph import MessagesState
from pydantic import BaseModel, Field

from graphcore.graph import FlowInput

from composer.authoring.state import (
    CheckingExtra, MappingVocab, SkippedProperty, check_completion, make_validation_stamper,
    make_verdict_stamper, validate_check_mapping,
)
from composer.spec.context import CacheKey, FoundryGeneration, FoundryJudge
from composer.spec.types import CheckName, PropertyTitle


FORGE_TEST_VALIDATION_KEY = "forge_test"

FEEDBACK = "feedback"

FORGE_TEST_STAMPER = make_verdict_stamper(FORGE_TEST_VALIDATION_KEY)
FEEDBACK_STAMPER = make_validation_stamper(FEEDBACK)

# WorkflowContext child key for the feedback judge (derives its memory
# namespace and thread ids).
FOUNDRY_JUDGE_KEY = CacheKey[FoundryGeneration, FoundryJudge]("judge")


class PropertyTestMapping(BaseModel):
    """Maps one property from the batch to the foundry test function(s)
    that demonstrate it."""
    property_title: PropertyTitle = Field(
        description="The unique snake_case title of the property (from the "
        "batch listing) that these tests demonstrate"
    )
    tests: list[CheckName] = Field(
        description="The names of the test functions (``test_*`` / "
        "``testFuzz_*`` / ``invariant_*``) in the test file that demonstrate "
        "this property"
    )


class FoundryGenerationExtra(CheckingExtra):
    property_tests: list[PropertyTestMapping]
    last_test_names: list[CheckName] | None
    failed: bool | None
    #: Stamped True by the budget monitor's state transformer when the wrap-up alert fires (the
    #: same update that lifts the validation gates), so the published result is known to be a
    #: budget-curtailed partial rather than a validated delivery.
    budget_curtailed: bool


class FoundryGenerationInput(FoundryGenerationExtra, FlowInput):
    pass


class FoundryGenerationState(FoundryGenerationExtra, MessagesState):
    result: NotRequired[str]


def check_foundry_completion(state: FoundryGenerationExtra) -> str | None:
    """Return None if the publish gate is satisfied, otherwise the reason."""
    return check_completion(
        state, (FORGE_TEST_STAMPER, FEEDBACK_STAMPER), nothing_written="no test written yet."
    )


#: How the foundry author words its publish-time mapping. Unlike CVL, forge names every test it
#: ran, so the mapping is checked against that ground truth in both directions.
_FOUNDRY_MAPPING = MappingVocab(
    check_noun="test",
    field_name="property_tests",
    ran_source="the stamping forge_test invocation",
)


def validate_property_tests(
    property_tests: list[PropertyTestMapping],
    skipped: list[SkippedProperty],
    titles: list[PropertyTitle],
    ran_test_names: list[CheckName],
) -> str | None:
    """Validate the property→tests mapping declared at completion time, against the tests forge
    actually ran."""
    return validate_check_mapping(
        [(m.property_title, m.tests) for m in property_tests],
        skipped,
        titles,
        _FOUNDRY_MAPPING,
        ran=ran_test_names,
    )
