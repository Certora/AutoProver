"""Run, execution and thread records in the store.

A **run** is the task: one autoprover invocation's identity, keyed by the run
id. An **execution** is one process that worked on it: the first one, and any
that resumed it after a suspension or a crash. Threads belong to the run and
record which execution ran them.

Two stored shapes coexist. :class:`RunMetaV1` is what every record written
before executions existed looks like: the run's own start and end, because a
run was exactly one process. :class:`RunMetaV2` carries the run's identity
only, with one :class:`ExecutionMeta` record per process under
:func:`executions_ns`. Readers never branch on the shape: :func:`as_run`
normalizes either into a :class:`Run`, treating a v1 record as a run with one
execution whose id is the run id. The writer upgrades a v1 record in place the
first time new code resumes it, preserving that execution.
"""

from typing import Literal, NotRequired, TypedDict, Protocol, AsyncIterator, Any, Callable, cast
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, UTC
from uuid import uuid4
from contextlib import asynccontextmanager
from contextvars import ContextVar

from langchain_core.runnables import RunnableConfig
from langgraph.store.base import BaseStore
from composer.core.user import user_data_ns
from composer.diagnostics.budget import current_cost_center


class _WithTimings(TypedDict):
    start_time: str
    end_time: str | None


class RunMetaV1(_WithTimings):
    """A run from before executions were a thing: the run's times ARE its one
    execution's times. Production data holds these; never written any more."""
    tags: dict[str, Any]


class RunMetaV2(TypedDict):
    """The run's identity. Its executions are separate records."""
    schema: Literal[2]
    created_at: str
    tags: dict[str, Any]


type RunMeta = RunMetaV1 | RunMetaV2
"""What a run record in the store can be. Normalize with :func:`as_run`."""

type ExecutionOutcome = Literal["completed", "failed", "awaiting_input"]


class ExecutionMeta(TypedDict):
    execution_id: str
    run_id: str
    start_time: str
    end_time: str | None
    #: The execution this one resumed, or None for the run's first.
    resumed_from: str | None
    #: None while the execution is in flight.
    outcome: ExecutionOutcome | None


class ThreadMeta(_WithTimings):
    run_id: str
    thread_id: str
    description: str
    from_tool_id: str | None

    start_checkpoint_id: str | None
    end_checkpoint_id: str | None

    #: The named budget scope this thread ran under (a `PhaseBudget` phase name), or
    #: None for work outside any named scope (the run pool / pre-pipeline). Recorded
    #: whether or not a budget was installed. NotRequired: absent on records written
    #: before cost-center tracking existed.
    cost_center: NotRequired[str | None]

    #: The execution that ran this thread segment. NotRequired: absent on records
    #: written before executions existed, when it was the run id.
    execution_id: NotRequired[str]


DEFAULT_META_NS = ("logging",)

def runs_ns(parent_ns: tuple[str, ...]) -> tuple[str, ...]:
    """Sub-namespace under ``parent_ns`` where ``RunMeta`` records live."""
    return parent_ns + ("runs",)


def threads_ns(parent_ns: tuple[str, ...]) -> tuple[str, ...]:
    """Sub-namespace under ``parent_ns`` where ``ThreadMeta`` records live."""
    return parent_ns + ("threads",)

def data_ns(parent_ns: tuple[str, ...], run_id: str) -> tuple[str, ...]:
    return parent_ns + ("run_data", run_id)

def executions_ns(parent_ns: tuple[str, ...], run_id: str) -> tuple[str, ...]:
    """Sub-namespace under ``parent_ns`` where a run's ``ExecutionMeta`` records live."""
    return parent_ns + ("executions", run_id)

def ambient_state_ns(parent_ns: tuple[str, ...], run_id: str) -> tuple[str, ...]:
    """Sub-namespace under ``parent_ns`` where a run's persisted ambient state (the
    run summary, the cost budget) lives, one record per kind of state."""
    return parent_ns + ("ambient", run_id)

def default_logging_ns(uid: str | None) -> tuple[str, ...]:
    return user_data_ns(uid) + DEFAULT_META_NS

def default_runs_ns(uid: str | None) -> tuple[str, ...]:
    return runs_ns(default_logging_ns(uid))

def default_threads_ns(uid: str | None) -> tuple[str, ...]:
    return threads_ns(default_logging_ns(uid))

def _time_string() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# The normalized view
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Execution:
    id: str
    run_id: str
    start_time: str
    end_time: str | None
    resumed_from: str | None
    outcome: ExecutionOutcome | None

    @property
    def in_flight(self) -> bool:
        return self.end_time is None


@dataclass(frozen=True)
class Run:
    """A run as readers see it, whatever shape it was stored in."""

    run_id: str
    tags: dict[str, Any]
    created_at: str
    #: Chronological; the last one is the latest.
    executions: tuple[Execution, ...]

    @property
    def latest(self) -> Execution | None:
        return self.executions[-1] if self.executions else None

    @property
    def start_time(self) -> str:
        return self.created_at

    @property
    def end_time(self) -> str | None:
        """When the run last stopped, or None while an execution is in flight."""
        latest = self.latest
        return None if latest is None else latest.end_time

    @property
    def status(self) -> str:
        latest = self.latest
        if latest is None:
            return "unknown"
        if latest.in_flight:
            return "in-progress"
        return latest.outcome or "completed"


def is_v2(meta: RunMeta) -> bool:
    return meta.get("schema") == 2


def legacy_execution(run_id: str, meta: RunMetaV1) -> Execution:
    """The one execution a pre-executions record describes: the run itself."""
    return Execution(
        id=run_id,
        run_id=run_id,
        start_time=meta["start_time"],
        end_time=meta["end_time"],
        resumed_from=None,
        outcome=None if meta["end_time"] is None else "completed",
    )


def as_run(run_id: str, meta: RunMeta, executions: Sequence[ExecutionMeta] = ()) -> Run:
    """Normalize a stored run record and its execution records (v2) or nothing
    (v1) into a :class:`Run`."""
    if is_v2(meta):
        v2 = cast(RunMetaV2, meta)
        found = sorted(
            (
                Execution(
                    id=e["execution_id"], run_id=e["run_id"], start_time=e["start_time"],
                    end_time=e["end_time"], resumed_from=e["resumed_from"], outcome=e["outcome"],
                )
                for e in executions
            ),
            key=lambda e: e.start_time,
        )
        return Run(run_id=run_id, tags=v2["tags"], created_at=v2["created_at"], executions=tuple(found))
    v1 = cast(RunMetaV1, meta)
    return Run(
        run_id=run_id, tags=v1["tags"], created_at=v1["start_time"],
        executions=(legacy_execution(run_id, v1),),
    )


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

class CheckpointLogger(Protocol):
    def last_checkpoint(self, last_checkpoint: str) -> None:
        ...

@dataclass
class _ThreadRunHandle:
    _store: BaseStore
    _partial: ThreadMeta
    _key: str
    _ns: tuple[str, ...]
    _last_checkpoint: str | None

    def last_checkpoint(self, last_checkpoint: str):
        self._last_checkpoint = last_checkpoint

    async def complete(
        self,
    ):
        end_time = _time_string()
        to_write : ThreadMeta = {
            **self._partial,
            "end_time": end_time,
            "end_checkpoint_id": self._last_checkpoint
        }
        await self._store.aput(self._ns, self._key, {**to_write})

class ThreadLogger:
    def __init__(self, store: BaseStore, run_id: str, ns: tuple[str, ...], execution_id: str):
        self.store = store
        self.ns = ns
        self.run_id = run_id
        self.execution_id = execution_id

    async def start(
        self, thread_id: str, start_checkpoint: str | None, from_tool_id: str | None, description: str
    ) -> _ThreadRunHandle:
        start_time = _time_string()
        thread_run_id = uuid4().hex
        staged : ThreadMeta = {
            "start_time": start_time,
            "from_tool_id": from_tool_id,
            "end_checkpoint_id": None,
            "end_time": None,
            "run_id": self.run_id,
            "execution_id": self.execution_id,
            "start_checkpoint_id": start_checkpoint,
            "thread_id": thread_id,
            "description": description,
            # Read from the ambient contextvar: start() runs (via log_thread) in the
            # thread's own task, inside whatever named budget scope spawned it.
            "cost_center": current_cost_center()
        }
        try:
            await self.store.aput(
                self.ns, thread_run_id, {**staged}
            )
        except Exception:
            pass # swallow
        return _ThreadRunHandle(
            _store=self.store,
            _partial=staged,
            _key=thread_run_id,
            _ns=self.ns,
            _last_checkpoint=None
        )

_logger : ContextVar[None | ThreadLogger] = ContextVar("_logger", default=None)


@asynccontextmanager
async def log_thread(
    description: str,
    runnable: RunnableConfig,
    within_tool: str | None
) -> AsyncIterator[CheckpointLogger]:
    cur = _logger.get()
    if not cur:
        class Dummy:
            def last_checkpoint(self, last_checkpoint: str):
                pass
        yield Dummy()
        return
    assert "configurable" in runnable

    thread_id = runnable["configurable"]["thread_id"]

    checkpoint_id = runnable["configurable"].get("checkpoint_id", None)

    handle = await cur.start(
        description=description,
        from_tool_id=within_tool,
        start_checkpoint=checkpoint_id,
        thread_id=thread_id
    )
    try:
        yield handle
    finally:
        try:
            await handle.complete()
        except Exception:
            pass # swallow

class RunDataLogger(Protocol):
    """What ``thread_logger`` yields: the run's data records, and how this
    execution ends when a clean exit is not the whole story."""

    async def __call__(self, key: str, val: dict[str, Any]) -> None:
        """Write a ``run_data`` record under ``key``."""
        ...

    def outcome(self, outcome: ExecutionOutcome) -> None:
        """Record how this execution ends, overriding ``completed`` on a normal
        exit: a run parked on user input finishes its process cleanly, and the
        record should say so."""
        ...


@dataclass
class _ExecutionLog:
    _store: BaseStore
    _data_ns: tuple[str, ...]
    noted: ExecutionOutcome | None = None

    async def __call__(self, key: str, val: dict[str, Any]) -> None:
        try:
            await self._store.aput(self._data_ns, key, val)
        except Exception:
            pass

    def outcome(self, outcome: ExecutionOutcome) -> None:
        self.noted = outcome


async def _upgraded_run_record(
    store: BaseStore, run_ns: tuple[str, ...], exec_ns: tuple[str, ...], run_id: str,
    tags: dict[str, Any], now: str,
) -> RunMetaV2:
    """The v2 run record to write: fresh for a new run, carried forward for a run
    this code has seen, and converted from a v1 record the first time new code
    resumes an old run, with that record's single execution preserved."""
    existing = await store.aget(run_ns, run_id)
    if existing is None:
        return {"schema": 2, "created_at": now, "tags": tags}
    meta = cast(RunMeta, existing.value)
    if is_v2(meta):
        return {"schema": 2, "created_at": cast(RunMetaV2, meta)["created_at"], "tags": tags}
    legacy = legacy_execution(run_id, cast(RunMetaV1, meta))
    await store.aput(exec_ns, legacy.id, {
        "execution_id": legacy.id, "run_id": run_id, "start_time": legacy.start_time,
        "end_time": legacy.end_time, "resumed_from": None, "outcome": legacy.outcome,
    })
    return {"schema": 2, "created_at": legacy.start_time, "tags": tags}


@asynccontextmanager
async def thread_logger(
    store: BaseStore,
    tags: dict[str, Any],
    ns: tuple[str, ...],
    *,
    run_id: str | None = None,
    execution_id: str | None = None,
    resumed_from: str | None = None,
) -> AsyncIterator[RunDataLogger]:
    """Record this process as an execution of ``run_id`` and route thread records
    to it. ``execution_id`` defaults to the run id, which is what a run that is
    its own single execution looks like; a resumed run passes the id the control
    plane gave this process and the execution it follows."""
    run_id = uuid4().hex if run_id is None else run_id
    execution_id = run_id if execution_id is None else execution_id
    run_ns = runs_ns(ns)
    exec_ns = executions_ns(ns, run_id)
    now = _time_string()
    execution: ExecutionMeta = {
        "execution_id": execution_id,
        "run_id": run_id,
        "start_time": now,
        "end_time": None,
        "resumed_from": resumed_from,
        "outcome": None,
    }
    try:
        await store.aput(run_ns, run_id, {**await _upgraded_run_record(store, run_ns, exec_ns, run_id, tags, now)})
        await store.aput(exec_ns, execution_id, {**execution})
    except Exception:
        pass
    tok = _logger.set(ThreadLogger(store, run_id, threads_ns(ns), execution_id))
    log = _ExecutionLog(store, data_ns(ns, run_id))
    outcome: ExecutionOutcome = "completed"
    try:
        yield log
    except BaseException:
        outcome = "failed"
        raise
    finally:
        _logger.reset(tok)
        execution["end_time"] = _time_string()
        execution["outcome"] = log.noted or outcome
        try:
            await store.aput(exec_ns, execution_id, {**execution})
        except Exception:
            pass
