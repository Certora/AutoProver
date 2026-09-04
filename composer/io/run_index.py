"""Store-side helpers and wire format for `ap-trail`.

Reads the per-user `RunMeta` / `ThreadMeta` records written by
``composer.io.thread_logging`` and packages them (with their per-thread
timelines) for either live drill-down or offline replay.
"""

import gzip
import os
from typing import Annotated, Literal, cast

from pydantic import BaseModel, Field

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.store.base import BaseStore

from composer.core.user import user_data_ns
from composer.io.thread_logging import (
    DEFAULT_META_NS,
    ExecutionMeta,
    Run,
    RunMeta,
    ThreadMeta,
    as_run,
    data_ns as _data_subns,
    executions_ns as _executions_subns,
    is_v2,
    runs_ns as _runs_subns,
    threads_ns as _threads_subns,
)
from composer.io.thread_timeline import SummarizationMarker, TimelineItem, load_timeline


# ---------------------------------------------------------------------------
# Namespaces
# ---------------------------------------------------------------------------

def logging_ns(uid: str | None = None) -> tuple[str, ...]:
    """Conventional namespace for thread_logging records: ``user_data/<uid>/logging``."""
    return user_data_ns(uid) + DEFAULT_META_NS


def runs_ns(uid: str | None = None) -> tuple[str, ...]:
    return _runs_subns(logging_ns(uid))


def threads_ns(uid: str | None = None) -> tuple[str, ...]:
    return _threads_subns(logging_ns(uid))


def data_ns(run_id: str, uid: str | None = None) -> tuple[str, ...]:
    return _data_subns(logging_ns(uid), run_id)


def executions_ns(run_id: str, uid: str | None = None) -> tuple[str, ...]:
    return _executions_subns(logging_ns(uid), run_id)


# ---------------------------------------------------------------------------
# Store readers
# ---------------------------------------------------------------------------

async def list_executions(
    store: BaseStore, run_id: str, *, uid: str | None = None
) -> list[ExecutionMeta]:
    """The run's execution records, as stored. Empty for a run written before
    executions existed; :func:`as_run` synthesizes that run's one execution."""
    items = await store.asearch(executions_ns(run_id, uid), limit=1000)
    return [cast(ExecutionMeta, it.value) for it in items]


async def _load_run(store: BaseStore, run_id: str, meta: RunMeta, uid: str | None) -> Run:
    executions = await list_executions(store, run_id, uid=uid) if is_v2(meta) else []
    return as_run(run_id, meta, executions)


async def list_runs(
    store: BaseStore,
    *,
    uid: str | None = None,
    limit: int = 50,
    since: str | None = None,
) -> list[Run]:
    """Runs most-recent-first, normalized whatever shape they were stored in.

    ``since`` is an ISO-8601 string compared lexically against the run's start
    (also ISO-8601, so lex compare == chronological compare).
    """
    items = await store.asearch(runs_ns(uid), limit=limit)
    runs = [await _load_run(store, it.key, cast(RunMeta, it.value), uid) for it in items]
    if since is not None:
        runs = [r for r in runs if r.start_time >= since]
    runs.sort(key=lambda r: r.start_time, reverse=True)
    return runs


async def get_run(
    store: BaseStore, run_id: str, *, uid: str | None = None
) -> Run | None:
    item = await store.aget(runs_ns(uid), run_id)
    if item is None:
        return None
    return await _load_run(store, run_id, cast(RunMeta, item.value), uid)


async def get_run_record(
    store: BaseStore, run_id: str, *, uid: str | None = None
) -> RunMeta | None:
    """The run record as stored, for the export; readers want :func:`get_run`."""
    item = await store.aget(runs_ns(uid), run_id)
    if item is None:
        return None
    return cast(RunMeta, item.value)


async def list_threads_for_run(
    store: BaseStore, run_id: str, *, uid: str | None = None
) -> list[tuple[str, ThreadMeta]]:
    """Return ``[(thread_run_id, meta), ...]`` for one run, oldest-first."""
    items = await store.asearch(
        threads_ns(uid), filter={"run_id": run_id}, limit=1000
    )
    pairs: list[tuple[str, ThreadMeta]] = [
        (it.key, cast(ThreadMeta, it.value)) for it in items
    ]
    pairs.sort(key=lambda kv: kv[1]["start_time"])
    return pairs


async def list_run_data(
    store: BaseStore, run_id: str, *, uid: str | None = None
) -> list[tuple[str, dict]]:
    """Return ``[(key, metadata), ...]`` for one run's ``run_data`` records, key-sorted."""
    items = await store.asearch(data_ns(run_id, uid), limit=1000)
    pairs: list[tuple[str, dict]] = [(it.key, cast(dict, it.value)) for it in items]
    pairs.sort(key=lambda kv: kv[0])
    return pairs


async def get_run_data(
    store: BaseStore, run_id: str, key: str, *, uid: str | None = None
) -> dict | None:
    """Return one ``run_data`` metadata dict by key, or ``None`` if absent."""
    item = await store.aget(data_ns(run_id, uid), key)
    if item is None:
        return None
    return cast(dict, item.value)


# ---------------------------------------------------------------------------
# Wire format
# ---------------------------------------------------------------------------

#: 2 adds ``executions``; a version-1 file has none and reads as a run that was
#: its own single execution, exactly like a v1 store record.
WIRE_VERSION = 2


# Inner discrimination on BaseMessage's `type` Literal field. AIMessage.type ==
# "ai", HumanMessage.type == "human", etc. — pydantic uses these to dispatch to
# the right subclass when deserializing. Any future BaseMessage subclass we
# care about gets added here.
type ChatMessage = Annotated[
    AIMessage | HumanMessage | SystemMessage | ToolMessage,
    Field(discriminator="type"),
]


class ExportedMessage(BaseModel):
    kind: Literal["message"] = "message"
    data: ChatMessage
    checkpoint_id: str | None


class ExportedSummary(BaseModel):
    kind: Literal["summary"] = "summary"
    checkpoint_id: str


type ExportedTimelineItem = Annotated[
    ExportedMessage | ExportedSummary,
    Field(discriminator="kind"),
]


class ExportedThread(BaseModel):
    thread_run_id: str
    meta: ThreadMeta
    timeline: list[ExportedTimelineItem]


class ExportedRun(BaseModel):
    version: int
    run_id: str
    run: RunMeta
    threads: list[ExportedThread]
    executions: list[ExecutionMeta] = Field(default_factory=list)

    def view(self) -> Run:
        return as_run(self.run_id, self.run, self.executions)


def _encode_timeline_item(item: TimelineItem, checkpoint_id: str | None) -> ExportedTimelineItem:
    if isinstance(item, SummarizationMarker):
        return ExportedSummary(checkpoint_id=item.checkpoint_id)
    return ExportedMessage(data=cast(ChatMessage, item), checkpoint_id=checkpoint_id)


def _decode_timeline_item(entry: ExportedTimelineItem) -> tuple[TimelineItem, str | None]:
    match entry:
        case ExportedSummary(checkpoint_id=cid):
            return SummarizationMarker(checkpoint_id=cid), None
        case ExportedMessage(data=msg, checkpoint_id=cid):
            return msg, cid


def decode_thread_timeline(exported: ExportedThread) -> list[tuple[TimelineItem, str | None]]:
    return [_decode_timeline_item(e) for e in exported.timeline]


async def build_export(
    store: BaseStore,
    checkpointer: BaseCheckpointSaver,
    run_id: str,
    *,
    uid: str | None = None,
) -> ExportedRun:
    """Materialize a full ``ExportedRun`` from live DB state.

    For each thread segment, walks the checkpoint chain bounded by the
    ThreadMeta's ``start_checkpoint_id`` / ``end_checkpoint_id``.
    """
    run = await get_run_record(store, run_id, uid=uid)
    if run is None:
        raise KeyError(f"No such run: {run_id}")
    executions = await list_executions(store, run_id, uid=uid) if is_v2(run) else []

    threads = await list_threads_for_run(store, run_id, uid=uid)
    exported_threads: list[ExportedThread] = []
    for thread_run_id, meta in threads:
        timeline = await load_timeline(
            checkpointer,
            meta["thread_id"],
            anchor_checkpoint_id=meta["end_checkpoint_id"],
            stop_at_checkpoint_id=meta["start_checkpoint_id"],
        )
        exported_threads.append(
            ExportedThread(
                thread_run_id=thread_run_id,
                meta=meta,
                timeline=[_encode_timeline_item(item, cid) for item, cid in timeline],
            )
        )

    return ExportedRun(
        version=WIRE_VERSION,
        run_id=run_id,
        run=run,
        threads=exported_threads,
        executions=executions,
    )


def write_export(exported: ExportedRun, path: str) -> int:
    """Gzip + JSON serialize an ``ExportedRun``. Returns bytes written."""
    payload = exported.model_dump_json().encode("utf-8")
    with gzip.open(path, "wb") as f:
        f.write(payload)
    return os.path.getsize(path)


def read_export(path: str) -> ExportedRun:
    with gzip.open(path, "rb") as f:
        payload = f.read()
    return ExportedRun.model_validate_json(payload)
