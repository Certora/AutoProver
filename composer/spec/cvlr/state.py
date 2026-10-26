"""State ``verify_rules`` reads.

The generic authoring state (:mod:`composer.authoring.state`) supplies the
buffer, the skip list, and the validation stamps. A verdict also depends on
the points-to summaries, the prover settings, and edits to the program. Those
enter the stamp through :func:`tuning_history`.
"""

from typing import Annotated, NotRequired

from langgraph.graph import MessagesState

from composer.authoring.state import AuthoringExtra
from composer.spec.cvlr.conf import TunableConf, conf_history
from composer.spec.cvlr.tuning import SummaryDirective, merge_summaries, summary_history
from composer.spec.source.live_explorer import VersionedHistory

#: Stamped when every rule the build listed verified. ``cargo_check`` does not stamp.
PROVER_VALIDATION_KEY = "prover"


def _latest_settings(_current: TunableConf, update: TunableConf) -> TunableConf:
    """Last write wins. The settings edit tool writes every field, so there is nothing to merge."""
    return update


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
