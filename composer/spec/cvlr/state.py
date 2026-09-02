"""State types and the publish gate for the CVLR harness author.

The generic authoring state (:mod:`composer.authoring.state`) supplies the buffer, the skip list,
the validation stamps and the digest gate. What is CVLR's own:

* ``expected_failures`` — rule-name → reason for a rule the author asserts *should* fail. On this
  backend that is usually a finding rather than a defect in the rule, so the prover gate excludes
  them from its all-green check instead of treating the run as unfinished.
* ``prover_link`` — the job link from the stamping run, which is what the report links to and what
  ``fetch_verdicts`` re-reads verdicts from.
* ``property_rules`` — the mapping the publish gate validates.
* ``rule_subjects`` — per rule, what it actually drives: a function of the program, or harness-local
  code standing in for one. Carried into the report for the same reason ``expected_failures`` is.
* ``summaries`` — the points-to summaries the author added (:mod:`composer.spec.cvlr.tuning`). These
  feed the digest through ``version_history``, because a summary changes what the prover checked:
  a stamp earned before one was added is about a different build.

**The ground truth is the buffer, not the run.** ``validate_check_mapping``'s docstring notes that
forge names every test it ran and a backend whose checker does not is passed ``None``; the prover
does name every rule, but only after a submission, which costs minutes and money. So the CVLR
backend supplies :mod:`composer.spec.cvlr.rules`' reading of the draft instead — exact, because both
declaration forms name their rules deterministically — and gets the both-direction check for free,
including in the budget wrap-up window where no run is available at all.
"""

from typing import Annotated, Literal, NotRequired

from langgraph.graph import MessagesState
from pydantic import BaseModel, Field

from graphcore.graph import FlowInput

from composer.authoring.state import (
    AuthoringExtra,
    MappingVocab,
    SkippedProperty,
    check_completion,
    merge_expected_failures,
    validate_check_mapping,
)
from composer.spec.context import CacheKey, CvlrGeneration, CvlrJudge
from composer.spec.cvlr.rules import rule_names
from composer.spec.cvlr.tuning import SummaryDirective, merge_summaries, summary_history
from composer.spec.types import CheckName, PropertyTitle, RuleName

#: Stamped by the prover gate when a run comes back with every rule accounted for. There is
#: deliberately no separate stamp for the fast ``cargo check``: a prover run builds first and reports
#: the compiler's own message without submitting when that fails
#: (:class:`composer.spec.cvlr.prover.BuildRejected`), so requiring both would gate one fact twice.
PROVER_VALIDATION_KEY = "prover"

FEEDBACK = "feedback"

#: WorkflowContext child key for the feedback judge (derives its memory namespace and thread ids).
CVLR_JUDGE_KEY = CacheKey[CvlrGeneration, CvlrJudge]("judge")


class PropertyRuleMapping(BaseModel):
    """Maps one property from the batch to the CVLR rule(s) that demonstrate it."""

    property_title: PropertyTitle = Field(
        description="The unique snake_case title of the property (from the batch listing) that "
        "these rules demonstrate"
    )
    rules: list[RuleName] = Field(
        description="The names of the rules in your draft that demonstrate this property. A "
        "`#[rule]` function's name is the rule name; a `cvlr_rules!` invocation named \"solvency\" "
        "over bases `[base_deposit, base_withdraw]` declares `solvency_deposit` and "
        "`solvency_withdraw`, so name those rather than the invocation."
    )


class DrivesProgramFunction(BaseModel):
    """The rule calls a function the program under verification defines."""

    subject: Literal["program_function"] = "program_function"
    rule: RuleName = Field(description="The rule this describes")
    function: str = Field(
        description="The path of the program function the rule calls, spelled as the harness "
        "writes it — e.g. `crate::vault_program::deposit`."
    )


class DrivesHarnessMirror(BaseModel):
    """The rule drives harness-local code that stands in for a program function.

    A property checked this way is a property of the stand-in. Whether it is also a property of the
    program depends on a transcription no tool checked, which is precisely why the claim has to be
    declared rather than left in a doc comment.
    """

    subject: Literal["harness_mirror"] = "harness_mirror"
    rule: RuleName = Field(description="The rule this describes")
    mirrors: str = Field(
        description="The program function the harness code stands in for, e.g. "
        "`crate::vault_program::deposit`."
    )
    reason: str = Field(
        description="Why the program function itself could not be called — the prover error it "
        "produced, and what you tried before reimplementing it."
    )


#: What a rule actually drives. Declared per rule at publish time and carried into the report: a
#: reader deciding how much a verdict is worth needs to know whether it came from the program's own
#: code, and nothing else in the deliverable answers that.
type RuleSubject = Annotated[
    DrivesProgramFunction | DrivesHarnessMirror, Field(discriminator="subject")
]


def validate_rule_subjects(subjects: list[RuleSubject], draft: str) -> str | None:
    """None if every declared rule has exactly one subject and no subject names an absent rule."""
    declared = set(rule_names(draft))
    seen: set[RuleName] = set()
    errors: list[str] = []
    for subject in subjects:
        if subject.rule not in declared:
            errors.append(
                f"`rule_subjects` names {subject.rule!r}, which your draft does not declare."
            )
        elif subject.rule in seen:
            errors.append(f"Rule {subject.rule!r} appears more than once in `rule_subjects`.")
        seen.add(subject.rule)
    for name in sorted(declared - seen):
        errors.append(
            f"Rule {name!r} has no entry in `rule_subjects`: say which program function it drives, "
            "or declare it as a harness mirror and say why."
        )
    return "\n".join(errors) if errors else None


class CvlrGenerationExtra(AuthoringExtra):
    property_rules: list[PropertyRuleMapping]
    #: One entry per declared rule, naming what it drives. See :data:`RuleSubject`.
    rule_subjects: list[RuleSubject]
    #: Points-to summaries the author added, in the order added, deduplicated by pattern. Reduced
    #: rather than replaced: several ``summarize_for_prover`` calls can land in one graph step.
    summaries: Annotated[list[SummaryDirective], merge_summaries]
    expected_failures: Annotated[dict[CheckName, str], merge_expected_failures]
    #: The job link from the most recent prover run that produced results, whether or not it was
    #: all green — a link to a failing run is still the most useful thing a report can offer.
    prover_link: NotRequired[str | None]
    failed: bool | None
    #: Stamped True by the budget monitor's state transformer when the wrap-up alert fires (the same
    #: update that lifts the validation gates), so the published result is known to be a
    #: budget-curtailed partial rather than a validated delivery.
    budget_curtailed: bool


class CvlrGenerationInput(CvlrGenerationExtra, FlowInput):
    pass


class CvlrGenerationState(CvlrGenerationExtra, MessagesState):
    result: NotRequired[str]


def tuning_history(state: CvlrGenerationExtra) -> tuple[str, ...]:
    """The publish surface's non-harness half, as ``version_history`` tokens.

    The generic digest already takes a history so that "a stamp earned before a source edit goes
    stale with it"; a summary is such an edit, and the sharpest kind — it changes what the prover
    analyzed without changing a character of the draft. Every stamp site passes this, so adding a
    summary after a green run costs the run rather than silently keeping its verdicts.
    """
    return summary_history(tuple(state["summaries"]))


def check_cvlr_completion(state: CvlrGenerationExtra) -> str | None:
    """None if the publish gate is satisfied, otherwise the reason."""
    return check_completion(
        state, tuning_history(state), nothing_written="no harness written yet."
    )


#: How this backend words its publish-time mapping. ``ran_source`` names the *draft* rather than a
#: run, because that is where the ground truth comes from — and a rejection that pointed at a prover
#: run the author had not made would be unactionable.
_CVLR_MAPPING = MappingVocab(
    check_noun="rule",
    field_name="property_rules",
    ran_source="your current draft (the `#[rule]` functions and `cvlr_rules!` invocations in it)",
)


def validate_property_rules(
    property_rules: list[PropertyRuleMapping],
    skipped: list[SkippedProperty],
    titles: list[PropertyTitle],
    draft: str,
) -> str | None:
    """Validate the property→rules mapping against the rules the draft actually declares.

    Both directions: no property may claim a rule the draft does not declare, and no declared rule
    may go untied to a property. The second half is what stops a harness accumulating rules nobody
    asked for, which on this backend also means prover time nobody is paying for on purpose.
    """
    return validate_check_mapping(
        [(m.property_title, m.rules) for m in property_rules],
        skipped,
        titles,
        _CVLR_MAPPING,
        ran=list(rule_names(draft)),
    )
