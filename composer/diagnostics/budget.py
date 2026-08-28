from typing import Iterator, Callable, Any, Mapping, Never, Protocol, Literal, TypedDict, LiteralString
from typing_extensions import TypeVar, ReadOnly
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import time

from langgraph.graph import MessagesState
from graphcore.graph import StateMonitor, MonitorReturn
from langchain_core.messages import HumanMessage, AnyMessage
from .timing import RunSummary, get_run_summary_or_none

StateVar = TypeVar("StateVar", default=MessagesState, bound=MessagesState)

# The fraction of a budget at which "budget pressure" begins: budget_monitor's
# warning fires and budget_pressure() flips true. One global so every accessor
# agrees on where the wrap-up window starts.
BUDGET_PRESSURE_THRESHOLD = 0.8


class BudgetExceeded(Exception):
    """Hard budget stop, raised cooperatively (from a monitor, between agent
    turns) once the active budget is blown. Only monitors given an
    ``on_overbudget`` callback raise; the workflow that launched the agent
    catches this and converts it into its give-up result."""


class BudgetPressureAbort(Exception):
    """Raised by ``pressure_abort_monitor`` to terminate an auxiliary agent
    (e.g. a feedback judge) whose output is worthless once the main agent is
    in its wrap-up window. Caught by the tool that launched the agent."""

type ConstraintType = Literal["time", "token"]

class _ConstraintWarnings(TypedDict):
    time: ReadOnly[str]
    token: ReadOnly[str]

class ResourceConstraint(Protocol):
    @property
    def sort(self) -> ConstraintType:
        ...

    def overbudget(self) -> bool:
        ...

    def pressured(self, threshold: float = BUDGET_PRESSURE_THRESHOLD) -> bool:
        ...

@dataclass
class BudgetCounter:
    """One node of the caps-over-pool scheme. Leaf counters are per-phase
    *caps* whose ``parent`` is the run's shared *pool* (the real budget);
    ``token_cost_budget`` creates parentless one-off counters. Cost accrues
    up the chain, and both the hard stop and the pressure window trip on
    whichever level is tighter — a phase can only starve later phases up to
    its cap, while unspent phase money never leaves the pool (rollover is
    automatic, not an explicit transfer)."""
    total_budget: float
    curr_cost: float
    parent: "BudgetCounter | None" = None

    def overbudget(self) -> bool:
        if self.curr_cost > self.total_budget:
            return True
        return self.parent.overbudget() if self.parent is not None else False

    def pressured(self, threshold: float = BUDGET_PRESSURE_THRESHOLD) -> bool:
        if self.curr_cost >= self.total_budget * threshold:
            return True
        return self.parent.pressured(threshold) if self.parent is not None else False

    @property
    def sort(self) -> ConstraintType:
        return "token"

_budget_accumulator = ContextVar[None | BudgetCounter]("_budget_accumulator", default=None)

_cost_centers = ContextVar[None | dict[str, BudgetCounter]]("_cost_centers", default=None)

# The *name* of the innermost named budget scope. Deliberately tracked separately from
# the counters — and unconditionally, budget or no budget — so telemetry (the thread
# logger stamps it into ThreadMeta.cost_center) can attribute work to phases on
# unbudgeted runs too.
_cost_center_name = ContextVar[str | None]("_cost_center_name", default=None)

_time_budget = ContextVar[float | None]("_time_budget", default=None)

@dataclass
class _TimeCounter:
    budget: float

    summ: RunSummary

    def overbudget(self) -> bool:
        return self.summ.total_wall_s() > self.budget

    def pressured(self, threshold: float = BUDGET_PRESSURE_THRESHOLD) -> bool:
        return self.summ.total_wall_s() > self.budget * threshold

    @property
    def sort(self) -> ConstraintType:
        return "time"


def _get_constraints() -> list[ResourceConstraint]:
    to_ret : list[ResourceConstraint] = []
    if (b := _budget_accumulator.get()) is not None:
        to_ret.append(b)
    if (t_b := _time_budget.get()) is not None and \
        (rs := get_run_summary_or_none()) is not None:
        to_ret.append(_TimeCounter(t_b, rs))
    return to_ret

def current_cost_center() -> str | None:
    """The name of the innermost named budget scope, or ``None`` outside any (the run
    pool, or code that runs before/after the pipeline). Valid regardless of whether a
    budget is installed."""
    return _cost_center_name.get()

DEFAULT_RESOURCE_PRESSURE_MESSAGE = """
<system-alert>
You have almost exceeded the {resource} allotted for this task.

Finish your task in as orderly a fashion as possible; partial/incomplete results are better
than going over budget.
</system-alert>
"""

DEFAULT_BUDGET_PRESSURE_MESSAGE = DEFAULT_RESOURCE_PRESSURE_MESSAGE.format(resource="token cost budget")

DEFAULT_TIME_PRESSURE_MESSAGE = DEFAULT_RESOURCE_PRESSURE_MESSAGE.format(resource="time")

_DEFAULT_WARNINGS : _ConstraintWarnings = {
    "time": DEFAULT_TIME_PRESSURE_MESSAGE,
    "token": DEFAULT_BUDGET_PRESSURE_MESSAGE
}

class _WarningState(TypedDict):
    time: bool
    token: bool

@contextmanager
def time_budget(
    total: float
) -> Iterator[None]:
    prev = _time_budget.get()
    if prev is not None:
        raise ValueError("Timer already set, nested timings not supported")
    prev_tok = _time_budget.set(total)
    try:
        yield
    finally:
        _time_budget.reset(prev_tok)

@contextmanager
def total_budget(
    total: float,
    caps: Mapping[str, float]
) -> Iterator[None]:
    """Install the run's budget: ``total`` is the pool (the real bound on
    spend) and ``caps`` are per-phase ceilings. Caps need not sum to the
    pool — they only bound how much a single phase may hog, so each can be
    generous; whatever a phase doesn't spend simply remains in the pool for
    later phases."""
    curr = _cost_centers.get()
    if curr is not None:
        raise RuntimeError("Budget already installed, cannot overwrite existing.")
    pool = BudgetCounter(total_budget=total, curr_cost=0.0)
    prev = _cost_centers.set({
        k: BudgetCounter(total_budget=v, curr_cost=0.0, parent=pool) for (k, v) in caps.items()
    })
    # Work running outside any named center (e.g. the report phase) accrues
    # to — and feels pressure from — the pool directly.
    prev_accum = _budget_accumulator.set(pool)
    try:
        yield
    finally:
        _budget_accumulator.reset(prev_accum)
        _cost_centers.reset(prev)

@contextmanager
def named_budget(
    nm: str
) -> Iterator[None]:
    if (res := _cost_centers.get()) is None:
        raise RuntimeError("No costs installed")
    if nm not in res:
        raise RuntimeError(f"Named budget item not known: {nm}")
    prev = _budget_accumulator.set(res[nm])
    name_tok = _cost_center_name.set(nm)
    try:
        yield
    finally:
        _cost_center_name.reset(name_tok)
        _budget_accumulator.reset(prev)

@contextmanager
def named_budget_or_nop(
    nm: str
) -> Iterator[None]:
    if (_cost_centers.get()) is None:
        # No budget installed: no counters to bind, but still stamp the cost-center
        # name — phase attribution is telemetry, not billing. (A @contextmanager
        # generator must yield exactly once even on this path.)
        name_tok = _cost_center_name.set(nm)
        try:
            yield
        finally:
            _cost_center_name.reset(name_tok)
        return
    with named_budget(nm):
        yield

@contextmanager
def token_cost_budget(
    total_cost: float,
) -> Iterator[BudgetCounter]:
    """Open a one-off cost budget, yielding the counter it accrues into.

    Yielding the counter rather than ``None`` so a caller can *read* what was spent, not only trip
    on it: a producer script that reports its own bill needs the same number the hard stop uses, and
    reconstructing it on the side is how two figures for one spend come about. The counter stays
    live after the block exits, which is what makes it reportable.

    Safe across ``asyncio.gather``: a spawned task copies the context *mapping*, so every copy still
    points at this one counter object and its accruals land here."""
    if _budget_accumulator.get() is not None:
        raise RuntimeError("Nested budgets not supported")
    accum = BudgetCounter(total_budget=total_cost, curr_cost=0.0)
    prev = _budget_accumulator.set(accum)
    try:
        yield accum
    finally:
        _budget_accumulator.reset(prev)

def accumulate_cost(
    cost: float
):
    # Accrue up the chain: the active center and (through parent) the pool.
    accum = _budget_accumulator.get()
    while accum is not None:
        accum.curr_cost += cost
        accum = accum.parent

def _none_if_empty[
    T: list[AnyMessage] | dict[str, Any]
](
    s: T
) -> T | None:
    if not s:
        return None
    return s

def budget_monitor(
    *,
    warn_threshold: float = BUDGET_PRESSURE_THRESHOLD,
    warning_message: str | Callable[[StateVar, ConstraintType], str] | None = None,
    state_transformer: Callable[[StateVar, ConstraintType], dict[str, Any]] | None = None,
    on_overbudget: Callable[[ConstraintType], None] | None = None
) -> StateMonitor[StateVar]:
    accum = _get_constraints()
    if len(accum) == 0:
        return lambda _ign: (None, None)
    warned : _WarningState = {
        "time": False,
        "token": False
    }
    def monitor(
        curr_state: StateVar
    ) -> MonitorReturn:
        to_ret : list[AnyMessage] = []
        upd : dict[str, Any] = {}
        for acc in accum:
            if acc.overbudget() and on_overbudget is not None:
                on_overbudget(acc.sort)
            if not acc.pressured(warn_threshold) or warned[acc.sort]:
                continue
            warned[acc.sort] = True
            
            if warning_message is None:
                msg = _DEFAULT_WARNINGS[acc.sort]
            elif isinstance(warning_message, str):
                msg = warning_message
            else:
                msg = warning_message(curr_state, acc.sort)
            to_ret.append(HumanMessage(msg))
            if state_transformer is not None:
                upd.update(state_transformer(curr_state, acc.sort))
        return _none_if_empty(to_ret), _none_if_empty(upd)
    return monitor

def constraint_sort_to_noun(s: ConstraintType) -> LiteralString:
    match s:
        case "time":
            return "Time"
        case "token":
            return "Token cost"

def raise_budget_exceeded(sort: ConstraintType) -> Never:
    """``on_overbudget`` callback for agents that opt into the hard stop."""
    raise BudgetExceeded(
        f"{constraint_sort_to_noun(sort)} budget exhausted; the agent was cooperatively terminated."
    )


def budget_pressure() -> bool:
    """Whether the active budget is inside its wrap-up window: accrued cost at
    or past ``BUDGET_PRESSURE_THRESHOLD`` of the phase cap *or* of the run
    pool, whichever trips first. False when no budget is installed. Use this
    to skip launching work that would only be told to immediately pack it in
    (e.g. further property-extraction rounds)."""
    res = _get_constraints()
    return any(r.pressured() for r in res)


def pressure_abort_monitor() -> StateMonitor[MessagesState]:
    """Monitor for auxiliary agents (feedback judges) that should not outlive
    the main agent's wrap-up window: raises ``BudgetPressureAbort`` between
    turns once budget pressure sets in. The tool that launched the agent
    catches the exception and returns a canned "terminated for budget"
    result. Reads the budget at call time, so it can be attached to a graph
    compiled outside any budget scope."""
    def monitor(_curr_state: StateVar) -> MonitorReturn:
        if budget_pressure():
            raise BudgetPressureAbort()
        return (None, None)
    return monitor
