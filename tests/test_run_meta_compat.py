"""Run and execution records: the v1 shape production data holds (a run was its
own single execution), the v2 shape with execution records, one normalized
``Run`` view over both, and the writer's upgrade of a v1 record in place.

An in-memory store stands in for Postgres; the namespaces are the real ones.
"""

import pytest
from langgraph.store.memory import InMemoryStore

from composer.io.run_index import (
    ExportedRun,
    build_export,
    get_run,
    list_executions,
    list_runs,
    runs_ns,
)
from composer.io.thread_logging import (
    RunMetaV1,
    as_run,
    default_logging_ns,
    thread_logger,
)

pytestmark = pytest.mark.asyncio

NS = default_logging_ns(None)


def v1_record(start: str, end: str | None) -> RunMetaV1:
    return {"start_time": start, "end_time": end, "tags": {"root_thread_id": "autoprove_old"}}


def test_a_v1_record_is_a_run_with_one_execution_named_after_it() -> None:
    finished = as_run("r-old", v1_record("2026-01-01T00:00:00+00:00", "2026-01-01T01:00:00+00:00"))
    assert finished.created_at == "2026-01-01T00:00:00+00:00"
    assert finished.tags == {"root_thread_id": "autoprove_old"}
    (only,) = finished.executions
    assert only.id == "r-old" and only.run_id == "r-old" and only.resumed_from is None
    assert only.outcome == "completed" and finished.status == "completed"
    assert finished.end_time == "2026-01-01T01:00:00+00:00"

    running = as_run("r-old", v1_record("2026-01-01T00:00:00+00:00", None))
    assert running.status == "in-progress" and running.end_time is None
    assert running.latest is not None and running.latest.in_flight


async def test_a_fresh_run_writes_v2_and_its_execution() -> None:
    store = InMemoryStore()
    async with thread_logger(store, {"k": "v"}, NS, run_id="r1", execution_id="e1") as _:
        mid = await get_run(store, "r1")
        assert mid is not None and mid.status == "in-progress"
        assert mid.latest is not None and mid.latest.id == "e1" and mid.latest.resumed_from is None

    stored = (await store.aget(runs_ns(None), "r1"))
    assert stored is not None and stored.value["schema"] == 2 and "start_time" not in stored.value
    run = await get_run(store, "r1")
    assert run is not None and run.status == "completed" and run.tags == {"k": "v"}
    (e1,) = run.executions
    assert e1.id == "e1" and e1.end_time is not None and e1.outcome == "completed"


async def test_execution_id_defaults_to_the_run_id() -> None:
    store = InMemoryStore()
    async with thread_logger(store, {}, NS, run_id="solo"):
        pass
    run = await get_run(store, "solo")
    assert run is not None and [e.id for e in run.executions] == ["solo"]


async def test_a_resumed_run_appends_an_execution_and_keeps_its_birth_date() -> None:
    store = InMemoryStore()
    async with thread_logger(store, {"k": "v"}, NS, run_id="r1", execution_id="e1") as log:
        log.outcome("awaiting_input")
    first = await get_run(store, "r1")
    assert first is not None and first.status == "awaiting_input"

    async with thread_logger(store, {"k": "v2"}, NS, run_id="r1", execution_id="e2", resumed_from="e1"):
        pass
    run = await get_run(store, "r1")
    assert run is not None
    assert run.created_at == first.created_at
    assert run.tags == {"k": "v2"}
    assert [(e.id, e.resumed_from, e.outcome) for e in run.executions] == [
        ("e1", None, "awaiting_input"), ("e2", "e1", "completed"),
    ]
    assert run.status == "completed"


async def test_a_crashing_execution_is_recorded_as_failed() -> None:
    store = InMemoryStore()
    with pytest.raises(RuntimeError):
        async with thread_logger(store, {}, NS, run_id="r1", execution_id="e1"):
            raise RuntimeError("boom")
    run = await get_run(store, "r1")
    assert run is not None and run.status == "failed"
    assert run.latest is not None and run.latest.end_time is not None


async def test_resuming_a_v1_run_upgrades_it_and_preserves_its_execution() -> None:
    """Production data: a run recorded before executions existed, now resumed by
    new code. Its record becomes v2, its one historical execution becomes a record
    named after the run, and the new execution follows it."""
    store = InMemoryStore()
    await store.aput(runs_ns(None), "old", dict(v1_record("2026-01-01T00:00:00+00:00", "2026-01-01T01:00:00+00:00")))
    before = await get_run(store, "old")
    assert before is not None and [e.id for e in before.executions] == ["old"]

    async with thread_logger(store, {"root_thread_id": "autoprove_old"}, NS, run_id="old", execution_id="e2", resumed_from="old"):
        pass

    stored = await store.aget(runs_ns(None), "old")
    assert stored is not None and stored.value["schema"] == 2
    assert stored.value["created_at"] == "2026-01-01T00:00:00+00:00"
    assert [e["execution_id"] for e in sorted(await list_executions(store, "old"), key=lambda e: e["start_time"])] == ["old", "e2"]
    run = await get_run(store, "old")
    assert run is not None
    assert [(e.id, e.resumed_from, e.outcome) for e in run.executions] == [
        ("old", None, "completed"), ("e2", "old", "completed"),
    ]


async def test_list_runs_orders_by_birth_and_filters_since() -> None:
    store = InMemoryStore()
    await store.aput(runs_ns(None), "old", dict(v1_record("2026-01-01T00:00:00+00:00", "2026-01-01T01:00:00+00:00")))
    async with thread_logger(store, {}, NS, run_id="new", execution_id="e1"):
        pass
    runs = await list_runs(store)
    assert [r.run_id for r in runs] == ["new", "old"]
    assert [r.run_id for r in await list_runs(store, since="2026-06-01")] == ["new"]


async def test_exports_read_v1_files_and_carry_executions_in_v2() -> None:
    v1_file = ExportedRun.model_validate({
        "version": 1, "run_id": "old", "run": dict(v1_record("2026-01-01T00:00:00+00:00", None)), "threads": [],
    })
    assert v1_file.executions == []
    assert v1_file.view().status == "in-progress" and [e.id for e in v1_file.view().executions] == ["old"]

    store = InMemoryStore()

    class _NoCheckpoints:
        pass

    async with thread_logger(store, {}, NS, run_id="r1", execution_id="e1"):
        pass
    exported = await build_export(store, _NoCheckpoints(), "r1")  # type: ignore[arg-type] - no threads to walk
    assert exported.version == 2
    assert [e["execution_id"] for e in exported.executions] == ["e1"]
    assert exported.view().status == "completed"
