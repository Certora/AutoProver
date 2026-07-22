from typing import Iterator, Callable, Any, Mapping
from typing_extensions import TypeVar
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from langgraph.graph import MessagesState
from graphcore.graph import StateMonitor, MonitorReturn
from langchain_core.messages import HumanMessage

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

_budget_accumulator = ContextVar[None | BudgetCounter]("_budget_accumulator", default=None)

_cost_centers = ContextVar[None | dict[str, BudgetCounter]]("_cost_centers", default=None)


DEFAULT_BUDGET_PRESSURE_MESSAGE = """
<system-alert>
You have almost exceeded the token cost budget allotted for this task.

Finish your task in as orderly a fashion as possible; partial/incomplete results are better
than going over budget.
</system-alert>
"""

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
        raise RuntimeError("Not good")
    pool = BudgetCounter(total_budget=total, curr_cost=0.0)
    prev = _cost_centers.set({
        k: BudgetCounter(total_budget=v, curr_cost=0.0, parent=pool) for (k, v) in caps.items()
    })
    # Work running outside any named center (e.g. the report phase) accrues
    # to — and feels pressure from — the pool directly.
    prev_accum = _budget_accumulator.set(pool)
    try:
        yield None
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
    try:
        yield
    finally:
        _budget_accumulator.reset(prev)

@contextmanager
def named_budget_or_nop(
    nm: str
) -> Iterator[None]:
    if (_cost_centers.get()) is None:
        # A @contextmanager generator must yield exactly once even on the
        # nop path — a bare return raises "generator didn't yield".
        yield
        return
    with named_budget(nm):
        yield

@contextmanager
def token_cost_budget(
    total_cost: float,
) -> Iterator[None]:
    if _budget_accumulator.get() is not None:
        raise RuntimeError("Nested budgets not supported")
    accum = BudgetCounter(total_budget=total_cost, curr_cost=0.0)
    prev = _budget_accumulator.set(accum)
    try:
        yield None
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

def budget_monitor(
    *,
    warn_threshold: float = BUDGET_PRESSURE_THRESHOLD,
    warning_message: str | Callable[[StateVar], str] | None = None,
    state_transformer: Callable[[StateVar], dict[str, Any]] | None = None,
    on_overbudget: Callable[[], None] | None = None
) -> StateMonitor[StateVar]:
    accum = _budget_accumulator.get()
    if accum is None:
        return lambda _ign: (None, None)
    warned = False
    def monitor(
        curr_state: StateVar
    ) -> MonitorReturn:
        nonlocal warned
        if accum.overbudget() and on_overbudget is not None:
            on_overbudget()
        if warned or not accum.pressured(warn_threshold):
            return (None, None)
        warned = True
        msg : str
        if warning_message is None:
            msg = DEFAULT_BUDGET_PRESSURE_MESSAGE
        elif isinstance(warning_message, str):
            msg = warning_message
        else:
            msg = warning_message(curr_state)
        
        state_upd = None
        if state_transformer is not None:
            state_upd = state_transformer(curr_state)
        return ([HumanMessage(msg)], state_upd)
    return monitor 

def overbudget() -> bool:
    res = _budget_accumulator.get()
    if res is None:
        return False
    return res.overbudget()


def raise_budget_exceeded() -> None:
    """``on_overbudget`` callback for agents that opt into the hard stop."""
    raise BudgetExceeded(
        "Token cost budget exhausted; the agent was cooperatively terminated."
    )


def budget_pressure() -> bool:
    """Whether the active budget is inside its wrap-up window: accrued cost at
    or past ``BUDGET_PRESSURE_THRESHOLD`` of the phase cap *or* of the run
    pool, whichever trips first. False when no budget is installed. Use this
    to skip launching work that would only be told to immediately pack it in
    (e.g. further property-extraction rounds)."""
    res = _budget_accumulator.get()
    if res is None:
        return False
    return res.pressured()


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
