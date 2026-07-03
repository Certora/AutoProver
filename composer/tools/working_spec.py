from typing import override

from langgraph.types import Command, interrupt

from pydantic import Field

from graphcore.graph import tool_state_update
from graphcore.tools.schemas import WithAsyncDependencies, WithImplementation, WithInjectedState, WithInjectedId

from composer.core.state import AIComposerState
from composer.human.types import ProposalType
from composer.cvl.tools import maybe_update_cvl
from composer.spec.proposal_store import ProposalStore

from composer.ui.tool_display import tool_display

@tool_display("Read working spec", None)
class ReadWorkingSpec(WithImplementation[str], WithInjectedState[AIComposerState], WithInjectedId):
    """
    Read the contents of your working spec.
    """
    @override
    def run(self) -> str:
        if not self.state["working_spec"]:
            return "No working spec written"
        return self.state["working_spec"]

@tool_display("Write working spec", None)
class WriteWorkingSpec(WithImplementation[Command | str], WithInjectedId):
    """
    Write a new version of your working spec draft from raw CVL text.
    Only one working spec can exist at a time; calling this tool replaces
    any previous draft.

    For applying a `cex_remediation` proposal, prefer
    `apply_remediation_proposal(proposal_key)` — it fetches the full
    proposed text by key so you don't re-emit it. Reach for this tool
    when you need to tweak a remediator proposal (e.g. fix a typecheck
    error in the proposed CVL) or for any other case where you have raw
    CVL text in hand.

    If the new version is not syntactically correct, this tool call will
    be rejected with an error message.
    """
    new_cvl: str = Field(description="The new working spec. Should be a complete, self-contained, syntatically correct CVL file (do NOT submit a 'patch')")

    @override
    def run(self) -> str | Command:
        return maybe_update_cvl(
            tool_call_id=self.tool_call_id,
            pp=self.new_cvl,
            spec_key="working_spec"
        )


@tool_display("Apply remediation proposal", None)
class ApplyRemediationProposal(WithAsyncDependencies[Command | str, ProposalStore], WithInjectedId):
    """
    Stage a `cex_remediation` proposal as your working spec draft. Pass
    the `proposal_key` that the remediator returned in its "Apply via"
    line; this tool looks up the full proposed CVL text and stores it as
    the transient working draft, replacing any previous draft.

    Use this for the common case of "remediator returned a proposal, I
    accept it as-is." For the case where you need to tweak the proposal
    (typecheck fix, small adjustment), reach for `write_working_spec`
    with the modified text.

    If the proposed CVL fails the typecheck, this tool call will be
    rejected with an error message.
    """
    proposal_key: str = Field(
        description=(
            "The opaque proposal_key printed in `cex_remediation`'s output "
            "under 'Apply via'. The full proposed spec text is stored under "
            "this key; this tool fetches and stages it."
        ),
    )

    @override
    async def run(self) -> str | Command:
        with self.tool_deps() as proposal_store:
            full_cvl = await proposal_store.lookup(self.proposal_key)
            if full_cvl is None:
                return (
                    f"No proposal found for proposal_key {self.proposal_key!r}. "
                    f"Re-invoke `cex_remediation` to get a fresh key; old keys "
                    f"do not survive across remediation calls."
                )
            return maybe_update_cvl(
                tool_call_id=self.tool_call_id,
                pp=full_cvl,
                spec_key="working_spec",
            )


@tool_display("Requested spec change", None)
class CommitWorkingSpec(WithImplementation[Command | str], WithInjectedId, WithInjectedState[AIComposerState]):
    """
    Call this tool to ask a human reviewer to approve "committing" your working spec to the "master" copy.

    You should only use this tool after you have run the prover with sufficient rigor to confirm that the changes present
    in the spec are correct and pass formal verification. In addition, the changes present here should be the minimal possible
    changes to ensure the formal verification passes. Do *NOT* rewrite entire portions of the specification or make large scale changes
    unless the user has explicitly approved these changes via the human_in_the_loop tool. Do *NOT* request changes that significantly
    weaken the specification or otherwise trivialize it.

    NB once the working spec has been committed to the VFS, it is discarded.
    """
    explanation: str = \
    Field(description="An explanation to the human reviewer as to why you think the changes in the working spec"
            "this change is necessary and why it is safe or sound to apply it.")

    @override
    def run(self) -> Command | str:
        if not self.state["working_spec"]:
            return "No working spec set."
        work_spec = self.state["working_spec"]
        proposal : ProposalType = {
            "type": "proposal",
            "current_spec": self.state["vfs"]["rules.spec"],
            "proposed_spec": work_spec,
            "explanation": self.explanation
        }

        response = interrupt(proposal)
        assert isinstance(response, str)
        if response.startswith("ACCEPTED"):
            return tool_state_update(
                tool_call_id=self.tool_call_id,
                content="Accepted",
                working_spec=None,
                vfs={"rules.spec": work_spec}
            )
        return response
