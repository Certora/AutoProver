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
import logging

from pydantic import Field, BaseModel, ValidationError

from composer.certora_env import ProverApp
from composer.prover.ptypes import (
    AnalysisErrorKind,
    AssertOrigin,
    Counterexample,
    GeneratedAssertion,
    GeneratedKind,
    ProverMessage,
    RulePath,
    RuleResult,
    SourceSpan,
    StatusCodes,
    UnclassifiedAssertion,
    Uncoded,
    UnknownCode,
    UnknownKind,
    UserAssertion,
)

_log = logging.getLogger(__name__)

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


#: The prover writes a frame's message as a format template: literal text with ``'{n}'`` standing in
#: for the n-th of ``arguments`` (``report/calltrace/sarif/Sarif.kt`` in EVMVerifier, whose
#: ``toString`` emits exactly this quoted, zero-based form).
_PLACEHOLDER = re.compile(r"'\{(\d+)\}'")


class MessageModel(BaseModel):
    text: str
    arguments: list[SarifArgs]

    @property
    def frame(self) -> str:
        """This message's identity, shared by every frame the prover emits from the same site.

        A counterexample's values live in ``arguments``, never in ``text``, so the template already
        *is* the identity: all 33 ``__rust_alloc`` frames of one measured trace carry the same
        ``text`` and differ only in the argument they took."""
        return _PLACEHOLDER.sub("{}", self.text)

    def render(self) -> str:
        """The template with this counterexample's values substituted for its placeholders."""
        return _PLACEHOLDER.sub(
            lambda placeholder: f"'{self.arguments[int(placeholder.group(1))].value}'", self.text
        )


class CallTraceModel(BaseModel):
    message: MessageModel
    childrenList: list["CallTraceModel"]


@dataclass(frozen=True)
class TraceShape:
    """Which call-trace frames a chain's counterexamples are worth rendering.

    Frames are named by :attr:`MessageModel.frame`, so a value-bearing frame is written the way the
    prover's format string writes it, with ``{}`` where its value goes. ``dropped`` removes a frame
    *and everything under it*, which is only safe where nothing interesting can nest inside it — a
    per-chain fact, see :data:`SOLANA_TRACE`. ``elided`` keeps the frame and replaces its subtree
    with a count.
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
#:
#: rustc 1.89 and later name the allocator shim ``__rustc::__rust_alloc``, so both spellings are
#: dropped.
SOLANA_TRACE = TraceShape(
    dropped=_GENERIC_NOISE | {"__rust_alloc: {}", "__rustc::__rust_alloc: {}", "CVT_alloc_slice: {}"},
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
                messages=[prover_message(m) for m in sorted(messages)]
            )]
    elif stat == "SKIPPED":
        warning_message = [
            prover_message(i.message)
            for i in r.errors
            if i.severity == "error" or i.severity == "warning"
        ]
        return [
            RuleResult(
                path=effective_path,
                counterexample=None,
                status=stat,
                messages=warning_message
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
    if not r.children:
        # A rule the Prover decides by static analysis alone arrives as a bare root with no
        # subchecks. Recursing into its children would drop its verdict, and a missing verdict
        # reads as nothing rather than as a failure.
        return [RuleResult(
            path=effective_path,
            counterexample=None,
            status=stat
        )]
    return _flat_yield(
        r.children, lambda c: flatten_tree_view(context, c, effective_path, shape, r.nodeType)
    )

#: Where a job's alert report lives, relative to its results directory.
ALERT_REPORT = Path("Reports") / "alertReport.json"

#: ``type`` of the alert the prover writes when it hits the global timeout.
OUT_OF_RESOURCES = "Out of Resources"
#: ``type`` of the alert the prover writes when Solana rule encoding fails.
SOLANA_ENCODING = "Solana Encoding"


@dataclass(frozen=True)
class ErrorAlert:
    """One ``ERROR`` entry from an alert report.

    ``text`` is ``hint`` when that is a non-empty string, otherwise ``message``.
    ``type`` is the report's ``type``, or ``""`` when the field is absent.
    """

    type: str
    text: str


def _nonempty_str(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def error_alerts(text: str) -> tuple[ErrorAlert, ...]:
    """The ``ERROR`` entries of an alert-report body, in file order.

    ``hint`` when it is a non-empty string, otherwise ``message``. On an internal
    prover error ``message`` is a generic support sentence and ``hint`` is the
    only field that names the crash. An entry with neither is dropped.

    Empty when ``text`` is not the JSON array the report is. That is not a claim
    that the job produced verdicts.
    """
    try:
        report = json.loads(text)
    except ValueError:
        return ()
    if not isinstance(report, list):
        return ()
    found: list[ErrorAlert] = []
    for alert in report:
        if not isinstance(alert, dict) or alert.get("severity") != "ERROR":
            continue
        chosen = _nonempty_str(alert.get("hint")) or _nonempty_str(alert.get("message"))
        if chosen is None:
            continue
        raw_type = alert.get("type")
        found.append(ErrorAlert(raw_type if isinstance(raw_type, str) else "", chosen))
    return tuple(found)


@dataclass(frozen=True)
class AlertReport:
    """The messages of a job's alert report, in the Prover's words and order.

    Empty when the job raised no alerts. The alerts do not name their rule, so a
    message repeats once per rule that raised it.
    """

    messages: tuple[str, ...] = ()


@dataclass(frozen=True)
class UnreadableAlertReport:
    """The alert report could not be fetched, or is not a JSON list of alerts."""


@dataclass(frozen=True)
class FetchedResults:
    """A finished run's results directory, and its alert report."""

    root: Path
    #: The prover's queue-free run time, when it is known.
    runtime_ms: int | None
    alerts: AlertReport | UnreadableAlertReport
    #: The job hit the time limit, so rules it had not finished are ``TIMEOUT``.
    halted: bool = False


def parse_alert_report(text: str) -> AlertReport | UnreadableAlertReport:
    """The alerts named in ``text``.

    ``text`` that is not a JSON list of alerts is :class:`UnreadableAlertReport`: that
    is not an empty list.
    """
    try:
        alerts = json.loads(text)
    except ValueError:
        return UnreadableAlertReport()
    if not isinstance(alerts, list):
        return UnreadableAlertReport()
    messages = (alert.get("message") if isinstance(alert, dict) else None for alert in alerts)
    return AlertReport(tuple(m for m in messages if isinstance(m, str)))


def read_alert_report(results_root: Path) -> AlertReport | UnreadableAlertReport:
    """The alert report under ``results_root`` (:func:`parse_alert_report`).

    An absent report is an empty :class:`AlertReport`. A report that cannot be read
    is :class:`UnreadableAlertReport`.
    """
    path = results_root / ALERT_REPORT
    try:
        text = path.read_text()
    except FileNotFoundError:
        return AlertReport()
    except OSError:
        return UnreadableAlertReport()
    return parse_alert_report(text)


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


#: Statuses that are verdicts. A node of a ``HALTED`` job that the Prover cut off is still
#: ``RUNNING``, and a timeout can be written as an ``ERROR``; both read as ``TIMEOUT``.
_FINISHED = frozenset({"VERIFIED", "VIOLATED", "SANITY_FAILED", "SKIPPED"})


def unfinished_statistics_files(tree: TreeViewStatus) -> frozenset[str]:
    """The statistics files (``LiveCheckInfo``) of every node without a verdict."""
    found: set[str] = set()

    def walk(node: RuleNodeModel) -> None:
        if node.status not in _FINISHED and node.LiveCheckInfo is not None:
            found.add(node.LiveCheckInfo)
        for child in node.children:
            walk(child)

    for rule in tree.rules:
        walk(rule)
    return frozenset(found)


@dataclass(frozen=True)
class Hotspot:
    function: str
    #: The function's share of the rule's branching or nonlinear operations, in percent.
    share: int


@dataclass(frozen=True)
class RuleStatistics:
    """Where an unfinished rule stood, from the Prover's ``rule_live_statistics_*.json``."""

    #: The Prover's verification phase, with its line breaks collapsed.
    phase: str
    path_count: str | None
    branching: tuple[Hotspot, ...]
    nonlinear: tuple[Hotspot, ...]

    def render(self, top: int = 2) -> str:
        parts = [self.phase]
        if self.path_count is not None:
            parts.append(f"path count {self.path_count}")
        for what, spots in (("branching", self.branching), ("nonlinear ops", self.nonlinear)):
            if spots:
                listed = ", ".join(f"{h.function} {h.share} %" for h in spots[:top])
                parts.append(f"{what}: {listed}")
        return "; ".join(parts)


_SHARE = re.compile(r"contrib\. to (?:branching|nonlinear ops): (\d+) %")
_PHASE_PREFIX = "verification phase:"


def _hotspots(section: object) -> tuple[Hotspot, ...]:
    if not isinstance(section, dict) or not isinstance(children := section.get("children"), list):
        return ()
    found: list[Hotspot] = []
    for child in children:
        if not isinstance(child, dict):
            continue
        label, value = child.get("label"), child.get("value")
        if not isinstance(label, str) or not label.startswith("function: ") or not isinstance(value, str):
            continue
        if (share := _SHARE.search(value)) is None:
            continue
        found.append(Hotspot(label.removeprefix("function: "), int(share.group(1))))
    return tuple(sorted(found, key=lambda h: -h.share))


_SPLITS = re.compile(
    r"splits attempted \(solved or split again\): (\d+) "
    r"total weight of splits proved: (\d+) % "
    r"splitting depth reached: (\d+) "
    r"configured max\. splitting depth: (\d+)"
)


def _phase(label: str) -> str:
    detail = " ".join(label.removeprefix(_PHASE_PREFIX).split())
    if detail == "static analysis":
        return "static analysis (the rule did not reach the solver)"
    if (splits := _SPLITS.fullmatch(detail)) is not None:
        attempted, proved, depth, max_depth = splits.groups()
        return (
            f"solving (splits attempted {attempted}, {proved} % proved, "
            f"splitting depth {depth} of {max_depth})"
        )
    return detail


def parse_rule_statistics(text: str) -> RuleStatistics | None:
    """A ``rule_live_statistics_*.json`` body, or None when it has no verification phase."""
    try:
        sections = json.loads(text)
    except ValueError:
        return None
    if not isinstance(sections, list):
        return None
    by_label: dict[str, dict] = {}
    phase: str | None = None
    for section in sections:
        if not isinstance(section, dict) or not isinstance(label := section.get("label"), str):
            continue
        if label.startswith(_PHASE_PREFIX):
            phase = _phase(label)
        else:
            by_label[label] = section
    if phase is None:
        return None
    path_count = by_label.get("path count", {}).get("value")
    return RuleStatistics(
        phase=phase,
        path_count=path_count if isinstance(path_count, str) else None,
        branching=_hotspots(by_label.get("path count hotspots")),
        nonlinear=_hotspots(by_label.get("nonlinearity hotspots")),
    )


def read_rule_statistics(tree_view_dir: Path, name: str) -> RuleStatistics | None:
    """The statistics file ``name`` in ``tree_view_dir``, or None when it is absent or unreadable."""
    try:
        text = (tree_view_dir / name).read_text()
    except OSError:
        return None
    return parse_rule_statistics(text)


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


def _descendants(node: CallTraceModel) -> int:
    return sum(1 + _descendants(c) for c in node.childrenList)


def calltrace_to_xml(node: CallTraceModel, shape: TraceShape) -> str:
    """Render one counterexample's call trace as the XML an analyzer reads.

    ``shape`` is deliberately not defaulted: a default silently renders a trace with the wrong
    chain's shape."""
    xml_parts = [f"<message>{node.message.render()}</message>"]

    for child in node.childrenList:
        frame = child.message.frame
        if frame in shape.dropped:
            continue
        if frame in shape.elided:
            xml_parts.append(
                f"<child><message>{child.message.render()}</message>"
                f"<elided>{_descendants(child)} frames of setup</elided></child>"
            )
            continue
        xml_parts.append(f"<child>{calltrace_to_xml(child, shape)}</child>")

    return "".join(xml_parts)


def assert_origin(reported: object) -> AssertOrigin:
    """Where a violated assertion came from, from the ``assertOrigin`` of its rule's output.

    A Prover that predates ``assertOrigin`` does not write the key, and its assertions read as
    unclassified, the rule's own. Only a run on a Prover that writes it can tell a limit of the
    Prover from a finding.
    """
    match reported:
        case "user":
            return UserAssertion()
        case str(kind) if kind in GeneratedKind:
            return GeneratedAssertion(GeneratedKind(kind))
        case str(kind):
            _log.warning("the Prover reported a kind of generated assertion not in GeneratedKind: %s", kind)
            return GeneratedAssertion(UnknownKind(kind))
        case _:
            return UnclassifiedAssertion()


#: The code at the start of a Prover error. A Prover before EVMVerifier#8815 prefixes an encoding
#: failure with ``In <rule>: ``.
_ERROR_CODE = re.compile(r"(?:In \S+: )?\[(\d+)\]")


def prover_message(text: str) -> ProverMessage:
    """A message the Prover reported on a rule, with the ``[NNNN]`` code that starts it."""
    match _ERROR_CODE.match(text):
        case None:
            return ProverMessage(Uncoded(), text)
        case m if int(m[1]) in AnalysisErrorKind:
            return ProverMessage(AnalysisErrorKind(int(m[1])), text)
        case m:
            return ProverMessage(UnknownCode(int(m[1])), text)


def counterexample(dump: dict[str, object], shape: TraceShape) -> Counterexample | None:
    """One violated rule's counterexample, or None when its output carries no call trace.

    On a Solana loop-unwinding violation ``assertMessage`` is the only statement of what failed:
    the trace just shows the loop getting two iterations in and stopping. ``assertOrigin`` is what
    separates a violation that found a bug from one that ran into an analysis bound
    (:func:`~composer.prover.ptypes.classify_violation`).
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
        origin=assert_origin(dump.get("assertOrigin")),
        assertion=assertion,
        source=source,
    )
