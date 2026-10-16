"""State types and the publish gate for the CVLR harness author.

The generic authoring state (:mod:`composer.authoring.state`) supplies the
buffer, the skip list, the validation stamps, and the digest gate. CVLR adds:

* ``expected_failures`` — rule name to why the author says it should fail.
  The prover gate treats these as accounted for.
* ``prover_link`` — the job link from the latest run that produced results.
  The report links to it, and ``fetch_verdicts`` re-reads verdicts from it.
* ``property_rules`` — the mapping the publish gate validates.
* ``rule_subjects`` — per rule, a program function or harness code standing
  in for one. Carried into the report.
* ``summaries`` — points-to summaries (:mod:`composer.spec.cvlr.tuning`).
  They enter the digest through ``version_history``. A summary changes what
  the prover checked.
* ``vfs`` and ``version_history`` — edits to the program's source, as whole
  files over the project, and the edit-store ids that produced them. The
  history uses the same digest channel. The edits reach the report through
  ``source_edits``.

Summaries, edits, and an ``optimistic_loop`` in ``prover_settings`` are also
what the feedback judge sees beyond the draft (:class:`HarnessAssumptions`).
None of them appears in the harness source.

The mapping gate's ground truth is the draft, read by
:mod:`composer.spec.cvlr.rules`. The prover names every rule only after a
submission. Both declaration forms name their rules deterministically, so the
check runs in both directions before any run, including when the budget
wrap-up leaves no time for one.
"""

import dataclasses
from pathlib import Path
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
from composer.spec.cvlr.conf import TunableConf, conf_history
from composer.spec.cvlr.rules import rule_names
from composer.spec.cvlr.tuning import SummaryDirective, merge_summaries, summary_history
from composer.spec.source.live_explorer import VersionedHistory
from composer.spec.source.munge.edit_store import AppliedEdit
from composer.spec.types import CheckName, PropertyTitle, RuleName

#: Stamped by the prover gate when every rule is accounted for. There is no
#: separate stamp for ``cargo check``. A prover run builds first and reports
#: the compiler's message without submitting when that fails
#: (:class:`composer.spec.cvlr.prover.BuildRejected`).
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

    A property checked this way is a property of the stand-in. It is a property
    of the program only if the transcription is faithful, and no tool checks
    that transcription.
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


#: What a rule drives. One entry per rule at publish time, carried into the
#: report so a reader can tell a verdict on the program from a verdict on a
#: stand-in.
type RuleSubject = Annotated[
    DrivesProgramFunction | DrivesHarnessMirror, Field(discriminator="subject")
]


#: A path into the program under verification starts with this. The harness is
#: a module inside that crate (``src/certora/specs/<unit>.rs``), so the
#: program's items are ``crate::…``. Any other root names a dependency.
_PROGRAM_ROOT = "crate::"


def _outside_the_program(path: str) -> bool:
    return not path.startswith(_PROGRAM_ROOT)


def validate_rule_subjects(subjects: list[RuleSubject], draft: str) -> str | None:
    """None when every declared rule has exactly one subject naming code in the program.

    A rule may call a dependency, such as ``anchor_lang``'s ``Signer::try_from``,
    verify, and be mapped to a property of the program while establishing
    nothing about the program. The subject names that path, so this check can
    reject it. The mapping gate and the verdict cannot.
    """
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
        named = (
            subject.function if subject.subject == "program_function" else subject.mirrors
        )
        if _outside_the_program(named):
            errors.append(
                f"Rule {subject.rule!r} names {named!r}, which is not in the program under "
                f"verification: the harness is a module inside that crate, so its own items are "
                f"reachable as `{_PROGRAM_ROOT}…` and anything else is a dependency. A rule that "
                f"drives a dependency proves a property of that dependency, however well it maps to "
                f"the property's words. Drive the program's own code, or skip the property and say "
                f"this is why."
            )
    for name in sorted(declared - seen):
        errors.append(
            f"Rule {name!r} has no entry in `rule_subjects`: say which program function it drives, "
            "or declare it as a harness mirror and say why."
        )
    return "\n".join(errors) if errors else None


def _latest_settings(_current: TunableConf, update: TunableConf) -> TunableConf:
    """Last write wins. The settings edit tool writes every field, so there is nothing to merge."""
    return update


class LastVerdicts(BaseModel):
    """The last prover run that produced verdicts.

    Markings are judged against it. A change of marking re-decides the stamp
    without another run. ``stamp`` is the digest of the draft at that run, so a
    draft edited since gets nothing from it.
    """

    status: dict[str, bool]
    #: Rules that stopped on an assertion the prover generated; no marking accounts for one.
    incomplete: list[str]
    #: Declared rules the report gave no verdict for; while any is missing, nothing stamps.
    unverdicted: list[str]
    stamp: dict[str, str]


class CvlrGenerationExtra(AuthoringExtra, VersionedHistory):
    property_rules: list[PropertyRuleMapping]
    #: One entry per declared rule, naming what it drives. See :data:`RuleSubject`.
    rule_subjects: list[RuleSubject]
    #: Points-to summaries the author added, in the order added, deduplicated by
    #: pattern. Several ``summarize_for_prover`` calls can land in one graph step,
    #: so the reducer appends.
    summaries: Annotated[list[SummaryDirective], merge_summaries]
    #: The unit's edits to the program's source: whole files by project-relative
    #: path, laid over the project. Replaced wholesale. ``commit_edit`` and
    #: ``revert_to_edit`` each write a complete snapshot from the edit store,
    #: which is what ``version_history`` names.
    vfs: dict[str, str]
    #: The prover settings this unit submits under. Every unit starts from the
    #: defaults. Per unit: one unit's change must not reach a sibling.
    #:
    #: Last write wins. ``adjust_prover_config`` writes the whole settings, and
    #: two writes in one graph step raise ``InvalidUpdateError`` on a plain field.
    prover_settings: Annotated[TunableConf, _latest_settings]
    #: The functions the last prover run treated as external: neither analyzed nor summarized, so
    #: their writes were dropped. Absent until a run has reported.
    external_functions: NotRequired[tuple[str, ...]]
    expected_failures: Annotated[dict[CheckName, str], merge_expected_failures]
    #: Absent until a prover run has produced verdicts.
    last_verdicts: NotRequired[LastVerdicts]
    #: The job link from the most recent prover run that produced results,
    #: including a run that did not verify.
    prover_link: NotRequired[str | None]
    failed: bool | None
    #: Set when the budget monitor's wrap-up alert fires, in the same update that
    #: lifts the validation gates. The published result is a partial.
    budget_curtailed: bool


class CvlrGenerationInput(CvlrGenerationExtra, FlowInput):
    pass


class CvlrGenerationState(CvlrGenerationExtra, MessagesState):
    result: NotRequired[str]


def tuning_history(state: CvlrGenerationExtra) -> tuple[str, ...]:
    """The publish surface outside the harness, as ``version_history`` tokens.

    A summary changes what the prover analyzed without changing the draft. An
    applied edit changes the program; its edit-store id is already a
    ``version_history`` token. The conf decides the loop bound, the solver
    flags, and whether vacuity is checked. Every stamp site passes this, so a
    change to any of the three after a green run drops that run's stamp.
    """
    return (
        summary_history(tuple(state["summaries"]))
        + tuple(state["version_history"])
        + conf_history(state["prover_settings"])
    )


@dataclasses.dataclass(frozen=True)
class HarnessAssumptions:
    """What a reviewer has to see besides the harness source.

    Summaries, source edits, and ``optimistic_loop`` each change what a green
    rule means, and none of them appears in the harness. The empty case is
    included. The judge's prompt tells it to weigh the summaries, and with no
    briefing it looks at the scaffold's tuning files and every sibling's
    directives.

    Expected-failure markings are included too. They are state, not source.
    """

    summaries: tuple[SummaryDirective, ...]
    #: The editor's account of each edit applied to the program, in the order applied.
    edits: tuple[AppliedEdit, ...] = ()
    #: A unified diff from the project's source to the unit's edited copy. Empty
    #: when there are no edits. The judge reads this along with the editor's
    #: summary.
    diff: str = ""
    #: The unit's prover settings. ``optimistic_loop`` assumes every loop finishes
    #: within ``loop_iter``, so the bound is part of the assumption.
    settings: TunableConf = TunableConf()
    #: Functions the last prover run treated as external. A verdict that depends
    #: on one of them depends on writes the model dropped.
    external_functions: tuple[str, ...] = ()
    #: Rule name to the author's reason it should fail. The marking is state, not
    #: source. The judge weighs each one as a claim that the program is defective.
    expected_failures: tuple[tuple[CheckName, str], ...] = ()

    def briefing(self) -> list[str]:
        """What a verdict on this harness depends on, and which rules are marked as meant to fail."""
        return self._conditions() + self._expected_failure_parts()

    def _expected_failure_parts(self) -> list[str]:
        if not self.expected_failures:
            return [
                "The author has marked no rule as expected to fail: every rule in this harness "
                "is claimed to verify."
            ]
        return [
            "The author has recorded these rules as expected to fail, each a claim that the "
            "program is defective. The marking is held outside the harness source, so you will "
            "not find it there:",
            *(f"  {rule}\n    Author's reason: {reason}" for rule, reason in self.expected_failures),
        ]

    def _conditions(self) -> list[str]:
        optimistic = self.settings.optimistic_loop
        if (
            not self.summaries
            and not self.edits
            and optimistic is None
            and not self.external_functions
        ):
            return [
                "The author has added no points-to summaries, has not edited the program's "
                "source, and has left `optimistic_loop` off. The verdicts this "
                "harness earns are conditional on nothing beyond the harness itself, so there is "
                "nothing to weigh on that axis."
            ]
        parts: list[str] = []
        if optimistic is not None:
            parts.append(
                "The author has turned on `optimistic_loop`, with a loop bound of "
                f"{self.settings.loop_iter}. Every rule in this harness was verified assuming each "
                "loop finishes within that bound, so a violation reachable only on a later "
                "iteration is never reported and the rule comes back verified anyway. This is set "
                "in the conf, not in the harness source you are reading.\n"
                f"    Author's justification: {optimistic.why}"
            )
        if self.summaries:
            parts.append(
                "The author has told the prover to stop analyzing these symbols. A summarized "
                "call returns an arbitrary value and writes nothing beyond the locations its "
                "summary names: every other store the real function makes is dropped. A rule "
                "asserting over state one of these functions writes is green having checked "
                "nothing, because that state keeps its old value. None of this appears in the "
                "harness source you are reading:"
            )
            parts += [
                f"  {d.pattern}\n"
                f"    Returns: {d.returns if d.returns is not None else 'unconstrained'}\n"
                f"    Author's justification: {d.why}"
                for d in self.summaries
            ]
        if self.edits:
            parts.append(
                "The author has also edited the program's own source. What the prover analyzed is "
                "the modified program, not the one in the repository, and these edits are "
                "likewise absent from the harness:"
            )
            parts += [
                f"  {e.executive_summary}\n    Justification: {e.why_sound}" for e in self.edits
            ]
            if self.diff:
                parts.append(
                    "This is what those edits do to the program. Read it rather than the summary "
                    "above: the summary is the editor's account of its own work."
                )
                parts.append(self.diff)
        if self.external_functions:
            parts.append(
                "The last prover run treated these functions as external, neither analyzed nor "
                "summarized. Each returns an arbitrary value and writes nothing else, so state it "
                "would change keeps its old value, and one that returns through memory may never "
                "succeed, which makes the code after it unreachable. A rule whose property depends "
                "on one of these can be green having checked nothing:"
            )
            parts += [f"  {name}" for name in self.external_functions]
        return parts


def harness_assumptions(
    state: CvlrGenerationExtra,
    edits: tuple[AppliedEdit, ...] = (),
    diff: str = "",
) -> HarnessAssumptions:
    """The judge-facing view of what :func:`tuning_history` feeds the digest.

    ``edits`` and ``diff`` are passed in. ``state`` holds edit ids and file
    contents. The accounts live in the edit store, and the diff needs the
    project.
    """
    return HarnessAssumptions(
        summaries=tuple(state["summaries"]),
        edits=edits,
        diff=diff,
        settings=state["prover_settings"],
        external_functions=state.get("external_functions", ()),
        expected_failures=tuple(state["expected_failures"].items()),
    )


def check_cvlr_completion(state: CvlrGenerationExtra) -> str | None:
    return check_completion(
        state, tuning_history(state), nothing_written="no harness written yet."
    )


#: Wording for this backend's publish-time mapping. ``ran_source`` names the
#: draft, which is where the ground truth is read. A rejection that pointed at
#: a prover run the author had not made would not say what to fix.
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
    """Check the property-to-rule mapping against the rules the draft declares.

    No property may claim a rule the draft does not declare, and no declared
    rule may go untied to a property.
    """
    return validate_check_mapping(
        [(m.property_title, m.rules) for m in property_rules],
        skipped,
        titles,
        _CVLR_MAPPING,
        ran=list(rule_names(draft)),
    )
