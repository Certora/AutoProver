"""The munge editor: the one agent allowed to change the program under verification.

``docs/who-edits-the-program.md``. The author writes its own unit's harness and asks for changes to
the program in words; this agent makes them, earns a review, and proves they compiled. The author
never edits the program's source, which is the point — a tool that rewrites somebody's repository
should have one place that does it and one gate in front of it.

**Solana-only, and deliberately so.** §8.6 costs out the alternative — generalising the EVM editor
(:mod:`composer.spec.source.munge.munge_agent`) over both chains — and recommends this order: build
against a locally declared seam first, lift a shared core once there are two implementations to
generalise from. What transfers from EVM is the *topology* (request in, edit, earn a review, submit
behind a digest, or refuse) and almost none of the machinery, because of one decision:

**The editor edits records, not text.** EVM's editor writes into a VFS overlay of file contents. This
one calls a typed tool per munge kind and accumulates
:class:`~composer.spec.cvlr.munge.FunctionMunge` records. That is not a stylistic difference. A munge
is scoped to one unit because it is an attribute gated on that unit's cargo feature, and *a free-form
text edit has no ``cfg_attr``* — two units editing one file through an overlay is a collision with no
defined answer, where two units' records are two dormant lines. A text overlay would take the shared
working tree with it (``docs/single-working-tree.md`` §2.3). So the model never names the feature:
the tool does, from the requesting unit's identity.

Three things follow from records rather than text, and each replaces a piece of EVM machinery:

* the diff the reviewer sees is :func:`~composer.spec.cvlr.tree.munge_diff`, computed from state with
  no working tree, rather than an overlay comparison;
* the approval is tied to a hash of the record list rather than of a VFS, so any later edit voids it
  for the same reason;
* what the author receives back is a set of records to keep or drop, so ``revert`` costs nothing
  and needs no edit store.
"""

import dataclasses
import hashlib
import logging
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Annotated, Literal, NotRequired, Sequence, override

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import MessagesState
from langgraph.types import Command
from pydantic import BaseModel, Field

from graphcore.graph import FlowInput, tool_return, tool_state_update
from graphcore.tools.schemas import (
    WithAsyncDependencies,
    WithImplementation,
    WithInjectedId,
    WithInjectedState,
)

from composer.cargo.depinfo import compiled_sources
from composer.cargo.session import CompileFailed, Compiled
from composer.spec.context import (
    CacheKey,
    CvlrGeneration,
    EditorAgent,
    EditorJudge,
    WorkflowContext,
)
from composer.spec.cvlr.munge import (
    AlreadyMunged,
    DropMunges,
    EarlyPanic,
    FunctionAmbiguous,
    FunctionMunge,
    FunctionNotFound,
    HookOnEntry,
    HookOnExit,
    InlineNever,
    MockFn,
    MungeKind,
    Munged,
    NotProjectSource,
    apply_munge,
    merge_munges,
)

from composer.spec.cvlr.tree import NotInWorkdir, munge_diff
from composer.spec.cvlr.tuning import SummaryDirective
from composer.spec.cvlr.state import CvlrGenerationState
from composer.spec.cvlr.verify import HarnessTarget
from composer.spec.graph_builder import bind_standard, run_to_completion
from composer.spec.service_host import ServiceHost
from composer.spec.util import uniq_thread_id
from composer.ui.tool_display import tool_display

_log = logging.getLogger(__name__)

EDITOR_KEY = CacheKey[CvlrGeneration, EditorAgent]("cvlr-editor")
JUDGE_KEY = CacheKey[EditorAgent, EditorJudge]("cvlr-munge-review")


# ---------------------------------------------------------------------------------------------
# the vocabulary, as the editor selects it


class ApplyEarlyPanic(BaseModel):
    """Rewrite every `?` in the function to `.unwrap()`."""

    kind: Literal["early_panic"] = "early_panic"


class ApplyMockFn(BaseModel):
    """Replace the function with a stand-in the author has already written."""

    kind: Literal["mock_fn"] = "mock_fn"
    stand_in: str = Field(
        description="Path to the replacement, spelled as the munged file can reach it — a function "
        "the program already defines, or a `pub fn` in the requesting unit's harness module named "
        "as `crate::certora::specs::<module>::<fn>`. It must already exist with the same signature; "
        "you do not write it, the author does."
    )


class ApplyInlineNever(BaseModel):
    """Keep the function out of line so it still has a symbol."""

    kind: Literal["inline_never"] = "inline_never"


class ApplyHookOnEntry(BaseModel):
    """Insert an observing call as the function's first statement."""

    kind: Literal["hook_on_entry"] = "hook_on_entry"
    call: str = Field(
        description="The call to insert, as Rust — e.g. `crate::certora::specs::vault::saw_entry()`. "
        "It must observe only; a call that changes the program's state is outside the charter."
    )


class ApplyHookOnExit(BaseModel):
    """Insert an observing call before the function returns."""

    kind: Literal["hook_on_exit"] = "hook_on_exit"
    call: str = Field(
        description="The call to insert, as Rust. It must observe only."
    )


#: Discriminated because the kinds carry different fields, which is the same reason ``RuleSubject``
#: is — ``mock_fn`` needs a target, the hooks need an expression, and the other two take nothing.
type MungeChoice = Annotated[
    ApplyEarlyPanic | ApplyMockFn | ApplyInlineNever | ApplyHookOnEntry | ApplyHookOnExit,
    Field(discriminator="kind"),
]


def kind_of(choice: MungeChoice) -> MungeKind:
    match choice:
        case ApplyEarlyPanic():
            return EarlyPanic()
        case ApplyMockFn(stand_in=stand_in):
            return MockFn(stand_in=stand_in)
        case ApplyInlineNever():
            return InlineNever()
        case ApplyHookOnEntry(call=call):
            return HookOnEntry(call=call)
        case ApplyHookOnExit(call=call):
            return HookOnExit(call=call)


# ---------------------------------------------------------------------------------------------
# what the editor hands back


class EditsProposed(BaseModel):
    """The editor's account of a finished, reviewed, compiling change."""

    executive_summary: str = Field(
        description="What you changed, covering every munge you applied."
    )
    why_sound: str = Field(
        description="Why these changes are either sound, or an acceptable over-approximation for "
        "the problem the author described. Name the charter kind you used and make the actual case."
    )
    how_to_apply: str | None = Field(
        default=None,
        description="What the author may still need to do — a stand-in to write, a rule to "
        "re-check. Null when there is nothing.",
    )


class EditsRefused(BaseModel):
    """Why the request cannot be honoured within the charter."""

    explanation: str = Field(
        description="Why the requested change cannot or should not be made, in the author's terms."
    )


type EditorOutcome = EditsProposed | EditsRefused


# ---------------------------------------------------------------------------------------------
# the compile gate


@dataclasses.dataclass(frozen=True)
class Accepted:
    """The candidate set builds, and every munge in it reached the compiler."""


@dataclasses.dataclass(frozen=True)
class DoesNotCompile:
    diagnostics: str


@dataclasses.dataclass(frozen=True)
class NotCompiled:
    """The build succeeded and these munged files were never read by rustc.

    The Solana ``EditsNotCompiled``, and the failure it catches is the quietest one this backend
    has: an attribute in a file no enabled feature reaches changes nothing, reports nothing, and
    leaves the report carrying a source-edit record for a change that had no effect
    (``docs/munge-and-working-copies.md`` §8 gap 2).
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
    candidate: Sequence[FunctionMunge],
    proposed: Sequence[FunctionMunge],
) -> GateOutcome:
    """Stage ``candidate`` into the run's working tree, compile it, and check ``proposed`` arrived.

    The author's side of the build rides in rather than being captured when the editor was built:
    the draft moves under the editor's feet, and compiling a candidate against a stale one would
    check something nobody is going to submit.

    Nothing needs undoing afterwards. The tree is derived from state, so the author's next stage
    rewrites every munged file from the pristine copy and a candidate that was never committed
    simply is not there (``docs/single-working-tree.md`` §4).
    """
    async with target.build_slot():
        target.stage(draft, summaries, candidate)
        run = await target.session.check(package=target.package, features=target.features)
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
    missed = tuple(sorted({m.path for m in proposed if (root / m.path).resolve() not in reached}))
    return NotCompiled(missed) if missed else Accepted()


# ---------------------------------------------------------------------------------------------
# state


def _digest(munges: Sequence[FunctionMunge]) -> str:
    """What an approval is tied to: the edits, not their prose.

    Keyed on ``edit_id`` — the file, the function, the attribute and the feature — so re-wording a
    justification does not void a review, and changing what the compiler sees does. Same trade as
    ``munge_history``, for the same reason.
    """
    joined = "\n".join(sorted(m.edit_id for m in munges))
    return hashlib.sha256(joined.encode()).hexdigest()


class EditorStateExtra(MessagesState):
    #: The author's problem statement, carried through to the reviewer so it can judge whether the
    #: edits stayed on script rather than merely being defensible in the abstract.
    request: str
    #: The requesting unit's cargo feature. Set by the host, never by the model: it is what scopes
    #: every munge to one unit.
    feature: str
    #: The author's side of the build, as it stood when the request was made. Carried rather than
    #: captured at construction because the editor compiles a candidate *with* the author's harness
    #: and existing munges, and a stale copy would gate something nobody is going to submit.
    draft: str
    summaries: list[SummaryDirective]
    committed: list[FunctionMunge]
    proposed: Annotated[list[FunctionMunge], merge_munges]
    #: Hash of the record list the reviewer approved. ``submit`` fires only when it still matches, so
    #: any edit after approval silently voids it.
    reviewed_digest: str | None
    memory: str | None


class EditorInput(FlowInput):
    request: str
    feature: str
    draft: str
    summaries: list[SummaryDirective]
    committed: list[FunctionMunge]
    proposed: list[FunctionMunge]
    reviewed_digest: str | None
    memory: str | None


class EditorState(EditorStateExtra):
    result: NotRequired[EditorOutcome]


# ---------------------------------------------------------------------------------------------
# the editor's tools


@tool_display(lambda p: f"Munging `{p['function']}`", "Munge")
class MungeFunction(
    WithInjectedState[EditorStateExtra],
    WithInjectedId,
    WithAsyncDependencies[Command | str, HarnessTarget],
):
    """Put one verification-only attribute on one of the program's own functions.

    Gated behind the requesting unit's cargo feature, so the deployed build is untouched and every
    other unit compiles the function as written. You do not choose the feature and cannot: it comes
    from the unit that asked.

    Apply as many as the request needs, then `request_review`. A munge you regret is `drop_munge`.

    * **`early_panic`** — every `?` in the function becomes `.unwrap()`, so its error paths panic and
      the prover prunes them. The remedy when a `?` *inside* the program, below the handler a rule
      calls, has an error construction the pointer analysis refuses. It removes the failure path
      rather than exposing one, so it cannot make an *acceptance* property statable.
    * **`mock_fn`** — the function is replaced by a stand-in the author has written. A mock
      *computes* where a prover summary havocs, so a property downstream of one still means
      something. You never write the stand-in.
    * **`inline_never`** — the function keeps a symbol of its own. Behaviour-preserving outright; for
      when a summary, an inlining directive or a counterexample needs a symbol to name.
    * **`hook_on_entry` / `hook_on_exit`** — an observing call at the function's first or last
      statement. The only way to reach a point *inside* an execution. Observation only: a call that
      changes the program's state is a rewrite, and outside your charter.

    If what the request needs is none of these, `give_up` and say which kind it would have taken.
    Do not improvise a sixth.
    """

    path: str = Field(
        description="The file to munge, relative to the workspace root — e.g. "
        "`programs/vault/src/state/reserve.rs`."
    )
    function: str = Field(
        description="The name of the function, as its `fn` line spells it. Not a path: the file "
        "plus the name is what identifies it, and two functions of one name in a file is refused "
        "rather than guessed at."
    )
    munge: MungeChoice = Field(description="Which attribute to apply")
    why: str = Field(
        description="What this attribute does to this function and why it is acceptable for the "
        "problem the author described. It goes into the report; it is the only account a reader "
        "gets of what the program was changed to."
    )

    @override
    async def run(self) -> Command | str:
        if not self.why.strip():
            return (
                "A non-empty `why` is required. A munge changes the program under verification, so "
                "an unexplained one leaves the report claiming a property of code nobody can "
                "account for."
            )
        with self.tool_deps() as target:
            match target.source_path(self.path):
                case NotInWorkdir():
                    return (
                        f"{self.path} resolves outside this project. Munge the program's own "
                        f"source, with a path relative to the workspace root."
                    )
                case NotProjectSource(directory=directory):
                    return (
                        f"{self.path} is under `{directory}`, which is not this project's source. "
                        f"A munge changes the program under verification; `{directory}` holds "
                        f"build output or the dependency sources cargo resolved, and modifying a "
                        f"dependency changes its behaviour for every crate in the graph — "
                        f"including the ones the author's property is about. If the code in the "
                        f"way is a dependency's, refuse and say so."
                    )
                case Path() as resolved:
                    pass
            if not resolved.is_file():
                return f"{self.path} is not a file in this project."
            record = FunctionMunge(
                path=self.path,
                function=self.function,
                kind=kind_of(self.munge),
                why=self.why,
                feature=self.state["feature"],
            )
            if any(m.edit_id == record.edit_id for m in self.state["proposed"]):
                return f"{self.function} in {self.path} already carries that attribute."
            # A dry run against what is on disk, so a name that matches nothing is a tool error you
            # can act on rather than a build failure two minutes later.
            match apply_munge(resolved.read_text(), record):
                case FunctionNotFound(nearby=nearby):
                    suggestion = f" This file does define: {', '.join(nearby)}." if nearby else ""
                    return f"{self.path} defines no function named {self.function}.{suggestion}"
                case FunctionAmbiguous(lines=lines):
                    return (
                        f"{self.path} defines {self.function} {len(lines)} times (lines "
                        f"{', '.join(str(n) for n in lines)}). Munging the wrong one compiles and "
                        f"changes nothing you can see, so this is refused: munge a caller that is "
                        f"unambiguous, or give up and say why."
                    )
                case AlreadyMunged(line=line):
                    return f"{self.path}:{line} already carries that attribute in the program."
                case Munged(line=line):
                    pass
        return Command(
            update={
                "proposed": [record],
                # Any edit after a review voids it, which is what keeps an approved diff and a
                # submitted one the same diff.
                "reviewed_digest": None,
                "messages": [
                    ToolMessage(
                        tool_call_id=self.tool_call_id,
                        content=(
                            f"Applied {record.kind.attribute()} to {self.path}:{line} "
                            f"({self.function}), gated on `{record.feature}`."
                        ),
                    )
                ],
            }
        )


@tool_display(lambda p: f"Dropping `{p['edit_id']}`", "Drop")
class DropMunge(WithInjectedState[EditorStateExtra], WithInjectedId, WithImplementation[Command | str]):
    """Take back one munge you applied. Voids any review you have earned."""

    edit_id: str = Field(description="The `edit_id` reported when the munge was applied.")

    @override
    def run(self) -> Command | str:
        if not any(m.edit_id == self.edit_id for m in self.state["proposed"]):
            held = ", ".join(m.edit_id for m in self.state["proposed"]) or "none"
            return f"No proposed munge has that id. You are holding: {held}."
        return Command(
            update={
                "proposed": DropMunges(frozenset({self.edit_id})),
                "reviewed_digest": None,
                "messages": [
                    ToolMessage(tool_call_id=self.tool_call_id, content="Dropped.")
                ],
            }
        )


# ---------------------------------------------------------------------------------------------
# the reviewer


class EditReview(BaseModel):
    """The reviewer's verdict on the proposed munges."""

    good: bool = Field(description="Whether the edits are acceptable as they stand.")
    feedback: str = Field(
        description="Actionable feedback when work is needed; may be empty when they are good."
    )


class ReviewState(MessagesState):
    memory: str | None
    result: NotRequired[EditReview]


class ReviewInput(FlowInput):
    memory: str | None


#: ``(review parts, calling tool_call_id) -> verdict``.
type ReviewThunk = Callable[[list[str | dict], str], Awaitable[EditReview]]


def build_reviewer(
    ctx: WorkflowContext[EditorAgent], env: ServiceHost, read_tools: Sequence[BaseTool]
) -> ReviewThunk:
    """The editor's own reviewer — a *submit* gate, and not the same thing as the property judge.

    Two reviews, answering different questions (``docs/who-edits-the-program.md`` §8.5). This one
    asks whether the editor did what was asked, faithfully, within charter; it never sees the
    properties, exactly as EVM's does not. The contextual property judge asks the other question —
    whether the rules still mean anything given these munges — at publish time, with the batch in
    hand. Neither substitutes for the other.

    ``read_tools`` is how it reads the program: a verdict on a munge that has not looked at the
    function being munged is a verdict on prose.
    """
    review_ctx = ctx.child(JUDGE_KEY)
    workflow = (
        bind_standard(env.builder_heavy(), ReviewState)
        .with_input(ReviewInput)
        .with_sys_prompt_template("cvlr_munge_review_system.j2")
        .with_initial_prompt("Review the following proposed edits to the program under verification:")
        .with_tools([review_ctx.get_memory_tool(), *read_tools])
        .compile_async()
    )

    async def review(parts: list[str | dict], within_tool: str) -> EditReview:
        res = await run_to_completion(
            workflow,
            ReviewInput(input=parts, memory=None),
            thread_id=uniq_thread_id("cvlr-munge-review"),
            recursion_limit=ctx.recursion_limit,
            description="CVLR munge reviewer",
            within_tool=within_tool,
        )
        assert "result" in res
        return res["result"]

    return review


# ---------------------------------------------------------------------------------------------
# completion


@dataclasses.dataclass(frozen=True)
class ReviewDeps:
    pristine: Path
    review: ReviewThunk


@tool_display("Requesting review", "Review")
class RequestReview(
    WithInjectedState[EditorStateExtra],
    WithInjectedId,
    WithAsyncDependencies[Command | str, ReviewDeps],
):
    """Ask the reviewer to evaluate the munges you have applied.

    You must earn an approving review before you can `submit_edits`, and the approval is tied to the
    exact munges you hold now — applying or dropping one afterwards voids it and you must ask again.
    """

    summary: EditsProposed = Field(description="Your account of the changes you want reviewed.")

    @override
    async def run(self) -> Command | str:
        proposed = self.state["proposed"]
        if not proposed:
            return "You have applied no munges, so there is nothing to review."
        with self.tool_deps() as deps:
            parts: list[str | dict] = [
                f"The author's request to the editor:\n\n{self.state['request']}",
                f"Executive summary:\n{self.summary.executive_summary}",
                f"Soundness argument:\n{self.summary.why_sound}",
            ]
            if self.summary.how_to_apply:
                parts.append(f"Integration notes:\n{self.summary.how_to_apply}")
            parts.append(
                "Every attribute below is gated on the requesting unit's own cargo feature, so it "
                "is inert in the deployed build and in every other unit's verification. What is at "
                "issue is whether it is the right change for the stated problem, not whether it "
                "escapes."
            )
            parts.append("The diff of the proposed edits:")
            parts.append(munge_diff(deps.pristine, tuple(proposed)))
            verdict = await deps.review(parts, self.tool_call_id)
        if verdict.good:
            return Command(
                update={
                    "reviewed_digest": _digest(proposed),
                    "messages": [
                        ToolMessage(
                            tool_call_id=self.tool_call_id,
                            content=(
                                "The reviewer approved these edits. Call `submit_edits` now; do "
                                "not change anything first."
                            ),
                        )
                    ],
                }
            )
        return tool_return(
            self.tool_call_id, f"The reviewer has feedback you must address:\n\n{verdict.feedback}"
        )


@tool_display("Submitting edits", "Submit")
class SubmitEdits(
    WithInjectedState[EditorStateExtra],
    WithInjectedId,
    WithAsyncDependencies[Command | str, HarnessTarget],
):
    """Submit your finished munges.

    Accepted only when an approving `request_review` still stands for these exact munges **and**
    they compile with every one of them actually reached by the build. On failure you get the reason
    and keep working; this does not end your turn.
    """

    summary: EditsProposed = Field(description="Your account of the completed changes.")

    @override
    async def run(self) -> Command | str:
        proposed = self.state["proposed"]
        if not proposed:
            return "You have applied no munges. Apply one, or `give_up` and say why."
        if self.state["reviewed_digest"] != _digest(proposed):
            return (
                "These edits are not approved as they stand. Call `request_review` on what you "
                "hold now — applying or dropping a munge since your last review voided it."
            )
        with self.tool_deps() as target:
            outcome = await gate_edits(
                target,
                draft=self.state["draft"],
                summaries=self.state["summaries"],
                candidate=[*self.state["committed"], *proposed],
                proposed=proposed,
            )
            match outcome:
                case DoesNotCompile(diagnostics=diagnostics):
                    return f"Your edits do not compile; fix them before submitting.\n\n{diagnostics}"
                case NotCompiled(paths=paths):
                    return (
                        "The build succeeded, but rustc never read "
                        f"{', '.join(paths)} — so your edits to those files changed nothing about "
                        "what is verified. The file is behind a `cfg` this build does not enable, "
                        "or nothing declares its module. Munge a file the build actually compiles, "
                        "or give up and say so."
                    )
                case NotChecked(reason=reason):
                    _log.warning("cvlr: submitting unverified edits — %s", reason)
                case Accepted():
                    pass
        return Command(
            update={
                "result": self.summary,
                "messages": [
                    ToolMessage(tool_call_id=self.tool_call_id, content="Edits accepted.")
                ],
            }
        )


@tool_display("Refusing the request", "Refuse")
class GiveUpEditing(WithInjectedId, WithImplementation[Command]):
    """Refuse the request.

    A first-class outcome, not a failure. Refuse when the change is outside the five kinds, when it
    would need a rewrite rather than an attribute, when the code in the way belongs to a dependency,
    or when no attribute would honestly solve the stated problem. Name the kind of change it would
    have taken — the author turns that into a recorded skip, and a skip naming a missing kind is how
    the vocabulary earns its next entry.
    """

    explanation: str = Field(description="Why the change cannot or should not be made.")

    @override
    def run(self) -> Command:
        return Command(
            update={
                "result": EditsRefused(explanation=self.explanation),
                "messages": [
                    ToolMessage(tool_call_id=self.tool_call_id, content="Acknowledged.")
                ],
            }
        )


# ---------------------------------------------------------------------------------------------
# the author's side


@dataclasses.dataclass(frozen=True)
class EditorDeps:
    """What the author-facing tool needs to run one editor conversation."""

    target: HarnessTarget
    pristine: Path
    runner: Callable[[EditorInput, str], Awaitable[EditorState]]


@tool_display("Asking the code editor", "Editor")
class CodeEditor(
    WithInjectedState[CvlrGenerationState],
    WithInjectedId,
    WithAsyncDependencies[Command | str, EditorDeps],
):
    """Ask a dedicated editor to change the program under verification.

    You do not edit the program yourself, and you should reach for this only after the conf, your
    rule and `summarize_for_prover` have all failed to move the block.

    **Describe the problem, not the edit.** You own the diagnosis — you are the one who saw the
    prover fail — and the editor owns what to do about it within its charter. A request that
    prescribes an attribute is one the editor has to re-derive anyway; a request that names the
    symptom and the code in the way is one it can act on.

    Good: "Rule `rule_fee_conserved` comes back with no verdict; the trace stops in
    `calculate_fees` in `programs/vault/src/state/reserve.rs` on the `?` at its first fallible call,
    and the prover reports [3308] on the error type's Display impl."
    Good: "I need to state a property about the vault's share balance *during*
    `process_deposit`, after the transfer and before the mint, and there is no function boundary
    there to hang a rule on."
    Bad: "Add `#[cfg_attr(feature = \\"certora\\", cvlr::early_panic)]` to `calculate_fees`."

    What comes back is applied to your build already: the editor's munges compiled, a reviewer
    approved them, and they are in your munge list. Read the diff. If you disagree, `revert_munge`.
    Every munge is carried into the report with its justification, and applying one invalidates the
    prover stamp — re-run `verify_rules`.
    """

    request: str = Field(
        description="A short, concrete statement of the problem you want solved: what failed, "
        "which rule, and which code you believe is in the way."
    )

    @override
    async def run(self) -> Command | str:
        draft = self.state["curr_spec"]
        if draft is None:
            return "No harness written yet — put a draft first, so the editor can build against it."
        with self.tool_deps() as deps:
            unit = deps.target.unit
            state = await deps.runner(
                EditorInput(
                    input=[self.request],
                    request=self.request,
                    feature=unit.feature,
                    draft=draft,
                    summaries=list(self.state["summaries"]),
                    committed=list(self.state["munges"]),
                    proposed=[],
                    reviewed_digest=None,
                    memory=None,
                ),
                self.tool_call_id,
            )
            assert "result" in state
            outcome = state["result"]
            if isinstance(outcome, EditsRefused):
                return (
                    f"The editor refused your request:\n\n{outcome.explanation}\n\n"
                    "If the property cannot be stated without the change it declined to make, "
                    "record a skip naming the kind of change it would have needed."
                )
            applied = state["proposed"]
            diff = munge_diff(deps.pristine, tuple(applied))
        listing = "\n".join(f"  {m.edit_id}" for m in applied)
        return tool_state_update(
            self.tool_call_id,
            f"""The editor applied {len(applied)} munge(s) to the program.

**What changed**
{outcome.executive_summary}

**Why it is sound**
{outcome.why_sound}

**Notes for you**
{outcome.how_to_apply or "(none)"}

These are now part of your build, and the prover stamp is invalidated — re-run `verify_rules`. To
take one back, `revert_munge` with its id:
{listing}

-----

{diff}""",
            munges=applied,
        )


@tool_display(lambda p: f"Reverting `{p['edit_id']}`", "Revert")
class RevertMunge(
    WithInjectedState[CvlrGenerationState], WithInjectedId, WithImplementation[Command | str]
):
    """Take back a munge the editor applied, undoing its change to the program.

    The final say is yours: you hold the properties, and a munge that is defensible in the abstract
    can still be wrong for the batch you are proving. The file goes back to what the project ships,
    and the prover stamp goes with it.
    """

    edit_id: str = Field(description="The id reported when the munge was applied.")

    @override
    def run(self) -> Command | str:
        if not any(m.edit_id == self.edit_id for m in self.state["munges"]):
            held = ", ".join(m.edit_id for m in self.state["munges"]) or "none"
            return f"No munge has that id. Your build carries: {held}."
        return tool_state_update(
            self.tool_call_id,
            f"Reverted {self.edit_id}. The program is back to what the project ships for that "
            "function, and the prover stamp is invalidated — re-run `verify_rules`.",
            munges=DropMunges(frozenset({self.edit_id})),
        )


# ---------------------------------------------------------------------------------------------
# assembly


def editor_tools(
    ctx: WorkflowContext[CvlrGeneration],
    env: ServiceHost,
    *,
    target: HarnessTarget,
    pristine: Path,
    read_tools: Sequence[BaseTool],
) -> list[BaseTool]:
    """The author's two program-editing tools: commission an edit, and take one back.

    ``munge_function`` is deliberately **not** among them. One entity edits the program under
    verification, and this is where that is enforced rather than asked for
    (``docs/who-edits-the-program.md`` §4, move A).
    """
    editor_ctx = ctx.child(EDITOR_KEY)
    reviewer = build_reviewer(editor_ctx, env, read_tools)

    workflow = (
        env.builder_heavy()
        .with_input(EditorInput)
        .with_state(EditorState)
        .with_output_key("result")
        .with_sys_prompt_template("cvlr_munge_editor_system.j2")
        .with_initial_prompt("Respond to the following edit request:")
        .with_tools(read_tools)
        .with_tools(
            [
                editor_ctx.get_memory_tool(),
                MungeFunction.bind(target).as_tool("munge_function"),
                DropMunge.as_tool("drop_munge"),
                RequestReview.bind(
                    ReviewDeps(pristine=pristine, review=reviewer)
                ).as_tool("request_review"),
                SubmitEdits.bind(target).as_tool("submit_edits"),
                GiveUpEditing.as_tool("give_up"),
            ]
        )
        .compile_async()
    )

    async def runner(inp: EditorInput, tid: str) -> EditorState:
        return await run_to_completion(
            graph=workflow,
            input=inp,
            description="CVLR munge editor",
            recursion_limit=ctx.recursion_limit,
            within_tool=tid,
            thread_id=uniq_thread_id("cvlr-editor"),
        )

    return [
        CodeEditor.bind(
            EditorDeps(target=target, pristine=pristine, runner=runner)
        ).as_tool("code_editor"),
        RevertMunge.as_tool("revert_munge"),
    ]
