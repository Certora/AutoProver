"""State types and the publish gate for the CVLR harness author.

The generic authoring state (:mod:`composer.authoring.state`) supplies the
buffer, the skip list, the validation stamps, and the digest gate. CVLR adds:

* ``expected_failures`` — rule name to why the author says it should fail.
  The prover gate treats these as accounted for.
* ``prover_link`` — the job link from the latest run that produced results.
  The report links to it, and ``fetch_verdicts`` re-reads verdicts from it.
* ``property_rules`` — the mapping the publish gate validates.
* ``rule_subjects`` — per rule, the program function it calls, or the program
  function harness code stands in for. ``reason`` is set only for a stand-in.
  Checked before publish.
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

The mapping gate's ground truth is the rule list stored with the verifying
run, read from that build's ``.cvlr_rules`` section. ``#[rule]`` and
``cvlr_rules!`` are how an author writes a rule. A checkpoint with no stored
list has not had its rules listed, and publish refuses it. Outside the budget
wrap-up the mapping is checked against the list. The wrap-up stores the
mapping as given.
"""

import dataclasses
from collections.abc import Collection
from pathlib import Path
from textwrap import indent
from typing import Annotated, NotRequired

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
from composer.spec.cvlr.conf import TunableConf, conf_history, merge_settings
from composer.spec.cvlr.tuning import SummaryDirective, merge_summaries, summary_history
from composer.spec.source.live_explorer import VersionedHistory
from composer.spec.source.munge.edit_store import AppliedEdit
from composer.spec.types import CheckName, PropertyTitle, RuleName

#: Stamped when every rule is accounted for. There is no separate stamp for
#: ``cargo check``. A prover run builds first and reports the compiler's
#: message without submitting when that fails
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


class RuleSubject(BaseModel):
    """The program function one rule drives, or the one harness code stands in for.

    ``reason`` is set only for a stand-in. Nothing checks that the stand-in
    matches the function it names.
    """

    rule: RuleName = Field(description="The rule this describes")
    function: str = Field(
        description="The path of the program function the rule calls, or the program function "
        "harness code stands in for, spelled as the harness writes it — e.g. "
        "`crate::vault_program::deposit`."
    )
    reason: str | None = Field(
        default=None,
        description="Set only when the rule drives harness code standing in for `function`: why "
        "the program function itself could not be called — the prover error it produced, and what "
        "you tried before reimplementing it. Omit when the rule calls the program function.",
    )


#: A path into the program under verification starts with this. The harness is
#: a module inside that crate (``src/certora/specs/<unit>.rs``), so the
#: program's items are ``crate::…``. Any other root names a dependency.
_PROGRAM_ROOT = "crate::"


def _outside_the_program(path: str) -> bool:
    return not path.startswith(_PROGRAM_ROOT)


def validate_rule_subjects(subjects: list[RuleSubject], declared: Collection[str]) -> str | None:
    """None when every listed rule has exactly one subject naming code in the program.

    A rule may call a dependency, such as ``anchor_lang``'s ``Signer::try_from``,
    verify, and be mapped to a property of the program while establishing
    nothing about the program. The subject names that path, so this check can
    reject it. The mapping gate and the verdict cannot.

    ``declared`` is the rule list the verifying build stored. It is not read
    from the draft.
    """
    listed = set(declared)
    seen: set[RuleName] = set()
    errors: list[str] = []
    for subject in subjects:
        if subject.rule not in listed:
            errors.append(
                f"`rule_subjects` names {subject.rule!r}, which the build did not list."
            )
        elif subject.rule in seen:
            errors.append(f"Rule {subject.rule!r} appears more than once in `rule_subjects`.")
        seen.add(subject.rule)
        named = subject.function
        if _outside_the_program(named):
            errors.append(
                f"Rule {subject.rule!r} names {named!r}, which is not in the program under "
                f"verification: the harness is a module inside that crate, so its own items are "
                f"reachable as `{_PROGRAM_ROOT}…` and anything else is a dependency. A rule that "
                f"drives a dependency proves a property of that dependency, however well it maps to "
                f"the property's words. Drive the program's own code, or skip the property and say "
                f"this is why."
            )
    for name in sorted(listed - seen):
        errors.append(
            f"Rule {name!r} has no entry in `rule_subjects`: say which program function it drives, "
            "or set `reason` when harness code stands in for one."
        )
    return "\n".join(errors) if errors else None


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
    #: The names the build listed, sorted. ``None`` means no build has listed
    #: them, which is also what a checkpoint from before this field loads as.
    #: An empty tuple means the section was present and named no rules.
    rules: tuple[str, ...] | None = None


class CvlrGenerationExtra(AuthoringExtra, VersionedHistory):
    property_rules: list[PropertyRuleMapping]
    #: One entry per declared rule: the program function it calls, or the one a
    #: stand-in replaces. See :class:`RuleSubject`.
    rule_subjects: list[RuleSubject]
    #: Points-to summaries, in the order added, deduplicated by pattern. Several
    #: ``summarize_for_prover`` calls can land in one graph step, so the reducer
    #: appends.
    summaries: Annotated[list[SummaryDirective], merge_summaries]
    #: The unit's edits to the program's source: whole files by project-relative
    #: path, laid over the project. Replaced wholesale. ``commit_edit`` and
    #: ``revert_to_edit`` each write a complete snapshot from the edit store,
    #: which is what ``version_history`` names.
    vfs: dict[str, str]
    #: The prover settings this unit submits under. Every unit starts from the
    #: defaults. One unit's change must not reach a sibling.
    #:
    #: ``adjust_prover_config`` writes only the fields it set, and the reducer
    #: applies them, so several calls in one graph step each land.
    prover_settings: Annotated[TunableConf, merge_settings]
    #: The last prover run's own warnings about functions it treated as external:
    #: neither analyzed nor summarized, so their writes were dropped. Absent until
    #: a run has reported.
    external_functions: NotRequired[tuple[str, ...]]
    expected_failures: Annotated[dict[CheckName, str], merge_expected_failures]
    #: Absent until a prover run has produced verdicts.
    last_verdicts: NotRequired[LastVerdicts]
    #: The job link from the most recent prover run that produced results,
    #: including a run that did not verify. Absent until then.
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
    """What a verdict depends on besides the draft, as ``version_history`` tokens.

    A summary changes what the prover analyzed without changing the draft. An
    edit to the program is named by its id in ``version_history``. The conf
    decides the loop bound, the solver flags, and whether vacuity is checked.
    Every stamp site passes this, so a change to any of the three after a green
    run drops that run's stamp.
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
    #: The last prover run's own warnings about functions it treated as external.
    #: A verdict that depends on one of them depends on writes the model dropped.
    external_functions: tuple[str, ...] = ()
    #: Rule name to the author's reason it should fail. The marking is state, not
    #: source. The judge weighs each one as a claim that the program is defective.
    expected_failures: tuple[tuple[CheckName, str], ...] = ()

    def briefing(self) -> list[str]:
        """What a verdict on this harness depends on, and which rules are marked as meant to fail."""
        return self._conditions() + self._expected_failure_parts()

    def _expected_failure_parts(self) -> list[str]:
        if not self.expected_failures:
            return ["No rule is marked as expected to fail."]
        return [
            "Rules marked as expected to fail:",
            *(f"  {rule}\n{indent(reason, '    ')}" for rule, reason in self.expected_failures),
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
                "No points-to summaries, no edits to the program's source, and `optimistic_loop` off."
            ]
        parts: list[str] = []
        if optimistic is not None:
            parts.append(
                f"`optimistic_loop` is on, loop bound of {self.settings.loop_iter}.\n"
                f"  {optimistic.why}"
            )
        if self.summaries:
            parts.append("Points-to summaries:")
            parts += [
                f"  {d.pattern}\n"
                f"    Returns: {d.returns if d.returns is not None else 'unconstrained'}\n"
                f"    {d.why}"
                for d in self.summaries
            ]
        if self.edits:
            parts.append("Edits to the program:")
            parts += [
                f"  {e.executive_summary}\n    {e.why_sound}" for e in self.edits
            ]
            if self.diff:
                parts.append(self.diff)
        if self.external_functions:
            parts.append("Functions the last prover run treated as external:")
            parts.extend(self.external_functions)
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
#: list the verifying build stored. The shared sentence is "no rule by that
#: name ran in {ran_source}".
_CVLR_MAPPING = MappingVocab(
    check_noun="rule",
    field_name="property_rules",
    ran_source="the `.cvlr_rules` section of the build that verified this draft",
)


def validate_property_rules(
    property_rules: list[PropertyRuleMapping],
    skipped: list[SkippedProperty],
    titles: list[PropertyTitle],
    declared: Collection[str],
) -> str | None:
    """Check the property-to-rule mapping against the rules the build listed.

    No property may claim a rule the build did not list, and no listed rule
    may go untied to a property. ``declared`` is that list. It is not read
    from the draft.
    """
    return validate_check_mapping(
        [(m.property_title, m.rules) for m in property_rules],
        skipped,
        titles,
        _CVLR_MAPPING,
        ran=[RuleName(name) for name in declared],
    )
