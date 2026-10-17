"""State the CVLR prover gate reads.

The generic authoring state (:mod:`composer.authoring.state`) supplies the
buffer, the skip list, and the validation stamps. A verdict also depends on
the points-to summaries, the prover settings, edits to the program, and which
rules are marked as expected to fail. Those enter the stamp through
:func:`tuning_history`.
"""

from typing import Annotated, NotRequired

from langgraph.graph import MessagesState
from pydantic import BaseModel

from composer.authoring.state import AuthoringExtra, merge_expected_failures
from composer.spec.cvlr.conf import TunableConf, conf_history
from composer.spec.cvlr.tuning import SummaryDirective, merge_summaries, summary_history
from composer.spec.source.live_explorer import VersionedHistory
from composer.spec.types import CheckName

#: Stamped when every rule is accounted for. There is no separate stamp for
#: ``cargo check``. A prover run builds first and reports the compiler's
#: message without submitting when that fails
#: (:class:`composer.spec.cvlr.prover.BuildRejected`).
PROVER_VALIDATION_KEY = "prover"


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
    #: Points-to summaries, in the order added, deduplicated by pattern. Several
    #: ``summarize_for_prover`` calls can land in one graph step, so the reducer
    #: appends.
    summaries: Annotated[list[SummaryDirective], merge_summaries]
    #: The unit's edits to the program: whole files by project-relative path,
    #: laid over the project. Replaced wholesale. ``version_history`` names them.
    vfs: dict[str, str]
    #: The prover settings this unit submits under. Every unit starts from the
    #: defaults. One unit's change must not reach a sibling.
    #:
    #: Last write wins. ``adjust_prover_config`` writes the whole settings, and
    #: two writes in one graph step raise ``InvalidUpdateError`` on a plain field.
    prover_settings: Annotated[TunableConf, _latest_settings]
    #: Functions the last prover run treated as external: neither analyzed nor
    #: summarized, so their writes were dropped. Absent until a run has reported.
    external_functions: NotRequired[tuple[str, ...]]
    expected_failures: Annotated[dict[CheckName, str], merge_expected_failures]
    #: Absent until a prover run has produced verdicts.
    last_verdicts: NotRequired[LastVerdicts]
    #: The job link from the most recent prover run that produced results,
    #: including a run that did not verify. Absent until then.
    prover_link: NotRequired[str | None]


class CvlrGenerationState(CvlrGenerationExtra, MessagesState):
    pass


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
