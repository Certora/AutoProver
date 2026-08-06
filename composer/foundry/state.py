"""State types + completion gate for the foundry test author.

The generic authoring state (:mod:`composer.authoring.state`) supplies the buffer, the skip list,
the validation stamps and the digest gate. What is foundry's own:

* ``expected_failures: dict[str, str]`` — test-name → reason map for tests
  intentionally expected to fail. Populated by ``expect_test_failure``,
  cleared per-key by ``expect_test_passage``. The ``forge_test`` runner
  excludes these from the all-green check.
* ``last_test_names`` — the test-function names reported by the most
  recent ``forge_test`` run (parsed from forge's JSON output). The runner
  records this unconditionally on every run that produced parseable
  results; the publish gate uses it as the ground truth
  :func:`~composer.authoring.state.validate_unit_mapping` checks the declared property→test
  mapping against, rather than trusting the agent's transcription.
* ``property_tests`` — that mapping.
"""

from typing import Annotated, NotRequired

from langgraph.graph import MessagesState
from pydantic import BaseModel, Field

from graphcore.graph import FlowInput

from composer.authoring.state import (
    AuthoringExtra, MappingVocab, SkippedProperty, check_completion,
    merge_expected_failures, validate_unit_mapping,
)
from composer.spec.context import CacheKey, FoundryGeneration, FoundryJudge


FORGE_TEST_VALIDATION_KEY = "forge_test"

FEEDBACK = "feedback"

# WorkflowContext child key for the feedback judge (derives its memory
# namespace and thread ids).
FOUNDRY_JUDGE_KEY = CacheKey[FoundryGeneration, FoundryJudge]("judge")


class PropertyTestMapping(BaseModel):
    """Maps one property from the batch to the foundry test function(s)
    that demonstrate it."""
    property_title: str = Field(
        description="The unique snake_case title of the property (from the "
        "batch listing) that these tests demonstrate"
    )
    tests: list[str] = Field(
        description="The names of the test functions (``test_*`` / "
        "``testFuzz_*`` / ``invariant_*``) in the test file that demonstrate "
        "this property"
    )


class FoundryGenerationExtra(AuthoringExtra):
    property_tests: list[PropertyTestMapping]
    expected_failures: Annotated[dict[str, str], merge_expected_failures]
    last_test_names: list[str] | None
    failed: bool | None


class FoundryGenerationInput(FoundryGenerationExtra, FlowInput):
    pass


class FoundryGenerationState(FoundryGenerationExtra, MessagesState):
    result: NotRequired[str]


def check_foundry_completion(state: FoundryGenerationExtra) -> str | None:
    """Return None if the publish gate is satisfied, otherwise the reason."""
    return check_completion(state, nothing_written="no test written yet.")


#: How the foundry author words its publish-time mapping. Unlike CVL, forge names every test it
#: ran, so the mapping is checked against that ground truth in both directions.
_FOUNDRY_MAPPING = MappingVocab(
    unit_noun="test",
    field_name="property_tests",
    ran_source="the stamping forge_test invocation",
)


def validate_property_tests(
    property_tests: list[PropertyTestMapping],
    skipped: list[SkippedProperty],
    titles: list[str],
    ran_test_names: list[str],
) -> str | None:
    """Validate the property→tests mapping declared at completion time, against the tests forge
    actually ran."""
    return validate_unit_mapping(
        [(m.property_title, m.tests) for m in property_tests],
        skipped,
        titles,
        _FOUNDRY_MAPPING,
        ran=ran_test_names,
    )
