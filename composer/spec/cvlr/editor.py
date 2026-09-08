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

The one kind that is a genuine rewrite rather than an attribute — extraction, ``§8.4`` — is a record
too, and for the same reason. :class:`~composer.spec.cvlr.munge.FunctionExtraction` stores the text
it replaces and renders the ``#[cfg]`` pair itself, so the deployed build is untouched by
construction rather than by the model remembering to gate what it wrote.
"""

import dataclasses
import hashlib
import logging
import re
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
    DeriveAttributeUnreadable,
    DeriveNotFound,
    DeriveSwap,
    DropMunges,
    EarlyPanic,
    FunctionAmbiguous,
    FunctionExtraction,
    FunctionItem,
    FunctionMunge,
    FunctionNotFound,
    HookOnEntry,
    HookOnExit,
    InlineNever,
    MockFn,
    Munge,
    MungeKind,
    Munged,
    NoFunctionBody,
    NotProjectSource,
    SwappedDerive,
    apply_attribute,
    apply_derive_swap,
    apply_extraction,
    function_item,
    function_names,
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
    candidate: Sequence[Munge],
    proposed: Sequence[Munge],
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
        await target.stage(draft, summaries, candidate)
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


def _digest(munges: Sequence[Munge]) -> str:
    """What an approval is tied to: the edits, not their prose.

    Keyed on ``edit_id`` — the file, the function, the feature, and the attribute or a digest of the
    rewrite — so re-wording a justification does not void a review, and changing what the compiler
    sees does. Same trade as ``munge_history``, for the same reason.
    """
    joined = "\n".join(sorted(m.edit_id for m in munges))
    return hashlib.sha256(joined.encode()).hexdigest()


def _latest_review(_current: str | None, update: str | None) -> str | None:
    """Last write wins, which is the right answer in both directions.

    Two edits in one step both write ``None`` and the fold gives ``None`` — no approval survives an
    edit, which is the invariant ``submit`` depends on. An edit landing beside a review is
    order-dependent and safe either way: the digest ``request_review`` computed was taken *before*
    the sibling edit joined :attr:`proposed`, so if it wins it no longer matches what ``submit``
    hashes and the submission is refused. The unsafe direction — a stale approval passing — is not
    reachable.
    """
    return update


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
    committed: list[Munge]
    proposed: Annotated[list[Munge], merge_munges]
    #: Hash of the record list the reviewer approved. ``submit`` fires only when it still matches, so
    #: any edit after approval silently voids it.
    #:
    #: Reduced rather than plain, because every edit tool writes it and the model can call two in
    #: one step — which is legitimate, and which :attr:`proposed` already merges. Without a reducer
    #: that step dies with ``InvalidUpdateError`` and takes the component with it.
    reviewed_digest: Annotated[str | None, _latest_review]
    memory: str | None


class EditorInput(FlowInput):
    request: str
    feature: str
    draft: str
    summaries: list[SummaryDirective]
    committed: list[Munge]
    proposed: list[Munge]
    reviewed_digest: str | None
    memory: str | None


class EditorState(EditorStateExtra):
    result: NotRequired[EditorOutcome]


# ---------------------------------------------------------------------------------------------
# the editor's tools


def _held(state: EditorStateExtra) -> list[Munge]:
    """Every edit this unit has against the program — landed and proposed.

    One list rather than two because the checks below are about the *file*, and the file does not
    know which of them the author has already accepted. Only this unit's: a sibling's edits are
    dormant in this build and cannot collide with these.
    """
    return [*state["committed"], *state["proposed"]]


def _extraction_of(state: EditorStateExtra, path: str, function: str) -> FunctionExtraction | None:
    return next(
        (
            m
            for m in _held(state)
            if isinstance(m, FunctionExtraction) and m.path == path and m.function == function
        ),
        None,
    )


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

    If the request needs a *new function boundary* rather than a line on an existing function,
    that is `extract_function`, not one of these. If it is neither, `give_up` and say which kind it
    would have taken; do not improvise one.
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
            match target.pristine_source(self.path):
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
            if any(m.edit_id == record.edit_id for m in _held(self.state)):
                return f"{self.function} in {self.path} already carries that attribute."
            if (split := _extraction_of(self.state, self.path, self.function)) is not None:
                return (
                    f"You are already splitting {self.function} ({split.edit_id}), and an attribute "
                    f"here would land on the copy this unit does *not* compile — a change that "
                    f"builds, reports nothing and does nothing. Put the attribute on the extracted "
                    f"function by writing it into that item's own text, or `drop_munge` the split."
                )
            # A dry run against the source replay will act on, so a name that matches nothing is a
            # tool error you can act on rather than a build failure two minutes later.
            match apply_attribute(resolved.read_text(), record):
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


@tool_display(lambda p: f"Extracting `{p['extracted_name']}`", "Extract")
class ExtractFunction(
    WithInjectedState[EditorStateExtra],
    WithInjectedId,
    WithAsyncDependencies[Command | str, HarnessTarget],
):
    """Split one of the program's functions so a rule can drive a piece of it directly.

    The one kind here that is not an attribute and the only one that restructures code. It answers a
    property about a state transition *inside* a function, where there is no boundary to hang a rule
    on — and only when the author needs to **call** that transition. If observing the point is
    enough, `hook_on_entry` / `hook_on_exit` is far smaller and you should prefer it.

    What lands in the file is three items, and the tool writes the `cfg` lines rather than you:

    ```rust
    #[cfg(not(feature = "<the unit that asked>"))]
    fn settle(..) { /* the original, byte for byte */ }
    #[cfg(feature = "<the unit that asked>")]
    fn settle(..) { settle_transition(..) }      // your `replacement`
    #[cfg(feature = "<the unit that asked>")]
    pub fn settle_transition(..) { .. }          // your `extracted`
    ```

    So the deployed build and every sibling unit compile the function exactly as the project wrote
    it. Two things follow, and both are refusals rather than judgement calls:

    * **`settle` must still do what it did.** Moving code into the extracted function is the entire
      change. Dropping a check, reordering effects or simplifying arithmetic on the way is a
      rewrite — `give_up` instead. This kind is a refactoring, which is what makes it cheap to
      justify; an extraction that changes behaviour is the most expensive thing you could do.
    * **The signature stays identical.** Every caller in the program compiles against whichever half
      its features select, so the two must be interchangeable. The tool checks this and refuses.

    The extracted function must be `pub`, and the author's rule has to be able to name it — if the
    module it lands in is not reachable from `crate::certora::specs`, say so and refuse: you cannot
    change visibility, and a `pub fn` inside a private module is not callable from the harness.
    """

    path: str = Field(
        description="The file holding the function to split, relative to the workspace root."
    )
    function: str = Field(
        description="The name of the enclosing function, as its `fn` line spells it. It keeps this "
        "name and this signature; only its body changes."
    )
    extracted_name: str = Field(
        description="The name of the new function. It must not already exist in the file."
    )
    replacement: str = Field(
        description="The complete definition of `function` as it should read under the feature — "
        "same signature, body delegating to the extracted function. Write the whole item, `fn` line "
        "included, and no `#[cfg]`: the tool adds that."
    )
    extracted: str = Field(
        description="The complete definition of the new function, `pub`, holding the code moved out "
        "of `function`. Write the whole item; the tool adds the `#[cfg]`."
    )
    why: str = Field(
        description="What the author cannot state without this boundary, and why the split is "
        "behaviour-preserving. It goes into the report as the account of what the program became."
    )

    @override
    async def run(self) -> Command | str:
        if not self.why.strip():
            return (
                "A non-empty `why` is required. An extraction restructures the program under "
                "verification, so an unexplained one leaves the report claiming a property of code "
                "nobody can account for."
            )
        with self.tool_deps() as target:
            match target.pristine_source(self.path):
                case NotInWorkdir():
                    return (
                        f"{self.path} resolves outside this project. Extract from the program's own "
                        f"source, with a path relative to the workspace root."
                    )
                case NotProjectSource(directory=directory):
                    return (
                        f"{self.path} is under `{directory}`, which is not this project's source. "
                        f"A munge changes the program under verification; `{directory}` holds build "
                        f"output or the dependency sources cargo resolved. If the code in the way "
                        f"is a dependency's, refuse and say so."
                    )
                case Path() as resolved:
                    pass
            if not resolved.is_file():
                return f"{self.path} is not a file in this project."
            source = resolved.read_text()
            match function_item(source, self.function):
                case FunctionNotFound(nearby=nearby):
                    suggestion = f" This file does define: {', '.join(nearby)}." if nearby else ""
                    return f"{self.path} defines no function named {self.function}.{suggestion}"
                case FunctionAmbiguous(lines=lines):
                    return (
                        f"{self.path} defines {self.function} {len(lines)} times (lines "
                        f"{', '.join(str(n) for n in lines)}). Splitting the wrong one of two "
                        f"same-named functions compiles and changes nothing you can see, so this "
                        f"is refused: name a function that is unambiguous, or give up and say why."
                    )
                case NoFunctionBody():
                    return (
                        f"{self.function} in {self.path} is a declaration with no body — a trait "
                        f"method or an `extern` entry. There is nothing to extract from it; the "
                        f"implementation you want is elsewhere."
                    )
                case FunctionItem() as original:
                    pass
            if self.extracted_name in function_names(source):
                return (
                    f"{self.path} already mentions a function named {self.extracted_name}. Pick a "
                    f"name the file does not use, so the extracted function is unambiguous."
                )
            match function_item(self.replacement, self.function):
                case FunctionItem() as rewritten:
                    pass
                case _:
                    return (
                        f"`replacement` must be exactly one complete definition of {self.function}, "
                        f"`fn` line through closing brace, and no `#[cfg]`."
                    )
            if rewritten.signature != original.signature:
                return (
                    "The replacement changes the signature, which every caller in the program "
                    "compiles against. Reproduce it exactly.\n"
                    f"  the program's: {original.signature}\n"
                    f"  yours:         {rewritten.signature}"
                )
            match function_item(self.extracted, self.extracted_name):
                case FunctionItem() as lifted:
                    pass
                case _:
                    return (
                        f"`extracted` must be exactly one complete definition of "
                        f"{self.extracted_name}, `fn` line through closing brace, and no `#[cfg]`."
                    )
            if not lifted.text.lstrip().startswith("pub"):
                return (
                    f"{self.extracted_name} must be `pub`. The whole point of the split is that the "
                    f"author's rule can call it, and it lives in the program's module, not theirs."
                )
            if not re.search(rf"\b{re.escape(self.extracted_name)}\b", self.replacement):
                return (
                    f"The replacement never calls {self.extracted_name}, so the two halves of the "
                    f"pair do different things. The feature-on `{self.function}` has to delegate to "
                    f"what you extracted."
                )
            record = FunctionExtraction(
                path=self.path,
                function=self.function,
                extracted_name=self.extracted_name,
                original=original.text,
                replacement=self.replacement,
                extracted=self.extracted,
                why=self.why,
                feature=self.state["feature"],
            )
            if any(m.edit_id == record.edit_id for m in _held(self.state)):
                return f"You have already recorded exactly that split of {self.function}."
            if (split := _extraction_of(self.state, self.path, self.function)) is not None:
                return (
                    f"You are already splitting {self.function} ({split.edit_id}). Two splits of "
                    f"one function for one unit are two definitions of it under the same feature, "
                    f"which does not build. `drop_munge` that one and record a single split that "
                    f"does both."
                )
            if any(
                isinstance(m, FunctionMunge)
                and m.path == self.path
                and m.function == self.function
                for m in _held(self.state)
            ):
                return (
                    f"This unit already has an attribute on {self.function}, and it would land on "
                    f"the copy the split leaves for everyone else — so it would stop doing "
                    f"anything. Drop it and write it into the extracted item's own text, or do not "
                    f"split this function."
                )
            match apply_extraction(source, record):
                case Munged(line=line):
                    pass
                case refusal:
                    return (
                        f"That split cannot be applied to {self.path} as it stands ({refusal}). "
                        f"Re-read the file."
                    )
        return Command(
            update={
                "proposed": [record],
                "reviewed_digest": None,
                "messages": [
                    ToolMessage(
                        tool_call_id=self.tool_call_id,
                        content=(
                            f"Split {self.path}:{line} ({self.function}), gated on "
                            f"`{record.feature}`. `{self.extracted_name}` is now a function of its "
                            f"own under that feature; the original is preserved verbatim for every "
                            f"build without it. Tell the author in `how_to_apply` how a rule names "
                            f"it — the compile gate is the only thing that will catch an "
                            f"unreachable path."
                        ),
                    )
                ],
            }
        )


@tool_display(lambda p: f"Swapping derives on `{p['swaps'][0]['type_name']}`", "Derives")
class SwapDerive(
    WithInjectedState[EditorStateExtra],
    WithInjectedId,
    WithAsyncDependencies[Command | str, HarnessTarget],
):
    """Move a type's derived trait impls behind this unit's feature, so the harness supplies them.

    The kind for code no function names. `mock_fn` replaces a function; a `#[derive(..)]` generates
    an impl with no name to give it, so when the Prover cannot follow a program's own
    (de)serialization — `[3005] memcpy with dynamically sized length`, `[3308] illegal dereference`,
    or an internal `sbf.domains.ScalarDomain` failure — the derive itself is the only handle.

    What lands is two `cfg_attr` lines above the type; derives you do not name stay on an ungated
    `derive`, so the deployed build sees exactly what it saw:

    ```rust
    #[cfg_attr(not(feature = "<the unit that asked>"), derive(BorshDeserialize, BorshSerialize))]
    #[cfg_attr(feature = "<the unit that asked>", derive(Copy))]
    #[derive(Clone, Debug, Default, PartialEq, BorshSchema)]   // untouched
    pub struct StakePool { .. }
    ```

    **This is the most consequential kind in the charter and the author has to live with what it
    costs.** Removing a deserializer means deserialization can no longer fail and no longer reads
    the account's bytes, so every property about *encoding* — a malformed account rejected, a
    truncated one rejected, a field surviving a round trip — becomes unprovable and will pass
    while meaning nothing. If the author's property is of that shape, refuse and say so.

    **`swaps` is a cascade, not one type.** `derive(Copy)` requires every field type to be `Copy`,
    so a struct usually drags its enums with it. Record them together: they stand or fall as one,
    and half a cascade does not compile. rustc names them one at a time, so read its error and add
    the next.

    Three things you do not do here. You do not write the impls — the author does, in their own
    module, because their harness is where a stand-in belongs. You do not choose what the replacement
    means. And you do not swap a type from a dependency: only this project's own source.
    """

    class Swap(BaseModel):
        """One type's derives."""

        type_name: str = Field(description="The type's name, as its `struct` or `enum` line spells it.")
        original: str = Field(
            description="The `#[derive(..)]` line through the type's declaration line, copied "
            "verbatim from the file. Both halves are required: the derive is what changes, and the "
            "declaration is what makes the capture unique."
        )
        removed: list[str] = Field(
            default_factory=list,
            description="Derives that move behind `cfg_attr(not(feature))` because the harness "
            "supplies them. Empty when the type only needs to gain one.",
        )
        added: list[str] = Field(
            default_factory=list,
            description="Derives to add under the feature. `Copy` in practice.",
        )

    path: str = Field(
        description="The file defining these types, relative to the workspace root. One call per "
        "file: a cascade that crosses files is one call each, and rustc will tell you."
    )
    swaps: list[Swap] = Field(
        description="Every type in this file the swap needs, including the ones dragged in by "
        "`Copy`. Recorded as one edit because they do not compile apart."
    )
    why: str = Field(
        description="What the author cannot prove without this, and which property class the "
        "swap serves. It goes to the judge, which is what decides whether the rules still mean "
        "anything afterwards."
    )

    @override
    async def run(self) -> Command | str:
        if not self.why.strip():
            return (
                "A non-empty `why` is required. This kind changes what the program's own state "
                "reads and writes are, so an unexplained one leaves a verdict nobody can account "
                "for."
            )
        if not self.swaps:
            return "`swaps` is empty. Name at least the type whose derives are in the way."
        with self.tool_deps() as target:
            match target.pristine_source(self.path):
                case NotInWorkdir():
                    return (
                        f"{self.path} resolves outside this project. Swap derives on the program's "
                        f"own types, with a path relative to the workspace root."
                    )
                case NotProjectSource(directory=directory):
                    return (
                        f"{self.path} is under `{directory}`, which is not this project's source. "
                        f"A type you cannot edit is a type whose derives you cannot swap — if the "
                        f"code in the way is a dependency's, refuse and say so."
                    )
                case Path() as resolved:
                    pass
            if not resolved.is_file():
                return f"{self.path} is not a file in this project."
            source = resolved.read_text()
            record = DeriveSwap(
                path=self.path,
                swaps=tuple(
                    SwappedDerive(
                        type_name=sw.type_name,
                        original=sw.original,
                        removed=tuple(sw.removed),
                        added=tuple(sw.added),
                    )
                    for sw in self.swaps
                ),
                why=self.why,
                feature=self.state["feature"],
            )
            for sw in record.swaps:
                if not sw.removed and not sw.added:
                    return (
                        f"The swap for {sw.type_name} removes nothing and adds nothing, so it "
                        f"would change no code. Say what it needs, or leave the type out."
                    )
            if any(m.edit_id == record.edit_id for m in _held(self.state)):
                return "You have already recorded exactly that swap."
            if (prior := _derive_swap_of(self.state, self.path)) is not None:
                return (
                    f"This unit already swaps derives in {self.path} ({prior.edit_id}). A cascade "
                    f"has to be one record — `drop_munge` that one and record a single swap "
                    f"covering every type."
                )
            match apply_derive_swap(source, record):
                case Munged(line=line):
                    pass
                case DeriveNotFound(type_name=name, missing=missing, present=present):
                    return (
                        f"{name} does not derive {', '.join(missing)}. A swap that removes nothing "
                        f"is a no-op the build will not report. It derives: "
                        f"{', '.join(present)}."
                    )
                case DeriveAttributeUnreadable(type_name=name, why=why):
                    return (
                        f"The captured text for {name} cannot be rewritten: {why}. Copy the "
                        f"`#[derive(..)]` line and the declaration line beneath it, exactly as the "
                        f"file has them."
                    )
                case refusal:
                    return (
                        f"That swap cannot be applied to {self.path} as it stands ({refusal}). "
                        f"Re-read the file — `original` must match it byte for byte."
                    )
        return Command(
            update={
                "proposed": [record],
                "reviewed_digest": None,
                "messages": [
                    ToolMessage(
                        tool_call_id=self.tool_call_id,
                        content=(
                            f"Swapped derives in {self.path}:{line} ({record.subject}), gated on "
                            f"`{record.feature}`. The deployed build still derives everything it "
                            f"did. Tell the author in `how_to_apply` which impls they now owe and "
                            f"that a rule reading state back needs a shared value rather than a "
                            f"fresh one per call — nothing else will catch either."
                        ),
                    )
                ],
            }
        )


def _derive_swap_of(state: EditorStateExtra, path: str) -> DeriveSwap | None:
    """This unit's existing swap in ``path``, if it has one."""
    for m in _held(state):
        if isinstance(m, DeriveSwap) and m.path == path:
            return m
    return None


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

    A first-class outcome, not a failure. Refuse when the change is outside the six kinds, when it
    would change what the program *does* rather than how it is arranged, when the code in the way
    belongs to a dependency, or when nothing in the charter would honestly solve the stated problem.
    Name the kind of change it would have taken — the author turns that into a recorded skip, and a
    skip naming a missing kind is how the vocabulary earns its next entry.
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
                ExtractFunction.bind(target).as_tool("extract_function"),
                SwapDerive.bind(target).as_tool("swap_derive"),
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
