from typing import Iterator, Callable, Any, Mapping
from typing_extensions import TypeVar
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from langgraph.graph import MessagesState
from graphcore.graph import StateMonitor, MonitorReturn
from langchain.messages import HumanMessage

StateVar = TypeVar("StateVar", default=MessagesState, bound=MessagesState)

@dataclass
class BudgetCounter:
    total_budget: float
    curr_cost: float

    def overbudget(self) -> bool:
        return self.curr_cost > self.total_budget

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
    costs: Mapping[str, float]
) -> Iterator[None]:
    curr = _cost_centers.get()
    if curr is not None:
        raise RuntimeError("Not good")
    prev = _cost_centers.set({
        k: BudgetCounter(total_budget=v, curr_cost=0.0) for (k, v) in costs.items()
    })
    try:
        yield None
    finally:
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
    accum = _budget_accumulator.get()
    if accum is None:
        return
    accum.curr_cost += cost

def budget_monitor(
    *,
    warn_threshold: float = 0.8,
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
        if warned or not (accum.curr_cost >= accum.total_budget * warn_threshold):
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
