"""State the CVLR prover gate reads.

The generic authoring state (:mod:`composer.authoring.state`) supplies the
buffer, the skip list, and the validation stamps. CVLR adds the job link from
the latest run that produced results.
"""

from typing import NotRequired

from langgraph.graph import MessagesState

from composer.authoring.state import AuthoringExtra

#: Stamped when every rule the build listed verified. There is no separate
#: stamp for ``cargo check``. A prover run builds first and reports the
#: compiler's message without submitting when that fails
#: (:class:`composer.spec.cvlr.prover.BuildRejected`).
PROVER_VALIDATION_KEY = "prover"


class CvlrGenerationExtra(AuthoringExtra):
    #: The job link from the most recent prover run that produced results,
    #: including a run that did not verify. Absent until then.
    prover_link: NotRequired[str | None]


class CvlrGenerationState(CvlrGenerationExtra, MessagesState):
    pass
