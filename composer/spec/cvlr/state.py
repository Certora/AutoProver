"""State ``verify_rules`` reads.

The generic authoring state (:mod:`composer.authoring.state`) supplies the
buffer, the skip list, the expected-failure markings, and the validation
stamps. A verdict also depends on the points-to summaries, the prover
settings, and edits to the program. Those enter the stamp through
:func:`tuning_history`.
"""

from typing import Annotated, NotRequired

from langgraph.graph import MessagesState
from pydantic import BaseModel

from composer.authoring.state import CheckingExtra
from composer.prover.ptypes import StatusCodes
from composer.prover.results import UnreadableAlertReport
from composer.spec.cvlr.conf import TunableConf, conf_history, merge_settings
from composer.spec.cvlr.externals import ExternalWarnings
from composer.spec.cvlr.tuning import SummaryDirective, merge_summaries, summary_history
from composer.spec.source.live_explorer import VersionedHistory

#: Stamped when every rule the build listed is accounted for. There is no
#: separate stamp for ``cargo check``. A prover run builds first and reports
#: the compiler's message without submitting when that fails
#: (:class:`composer.spec.cvlr.prover.BuildRejected`).
PROVER_VALIDATION_KEY = "prover"


class LastVerdicts(BaseModel):
    """The last prover run that produced verdicts.

    ``verify_rules`` judges it again under the current markings, and stamps
    without submitting when the draft has not changed since and every rule is
    accounted for.
    """

    status: dict[str, StatusCodes]
    #: Rules that stopped on an assertion the prover generated.
    incomplete: list[str]
    #: Rules the build listed that the report gave no verdict for.
    unverdicted: list[str]
    #: :func:`~composer.authoring.state.draft_digest` with :func:`tuning_history`
    #: at that run. The markings are not in it: they change how a run is judged,
    #: not which run it was.
    draft: str


class CvlrGenerationExtra(CheckingExtra, VersionedHistory):
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
    #: ``adjust_prover_config`` writes only the fields it set, and the reducer
    #: applies them, so several calls in one graph step each land.
    prover_settings: Annotated[TunableConf, merge_settings]
    #: The last prover run's warnings about functions it treated as external.
    #: Absent until a run has reported.
    external_functions: NotRequired[ExternalWarnings | UnreadableAlertReport]
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
