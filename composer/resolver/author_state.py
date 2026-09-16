"""The ``CVLAuthorState`` the DZ chain consumes, built from a fetched run.

Two of its members are closures in the pipeline: the prover runner captures
the author's conf, and the edit store folds a proposal into the author's
edit history. Here the runner is :class:`WorkspaceProverRunner` and the
proposals are only recorded: the agent's overlay is what :mod:`.patches`
turns into a diff, and nothing applies it on the resolver's side.
"""
from dataclasses import dataclass, field
from pathlib import Path

from composer.io.task_host import TaskHost
from composer.spec.source.plugin import CVLAuthorState

from .runner import WorkspaceProverRunner
from .workspace import RunWorkspace


@dataclass(frozen=True)
class Proposal:
    vfs: dict[str, str]
    executive_summary: str
    why_sound: str


@dataclass
class RecordingEditProposer:
    """Implements :class:`composer.spec.source.plugin.EditProposer`; keeps every
    proposal in order and hands back an id the agent's report can name."""
    proposals: list[Proposal] = field(default_factory=list)

    async def propose(
        self, vfs: dict[str, str], *, executive_summary: str, why_sound: str
    ) -> str:
        self.proposals.append(Proposal(dict(vfs), executive_summary, why_sound))
        return f"proposal-{len(self.proposals)}"


def author_state_for(
    workspace: RunWorkspace, *, cloud: bool, spec: str | None = None
) -> tuple[CVLAuthorState, RecordingEditProposer]:
    """The state for one resolver run over ``workspace``. ``spec`` overrides the
    run's own spec as the starting buffer (the run's spec by default)."""
    proposer = RecordingEditProposer()
    state = CVLAuthorState(
        working_dir=Path(workspace.run_dir),
        curr_spec=workspace.spec_text() if spec is None else spec,
        prover_runner=WorkspaceProverRunner(workspace, cloud=cloud).run,
        host=TaskHost(),
        edit_store=proposer,
    )
    return state, proposer
