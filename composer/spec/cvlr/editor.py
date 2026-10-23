"""The code editor: the one agent allowed to change the program under verification.

``docs/who-edits-the-program.md`` §1-7. The author writes its own
unit's harness and asks for changes to the program in words; this agent makes them, earns a review,
and proves they compiled. The author never edits the program's source, which is the point — a tool
that rewrites somebody's repository should have one place that does it and one gate in front of it.

**The editor edits text, as EVM's does** (:mod:`composer.spec.source.munge.munge_agent`), and most of
the machinery is that module's: whole files in a VFS overlay over the project, graphcore's
``edit_file``/``put_file``, a reviewer that reads the edited overlay, an approval tied to a digest
of it, and an :class:`~composer.spec.source.munge.edit_store.EditStore` the author applies the result
from with ``commit_edit``. What is CVLR's own is three things:

* **The gate.** Every edit stays inert outside the requesting unit, so that the user can apply the
  patch to their own tree and get the build they shipped with the unit's cargo feature off. The
  editor writes that ``cfg`` itself, told the feature and the gated shape each kind of edit takes
  (``cvlr_munge_editor_system.j2``), and the reviewer checks it. Nothing checks it mechanically.
* **The compile check** (:func:`gate_edits`): the overlay is staged into the run's working tree under
  the build permit and compiled with the unit's features, and an edited source file rustc never
  read is refused.
* **The write boundary** (:func:`editable_paths`): the project's own crates' sources and manifests,
  never a dependency, and never the harness directories, which are the author's.
"""

import dataclasses
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from pathlib import Path, PurePath, PurePosixPath
from typing import NotRequired, override

from langchain_core.tools import BaseTool
from langgraph.graph import MessagesState
from langgraph.types import Command
from langgraph.store.base import BaseStore
from pydantic import Field

from graphcore.graph import Builder
from graphcore.tools.schemas import WithAsyncDependencies, WithInjectedId, WithInjectedState
from graphcore.tools.vfs import GlobalExcludeArg, VFSAccessor, VFSInput, VFSState

from composer.cargo.depinfo import compiled_sources
from composer.cargo.session import CompileFailed, Compiled
from composer.kb.kb_context import with_cvlr_facts
from composer.pipeline.ecosystem import Ecosystem
from composer.spec.agent_index import AgentIndex
from composer.spec.context import CacheKey, CvlrGeneration, EditorAgent, SourceCode, WorkflowContext
from composer.spec.cvlr.scaffold import ENVS_DIR, SPECS_DIR
from composer.spec.cvlr.state import CvlrGenerationState
from composer.spec.cvlr.tree import Editable, ProjectSource, check_edit
from composer.spec.cvlr.tuning import SummaryDirective
from composer.spec.cvlr.verify import HarnessTarget
from composer.spec.graph_builder import run_to_completion
from composer.spec.service_host import ServiceHost, Sort
from composer.spec.source.live_explorer import LiveEditTools, setup_live_edits
from composer.spec.source.munge.edit_oracle import mk_oracle
from composer.spec.source.munge.edit_store import EditStore, MungeEditor
from composer.spec.source.munge.edit_tools import edit_history_tools
from composer.spec.source.munge.munge_agent import (
    CommonMungeDescription,
    FeedbackBuilder,
    GiveUpTool,
    MungeRefusal,
    RequestReviewTool,
    ReviewDeps,
    accept_edits,
    committed_edit_message,
    munge_feedback_judge,
    unapproved,
)
from composer.spec.source.munge.tool_names import CODE_EDITOR, GIVE_UP, REQUEST_REVIEW, SUBMIT_EDIT
from composer.spec.source.munge.vfs_diff import edited_paths, summarize_changes
from composer.spec.util import uniq_thread_id
from composer.ui.tool_display import tool_display

_log = logging.getLogger(__name__)

EDITOR_KEY = CacheKey[CvlrGeneration, EditorAgent]("cvlr-editor")


# ---------------------------------------------------------------------------------------------
# the editing kit


def editable_paths(
    project: Path, source: ProjectSource, package_dir: Path
) -> Callable[[PurePath], bool]:
    """The editor's write filter, in graphcore's exclusion polarity: True for a path it may not
    write.

    It may write the project's own crates' sources and manifests (:func:`check_edit`), except the
    verified package's ``specs`` and ``envs`` directories: the first holds every unit's harness
    module, which is the author's and which the tree overlays from state, and the second holds the
    tuning files the prover's conf names, which ``summarize_for_prover`` writes.
    """
    reserved = tuple(
        PurePosixPath((package_dir / d).as_posix()) for d in (SPECS_DIR, ENVS_DIR)
    )

    def excluded(path: PurePath) -> bool:
        relative = PurePosixPath(path.as_posix())
        return (
            any(relative.is_relative_to(r) for r in reserved)
            or not isinstance(check_edit(project, source, str(relative)), Editable)
        )

    return excluded


@dataclasses.dataclass(frozen=True)
class EditingServices:
    """What the run's services contribute to program editing, before preflight has said what an
    edit may touch.

    Carried from the entry point to :func:`program_editing`, which finishes the kit once the
    project's own crates are known.
    """

    #: Where edits are committed: run-scoped, keyed by the overlay's digest.
    store: EditStore
    #: The frozen code explorer's cache, which the live explorer reads through so that findings
    #: recorded before any edit stay visible.
    explorer_index: AgentIndex
    indexed_store: BaseStore
    source_key: str
    #: The agents' read filter: the ecosystem's, plus the run's own work directory.
    forbidden_read: GlobalExcludeArg
    recursion_limit: int


@dataclasses.dataclass(frozen=True)
class ProgramEditing:
    """The run's program-editing kit: the VFS-aware tools every agent of a unit reads the program
    through, and the store edits are committed to."""

    live: LiveEditTools
    store: EditStore


def program_editing(
    services: EditingServices,
    env: ServiceHost,
    source: SourceCode,
    project_source: ProjectSource,
    package_dir: Path,
    ecosystem: Ecosystem,
) -> ProgramEditing:
    """Finish the editing kit: tools reading the project through a unit's overlay, and writing only
    where :func:`editable_paths` allows."""
    project = Path(source.project_root)
    surface = dataclasses.replace(source, forbidden_read=services.forbidden_read)
    live = setup_live_edits(
        builder=env.builder_lite(),
        sc=surface,
        base_store=services.explorer_index,
        store=services.indexed_store,
        source_key=services.source_key,
        oracle=mk_oracle(services.store, surface),
        recursion_limit=services.recursion_limit,
        ecosystem=ecosystem,
        forbidden_write=editable_paths(project, project_source, package_dir),
    )
    return ProgramEditing(live=live, store=services.store)


@dataclasses.dataclass(frozen=True)
class EditingJudgeHost:
    """A property judge's construction surface: the reference tools, plus the source tools that read
    the unit's edited program rather than the project as shipped. The judge's state must carry the
    unit's ``vfs`` and ``version_history``."""

    env: ServiceHost
    editing: ProgramEditing

    def builder_heavy(self) -> Builder[None, None, None]:
        return self.env.builder_heavy()

    @property
    def sort(self) -> Sort:
        return self.env.sort

    @property
    def judge_tools(self) -> tuple[BaseTool, ...]:
        live = self.editing.live
        return self.env.rag_tools + live.read_tools + (live.explorer, live.doc_tool)



# ---------------------------------------------------------------------------------------------
# the compile gate


@dataclasses.dataclass(frozen=True)
class Accepted:
    """The overlay builds, and every edited source file of the package reached the compiler."""


@dataclasses.dataclass(frozen=True)
class DoesNotCompile:
    diagnostics: str


@dataclasses.dataclass(frozen=True)
class NotCompiled:
    """The build succeeded and these edited files were never read by rustc.

    The Solana ``EditsNotCompiled``, and the failure it catches is the quietest one this backend
    has: an edit in a file no enabled feature reaches changes nothing, reports nothing, and leaves
    the report carrying a source-edit record for a change that had no effect.
    """

    paths: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class NotChecked:
    """The build succeeded but its dep-info could not be found, so nothing was verified."""

    reason: str


type GateOutcome = Accepted | DoesNotCompile | NotCompiled | NotChecked


async def gate_edits(
    target: HarnessTarget,
    *,
    draft: str,
    summaries: Sequence[SummaryDirective],
    overlay: dict[str, str],
    edited: Iterable[str],
) -> GateOutcome:
    """Stage ``overlay`` into the run's working tree, compile it, and check ``edited`` arrived.

    The author's side of the build rides in rather than being captured when the editor was built:
    the draft moves under the editor's feet, and compiling an overlay against a stale one would
    check something nobody is going to submit.

    Only the verified package's ``.rs`` files are checked against dep-info, because only that
    package's dep-info is read. A manifest is never in dep-info, and an edit inside another local
    crate is checked by the build alone.

    Nothing needs undoing afterwards. The tree is derived from state, so whichever unit stages next
    replaces this overlay, and an edit the author never commits is simply never staged again.
    """
    async with target.build_slot() as permit:
        await permit.stage(draft, summaries, overlay)
        run = await target.session.check(
            manifest_dir=target.package_dir, features=target.features
        )
        if isinstance(run.verdict, CompileFailed):
            return DoesNotCompile(run.verdict.diagnostics)
        assert isinstance(run.verdict, Compiled)
        reached = compiled_sources(
            target.session.workdir,
            target.package,
            marker=target.module_path,
            # `<pkg>/src/certora/specs/<unit>.rs` — four levels up is the package root, which is
            # the other directory cargo may have spelled a dep-info path against.
            package_root=target.module_path.parents[3],
        )
    if reached is None:
        return NotChecked(
            "the build left no dep-info naming this unit's harness module, so whether these edits "
            "reached the compiler could not be established"
        )
    root = target.session.workdir
    package = target.package_root.resolve()
    missed = tuple(
        sorted(
            path
            for path in edited
            if path.endswith(".rs")
            and (root / path).resolve().is_relative_to(package)
            and (root / path).resolve() not in reached
        )
    )
    return NotCompiled(missed) if missed else Accepted()


# ---------------------------------------------------------------------------------------------
# the editor's state


class EditorStateExtra(VFSState):
    #: The author's problem statement, carried through to the reviewer so it can judge whether the
    #: edits stayed on script rather than merely being defensible in the abstract.
    request: str
    #: The author's overlay when the request was made: what the review diffs against.
    orig_vfs: dict[str, str]
    #: Digest of the overlay the reviewer approved. ``submit_edit`` fires only when it still
    #: matches, so any edit after approval voids it.
    reviewed_digest: str | None
    memory: str | None
    #: The author's side of the build, as it stood when the request was made. Carried rather than
    #: captured at construction because the gate compiles the overlay *with* the author's harness,
    #: and a stale copy would gate something nobody is going to submit.
    draft: str
    summaries: list[SummaryDirective]


class EditorInput(EditorStateExtra, VFSInput):
    pass


class EditorState(EditorStateExtra, MessagesState):
    result: NotRequired[CommonMungeDescription | MungeRefusal]


# ---------------------------------------------------------------------------------------------
# submission


@dataclasses.dataclass(frozen=True)
class SubmitDeps:
    target: HarnessTarget
    #: Resolves the overlay against the project, to tell which of its files the edit changed.
    mat: VFSAccessor[VFSState]


@tool_display("Submitting edits", "Submit")
class SubmitEdits(
    WithInjectedState[EditorStateExtra],
    WithInjectedId,
    WithAsyncDependencies[Command | str, SubmitDeps],
):
    """Submit your finished edits.

    Accepted only when an approving `request_review` still stands for these exact edits **and** the
    program compiles with them, with every edited source file actually read by the build. On
    failure you get the reason and keep working; this does not end your turn.
    """

    summary: CommonMungeDescription = Field(description="Your account of the completed changes.")

    @override
    async def run(self) -> Command | str:
        if (lapsed := unapproved(self.state)) is not None:
            return lapsed
        with self.tool_deps() as deps:
            target = deps.target
            refused = [
                check.describe()
                for path in self.state["vfs"]
                if not isinstance(check := target.tree.check_edit(path), Editable)
            ]
            if refused:
                return (
                    "These paths are outside what an edit to the program may touch:\n"
                    + "\n".join(f"  {r}" for r in refused)
                )
            edited = edited_paths(self.state, deps.mat, self.state["orig_vfs"])
            if not edited:
                return "You have changed nothing. Make the edit, or `give_up` and say why."
            outcome = await gate_edits(
                target,
                draft=self.state["draft"],
                summaries=self.state["summaries"],
                overlay=self.state["vfs"],
                edited=edited,
            )
        match outcome:
            case DoesNotCompile(diagnostics=diagnostics):
                return f"Your edits do not compile; fix them before submitting.\n\n{diagnostics}"
            case NotCompiled(paths=paths):
                return (
                    "The build succeeded, but rustc never read "
                    f"{', '.join(paths)} — so your edits to those files changed nothing about "
                    "what is verified. The file is behind a `cfg` this build does not enable, or "
                    "nothing declares its module. Edit a file the build actually compiles, or give "
                    "up and say so."
                )
            case NotChecked(reason=reason):
                _log.warning("cvlr: submitting unverified edits — %s", reason)
            case Accepted():
                pass
        return accept_edits(self.tool_call_id, self.summary)


# ---------------------------------------------------------------------------------------------
# the author's side


@dataclasses.dataclass(frozen=True)
class EditorDeps:
    """What the author-facing tool needs to run one editor conversation and keep its result."""

    target: HarnessTarget
    runner: Callable[[EditorInput, str], Awaitable[EditorState]]
    store: EditStore
    mat: VFSAccessor[VFSState]


@tool_display("Asking the code editor", "Editor")
class CodeEditor(
    WithInjectedState[CvlrGenerationState],
    WithInjectedId,
    WithAsyncDependencies[str, EditorDeps],
):
    """Ask a dedicated editor to change the program under verification.

    You do not edit the program yourself, and you should reach for this only after the conf, your
    rule and `summarize_for_prover` have all failed to move the block.

    **Describe the problem, not the edit.** You own the diagnosis — you are the one who saw the
    prover fail — and the editor owns what to do about it within its charter. A request that
    prescribes an edit is one the editor has to re-derive anyway; a request that names the symptom
    and the code in the way is one it can act on.

    Good: "Rule `rule_fee_conserved` comes back with no verdict; the trace stops in
    `calculate_fees` in `programs/vault/src/state/reserve.rs` on the `?` at its first fallible call,
    and the prover reports [3308] on the error type's Display impl."
    Good: "I need to state a property about the vault's share balance *during*
    `process_deposit`, after the transfer and before the mint, and there is no function boundary
    there to hang a rule on."
    Bad: "Add `#[cfg_attr(feature = \\"certora\\", cvlr::early_panic)]` to `calculate_fees`."

    What comes back has compiled and been approved by a reviewer, but is **not** applied yet: read
    the diff, and apply it with `commit_edit` and the key it names. Every applied edit is carried
    into the report with its justification, and applying one invalidates the prover stamp — re-run
    `verify_rules`.
    """

    request: str = Field(
        description="A short, concrete statement of the problem you want solved: what failed, "
        "which rule, and which code you believe is in the way."
    )

    @override
    async def run(self) -> str:
        draft = self.state["curr_spec"]
        if draft is None:
            return "No harness written yet — put a draft first, so the editor can build against it."
        with self.tool_deps() as deps:
            feature = deps.target.unit.feature
            state = await deps.runner(
                EditorInput(
                    input=[
                        f"The requesting unit's cargo feature is `{feature}`. Every edit you make "
                        f"is gated on it.",
                        self.request,
                    ],
                    request=self.request,
                    vfs=dict(self.state["vfs"]),
                    orig_vfs=dict(self.state["vfs"]),
                    reviewed_digest=None,
                    memory=None,
                    draft=draft,
                    summaries=list(self.state["summaries"]),
                ),
                self.tool_call_id,
            )
            assert "result" in state
            outcome = state["result"]
            if isinstance(outcome, MungeRefusal):
                return (
                    f"The editor refused your request:\n\n{outcome.explanation}\n\n"
                    "If the property cannot be stated without the change it declined to make, "
                    "record a skip naming the kind of change it would have needed."
                )
            key = await deps.store.commit(
                state["vfs"],
                executive_summary=outcome.executive_summary,
                why_sound=outcome.why_sound,
                attribution=MungeEditor(),
            )
            diff = summarize_changes(state, deps.mat, self.state["vfs"])
        return committed_edit_message(outcome, key, diff)


# ---------------------------------------------------------------------------------------------
# assembly


def _cvlr_review_prompts(b: FeedbackBuilder) -> FeedbackBuilder:
    return b.with_sys_prompt_template("cvlr_munge_review_system.j2").with_initial_prompt(
        with_cvlr_facts(
            "Review the following proposed edits to the program under verification. The author's "
            "original request, the editor's report, and the diff of the edits follow."
        )
    )


def editor_tools(
    ctx: WorkflowContext[CvlrGeneration],
    env: ServiceHost,
    *,
    target: HarnessTarget,
    editing: ProgramEditing,
) -> list[BaseTool]:
    """The author's program-editing tools: commission an edit, and apply, list and revert the ones
    it commissioned.

    No tool that writes the program is among them. One entity edits the program under
    verification, and this is where that is enforced rather than asked for
    (``docs/who-edits-the-program.md`` §4, move A).
    """
    editor_ctx = ctx.child(EDITOR_KEY)
    live = editing.live
    reviewer = munge_feedback_judge(editor_ctx, env, live, prompts=_cvlr_review_prompts)
    feature = target.unit.feature

    workflow = (
        env.builder_heavy()
        .with_input(EditorInput)
        .with_state(EditorState)
        .with_output_key("result")
        .with_sys_prompt_template("cvlr_munge_editor_system.j2")
        # No CVLR bundle, unlike every other CVLR agent. The charter forbids reasoning about the
        # Prover's internals, which are the bundle's subject: the author owns the diagnosis.
        .with_initial_prompt("Respond to the following edit request:")
        .with_tools(live.write_tools)
        .with_tools(
            [
                editor_ctx.get_memory_tool(),
                RequestReviewTool.bind(
                    ReviewDeps(
                        accessor=live.mat,
                        feedback=reviewer,
                        briefing=(
                            f"The requesting unit's cargo feature is `{feature}`. Every hunk must "
                            f"be gated on it, and on it alone.",
                        ),
                    )
                ).as_tool(REQUEST_REVIEW),
                SubmitEdits.bind(SubmitDeps(target, live.mat)).as_tool(SUBMIT_EDIT),
                GiveUpTool.as_tool(GIVE_UP),
            ]
        )
        .compile_async()
    )

    async def runner(inp: EditorInput, tid: str) -> EditorState:
        return await run_to_completion(
            graph=workflow,
            input=inp,
            description="CVLR code editor",
            recursion_limit=ctx.recursion_limit,
            within_tool=tid,
            thread_id=uniq_thread_id("cvlr-editor"),
        )

    return [
        CodeEditor.bind(
            EditorDeps(target=target, runner=runner, store=editing.store, mat=live.mat)
        ).as_tool(CODE_EDITOR),
        *edit_history_tools(editing.store, live.mat),
    ]
