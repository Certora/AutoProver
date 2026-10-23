"""State the CVLR prover gate reads, extending :class:`~composer.authoring.state.AuthoringExtra`."""

from typing import NotRequired

from langgraph.graph import MessagesState

from composer.authoring.state import AuthoringExtra

#: Stamped when every rule the build listed verified. ``cargo_check`` does not stamp.
PROVER_VALIDATION_KEY = "prover"


class CvlrGenerationExtra(AuthoringExtra):
    #: The job link from the most recent prover run that produced results,
    #: including a run that did not verify. Absent until then.
    prover_link: NotRequired[str | None]


class CvlrGenerationState(CvlrGenerationExtra, MessagesState):
    pass
