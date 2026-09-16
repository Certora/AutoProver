"""Parsing a prover run's treeView reports into per-rule results.

Chain-neutral: every Prover CLI emits the same treeView shape, so one parser serves them all. What
differs per chain is which frames of a counterexample's call trace are worth showing — a trace is
mostly runtime the property is not about, and each chain buries the counterexample under its own
scaffolding. That is what :class:`TraceShape` is for.
"""

from typing import Optional, Callable, TypeVar
from typing_extensions import Iterable
from dataclasses import dataclass
from pathlib import Path
import re
import json

from pydantic import Field, BaseModel, ValidationError

from composer.certora_env import ProverApp
from composer.prover.ptypes import (
    Counterexample,
    RulePath,
    RuleResult,
    SourceSpan,
    StatusCodes,
)

T = TypeVar('T')
R = TypeVar('R')

class RuleNotificationMessages(BaseModel):
    severity: str
    message: str

class RuleNodeModel(BaseModel):
    name: str = Field(description="The name of the node")
    output: list[str]
    children: list["RuleNodeModel"]
    status: Optional[str] = Field(description="The smt status")
    nodeType: str
    errors: list[RuleNotificationMessages]
    LiveCheckInfo: str | None = Field(default=None)


class TreeViewStatus(BaseModel):
    rules: list[RuleNodeModel]


class SarifArgs(BaseModel):
    value: str
    # ignoring the other fields


class MessageModel(BaseModel):
    text: str
    arguments: list[SarifArgs]


class CallTraceModel(BaseModel):
    message: MessageModel
    childrenList: list["CallTraceModel"]


@dataclass(frozen=True)
class TraceShape:
    """Which call-trace frames a chain's counterexamples are worth rendering.

    Frames are matched by :func:`_frame_name`. ``dropped`` removes a frame *and everything under
    it*, which is only safe where nothing interesting can nest inside it — a per-chain fact, see
    :data:`SOLANA_TRACE`. ``elided`` keeps the frame and replaces its subtree with a count.
    """

    dropped: frozenset[str] = frozenset()
    elided: frozenset[str] = frozenset()


#: Frames the prover emits on every chain and no counterexample needs.
_GENERIC_NOISE = frozenset({"Setup", "Global State"})

#: Nothing chain-specific claimed. What a chain's own setup frame is called is a thing to measure
#: from one of its counterexamples rather than guess, so a chain gets this until one exists.
GENERIC_TRACE = TraceShape(dropped=_GENERIC_NOISE)

EVM_TRACE = TraceShape(
    dropped=_GENERIC_NOISE | {"Evaluate branch condition", "unknown loop source code"},
)

#: Solana's traces are dominated by account materialization: on one measured counterexample, 128 of
#: 135 frames were the allocator and nondet-size choices under ``cvlr_deserialize_nondet_accounts``.
#: That subtree is elided rather than dropped, so a reader can tell setup happened and how much of
#: it was hidden.
#:
#: Does not take EVM's two extra drops. A Solana loop-unwinding violation nests both its
#: ``Assert 'loop has terminated' failed`` and the per-iteration structure that says how far the
#: loop got under ``unknown loop source code``, so dropping that frame renders a trace that states
#: no failure at all.
SOLANA_TRACE = TraceShape(
    dropped=_GENERIC_NOISE | {"__rust_alloc", "CVT_alloc_slice"},
    elided=frozenset({"cvlr_solana::layout::cvlr_deserialize_nondet_accounts(...)"}),
)


def trace_shape(app: ProverApp) -> TraceShape:
    """The rendering shape for a chain's traces. Exhaustive over :data:`ProverApp` on purpose: a new
    chain should have to state what its traces look like rather than inherit another chain's."""
    match app:
        case "evm":
            return EVM_TRACE
        case "solana":
            return SOLANA_TRACE
        case "soroban":
            return GENERIC_TRACE


def _flat_yield(curr: Iterable[T], gen: Callable[[T], Iterable[R]]) -> Iterable[R]:
    for t in curr:
        for to_yield in gen(t):
            yield to_yield

def _to_status_string(s: str | None) -> StatusCodes:
    if s is None:
        return "ERROR"
    match s:
        case "VIOLATED" | "VERIFIED" | "TIMEOUT" | "SANITY_FAILED" | "SKIPPED":
            return s
        case _:
            return "ERROR"


def flatten_tree_view_root(
    context: Path, r: RuleNodeModel, shape: TraceShape
) -> Iterable[RuleResult]:
    assert r.nodeType == "ROOT"
    return flatten_tree_view(context, r, RulePath(rule=r.name), shape, None)

def _collect_child_errors(
    r: RuleNodeModel, err_messages: set[str], sev_filter: Callable[[str], bool]
):
    if _to_status_string(r.status) != "ERROR":
        return
    for m in r.errors:
        if not sev_filter(m.severity):
            continue
        err_messages.add(m.message)
    for c in r.children:
        _collect_child_errors(c, err_messages, sev_filter)

def flatten_tree_view(
    context: Path,
    r: RuleNodeModel,
    path: RulePath,
    shape: TraceShape,
    parent_type: str | None = None,
) -> Iterable[RuleResult]:
    stat = _to_status_string(r.status)
    effective_path = path
    if r.nodeType == "METHOD_INSTANTIATION":
        effective_path = effective_path.copy(method=r.name)
    elif r.nodeType == "CONTRACT":
        effective_path = effective_path.copy(contract = r.name)
    elif r.nodeType == "INVARIANT_SUBCHECK":
        if "constructor" in r.name:
            effective_path = effective_path.copy(method="constructor")
    elif r.nodeType == "INDUCTION_STEPS" and parent_type is not None and parent_type == "CUSTOM_INDUCTION_STEP":
        # Handle nodes with format "ContractName.methodSignature"
        # Set both contract and method fields to match how target paths are constructed
        if "." in r.name:
            contract_name, _ = r.name.split(".", 1)
            effective_path = effective_path.copy(contract=contract_name, method=r.name)
        else:
            effective_path = effective_path.copy(method=r.name)

    if stat == "ERROR":
        messages : set[str] = set()
        _collect_child_errors(r, messages, lambda sev: sev == "error")
        if all(
            "timed-out" in err or ("did not run: BlockingCoroutine was cancelled") in err for err in messages
        ):
            # time out masquerading as an error...    
            stat = "TIMEOUT"
        else:
            return [RuleResult(
                path=effective_path,
                counterexample=None,
                status=stat,
                error_messages=list(messages)
            )]
    elif stat == "SKIPPED":
        warning_message = [
            i.message for i in r.errors if i.severity == "error" or i.severity == "warning"
        ]
        return [
            RuleResult(
                path=effective_path,
                counterexample=None,
                status=stat,
                error_messages=warning_message
            )
        ]
    if stat == "VERIFIED":
        non_sanity_children = any([ c.nodeType != "SANITY" for c in r.children ])
        if non_sanity_children:
            return _flat_yield(
                r.children,
                lambda c: flatten_tree_view(context, c, effective_path, shape, r.nodeType),
            )
        else:
            return [RuleResult(
                path=effective_path,
                counterexample=None,
                status=stat
            )]

    if stat == "TIMEOUT":
        if all(tc.nodeType == "SANITY" for tc in r.children):
            return [RuleResult(path=effective_path, counterexample=None,status=stat, live_check_info=r.LiveCheckInfo)]
    assert stat == "TIMEOUT" or stat == "VIOLATED" or stat == "SANITY_FAILED"
    violated_assert_children = any([ c.nodeType == "VIOLATED_ASSERT" for c in r.children])
    if violated_assert_children:
        assert stat == "VIOLATED" and len(r.output) > 0
        output_file = r.output[0]
        dump_model = json.loads((context / output_file).read_text())
        assert isinstance(dump_model, dict)
        return [RuleResult(
            path = effective_path,
            counterexample=counterexample(dump_model, shape),
            status=stat
        )]
    if r.nodeType == "SANITY":
        assert stat == "SANITY_FAILED"
        return [RuleResult(
            path=effective_path,
            counterexample=None,
            status=stat
        )]
    return _flat_yield(
        r.children, lambda c: flatten_tree_view(context, c, effective_path, shape, r.nodeType)
    )

class NoTreeViewResultError(RuntimeError):
    def __init__(self, where: Path):
        super().__init__(f"No tree views found in {where}")

class MalformedTreeVew(RuntimeError):
    def __init__(self, wrapped: ValidationError):
        super().__init__(wrapped)


def get_final_treeview(s: Path) -> tuple[TreeViewStatus, Path]:
    tree_view_dir = s / "Reports" / "treeView"
    status_files = tree_view_dir.glob("treeViewStatus_*.json")

    search_patt = re.compile(
        r'treeViewStatus_(\d+).json'
    )

    max_n = -1
    for p in status_files:
        if p.name is None:
            continue
        match = search_patt.match(p.name)
        if match is None:
            continue
        index = int(match.group(1))
        if index < max_n:
            continue
        max_n = index
    if max_n == -1:
        raise NoTreeViewResultError(s)

    final_status = s / "Reports" / "treeView" / f"treeViewStatus_{max_n}.json"
    with open(final_status, "r") as result_file:
        run_status = json.load(result_file)
    try:
        loaded_data = TreeViewStatus.model_validate(run_status)
        return (loaded_data, tree_view_dir)
    except ValidationError as e:
        raise MalformedTreeVew(e)


def read_and_format_run_result(s: Path, app: ProverApp) -> dict[str, RuleResult] | str:
    """Every rule in a finished run's final treeView, keyed by rule name.

    ``app`` decides only how counterexample traces are rendered (:func:`trace_shape`); everything
    else about the parse is shared."""
    loaded_data : TreeViewStatus
    tree_view_dir: Path
    try:
        (loaded_data, tree_view_dir) = get_final_treeview(s)
    except NoTreeViewResultError:
        return "Certora prover returned no results: this is likely a bug"
    except MalformedTreeVew:
        return "Certora prover returned malformed tree view data: this is likely a bug"

    shape = trace_shape(app)
    to_ret: dict[str, RuleResult] = {}
    for r in _flat_yield(
        loaded_data.rules, lambda r: flatten_tree_view_root(tree_view_dir, r, shape)
    ):
        to_ret[r.name] = r
    return to_ret

def _rendered_message(m: MessageModel) -> str:
    """The frame's message with this counterexample's values substituted into it."""
    text = m.text
    for i, arg in enumerate(m.arguments):
        text = text.replace(f"{{{i}}}", arg.value)
    return text


def _frame_name(text: str) -> str:
    """A frame's identity, with whatever value it held stripped off.

    The prover formats a value-bearing frame as ``name: '{0}'`` — the name is the frame, the
    argument is the value it took in this counterexample. Matching a :class:`TraceShape` against the
    whole text would make every single allocation its own frame."""
    head, sep, _ = text.partition(": '")
    return head if sep else text


def _descendants(node: CallTraceModel) -> int:
    return sum(1 + _descendants(c) for c in node.childrenList)


def calltrace_to_xml(node: CallTraceModel, shape: TraceShape) -> str:
    """Render one counterexample's call trace as the XML an analyzer reads.

    ``shape`` is deliberately not defaulted: a default silently renders a trace with the wrong
    chain's shape."""
    xml_parts = [f"<message>{_rendered_message(node.message)}</message>"]

    for child in node.childrenList:
        name = _frame_name(child.message.text)
        if name in shape.dropped:
            continue
        if name in shape.elided:
            xml_parts.append(
                f"<child><message>{_rendered_message(child.message)}</message>"
                f"<elided>{_descendants(child)} frames of setup</elided></child>"
            )
            continue
        xml_parts.append(f"<child>{calltrace_to_xml(child, shape)}</child>")

    return "".join(xml_parts)


def counterexample(dump: dict[str, object], shape: TraceShape) -> Counterexample | None:
    """One violated rule's counterexample, or None when its output carries no call trace.

    ``assertMessage`` matters twice over: on a Solana loop-unwinding violation it is the only
    statement of what failed — the trace just shows the loop getting two iterations in and stopping
    — and it is what separates a violation that found a bug from one that ran into an analysis
    bound (:func:`~composer.prover.ptypes.classify_violation`).
    """
    if "callTrace" not in dump:
        return None
    assertion: str | None = None
    match dump.get("assertMessage"):
        case str(message) if message:
            assertion = message
        case _:
            pass
    source: SourceSpan | None = None
    match dump.get("jumpToDefinition"):
        case {"file": str(file), "start": {"line": int(line)}}:
            source = SourceSpan(file=file, line=line)
        case _:
            pass
    return Counterexample(
        trace=calltrace_to_xml(CallTraceModel.model_validate(dump["callTrace"]), shape),
        assertion=assertion,
        source=source,
    )
