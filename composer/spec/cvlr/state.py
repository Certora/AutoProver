"""State ``verify_rules`` reads.

The generic authoring state (:mod:`composer.authoring.state`) supplies the
buffer, the skip list, and the validation stamps. A verdict also depends on
the points-to summaries, the prover settings, and edits to the program. Those
enter the stamp through :func:`tuning_history`. Expected-failure markings do
not. They are applied to the last run's verdicts when the stamp is decided.
"""

from typing import Annotated, NotRequired

from langgraph.graph import MessagesState
from pydantic import BaseModel

from composer.authoring.state import AuthoringExtra, merge_expected_failures
from composer.prover.ptypes import StatusCodes
from composer.prover.results import ExternalWarnings, UnreadableAlertReport
from composer.spec.cvlr.conf import TunableConf, conf_history, merge_settings
from composer.spec.cvlr.tuning import SummaryDirective, merge_summaries, summary_history
from composer.spec.source.live_explorer import VersionedHistory
from composer.spec.types import CheckName

#: Stamped when every rule the build listed is accounted for. There is no
#: separate stamp for ``cargo check``. A prover run builds first and reports
#: the compiler's message without submitting when that fails
#: (:class:`composer.spec.cvlr.prover.BuildRejected`).
PROVER_VALIDATION_KEY = "prover"


class LastVerdicts(BaseModel):
    """The last prover run that produced verdicts.

    Markings are judged against it. A change of marking re-decides the stamp
    without another run. ``stamp`` is the digest of the draft, the skips, and
    :func:`tuning_history` at that run. A draft edited since does not match it.
    """

    status: dict[str, StatusCodes]
    #: Rules that stopped on an assertion the prover generated; no marking accounts for one.
    incomplete: list[str]
    #: Rules the build listed that the report gave no verdict for. While any is
    #: missing, nothing stamps.
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
    #: ``adjust_prover_config`` writes only the fields it set, and the reducer
    #: applies them, so several calls in one graph step each land.
    prover_settings: Annotated[TunableConf, merge_settings]
    #: The last prover run's warnings about functions it treated as external.
    #: Absent until a run has reported.
    external_functions: NotRequired[ExternalWarnings | UnreadableAlertReport]
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
