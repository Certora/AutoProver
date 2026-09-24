"""``ap-trail recover-drafts`` — walking a dead run's drafts back into the cache.

The addressing is the part worth pinning: a unit's thread id is *rebuilt* from its cache
namespace, because namespace elements contain the ``-`` that joins thread-id segments and so a
thread id does not split back apart. A regression here caches a draft under the wrong unit,
which no type would catch.
"""

import argparse
from typing import Any

import pytest
from langgraph.store.memory import InMemoryStore

from composer.cli.diagnostics import ap_trail_recover as recover
from composer.spec.context import CvlrGeneration, WorkflowContext
from composer.spec.cvlr.author import LAST_ATTEMPT_KEY, LastCvlrAttempt

ROOT = ("user_data", "_anonymous", "ns", "proj")
#: The shape that makes reconstruction non-trivial: a hyphen inside a single namespace element.
UNIT = ("solana-properties", "comp1", "batch1")
THREAD_ROOT = "cvlr_abc123"


class _Munge:
    def __init__(self, text: str):
        self._text = text

    def describe(self) -> str:
        return self._text


class _Checkpointer:
    """Enough of the saver to answer ``alist``: drafts oldest-first per thread, served newest-first."""

    def __init__(self, drafts: dict[str, list[dict[str, Any]]]):
        self._drafts = drafts

    async def alist(self, config: dict[str, Any], **_kw: Any):
        for values in reversed(self._drafts.get(config["configurable"]["thread_id"], [])):
            yield type("Entry", (), {"checkpoint": {"channel_values": values}})()


async def _cached(store: InMemoryStore, unit: tuple[str, ...]) -> LastCvlrAttempt | None:
    ctx = WorkflowContext.create(
        services=lambda _n: None,  # type: ignore[arg-type]
        thread_id="probe", store=store, recursion_limit=1, cache_namespace=ROOT + unit,
    ).abstract(CvlrGeneration)
    return await ctx.child(LAST_ATTEMPT_KEY).cache_get(LastCvlrAttempt)


async def _run(store: InMemoryStore, checkpointer: Any, **kw: Any) -> int:
    opts: dict[str, Any] = {"dry_run": False, "largest": False, **kw}
    return await recover._recover(
        store, checkpointer, root=ROOT, thread_root=THREAD_ROOT, **opts
    )


def _store_with_unit() -> InMemoryStore:
    store = InMemoryStore()
    # A namespace only exists once something is under it; `_desc` is what `child(key, tag)` writes.
    store.put(ROOT + UNIT, "_desc", {"what": "a unit"})
    return store


@pytest.mark.asyncio
async def test_a_units_thread_id_is_rebuilt_from_its_namespace():
    store = _store_with_unit()
    thread = f"{THREAD_ROOT}-solana-properties-comp1-batch1"
    found = await _run(store, _Checkpointer({thread: [{"curr_spec": "rule a", "munges": []}]}))

    assert found == 1
    got = await _cached(store, UNIT)
    assert got is not None and got.spec == "rule a"


@pytest.mark.asyncio
async def test_the_final_draft_is_taken_by_default_and_the_peak_on_request():
    thread = f"{THREAD_ROOT}-solana-properties-comp1-batch1"
    history = [
        {"curr_spec": "a\nb\nc\nd", "munges": [_Munge("swap x")]},
        {"curr_spec": "a", "munges": []},
    ]

    store = _store_with_unit()
    await _run(store, _Checkpointer({thread: history}))
    final = await _cached(store, UNIT)
    assert final is not None and final.spec == "a" and final.munges == []

    store = _store_with_unit()
    await _run(store, _Checkpointer({thread: history}), largest=True)
    peak = await _cached(store, UNIT)
    # The munges travel from the same checkpoint as the draft, not from the newest one.
    assert peak is not None and peak.spec == "a\nb\nc\nd" and peak.munges == ["swap x"]


@pytest.mark.asyncio
async def test_a_thread_that_never_held_a_draft_is_passed_over():
    # Property-extraction threads share the namespace tree and have no ``curr_spec``; caching an
    # empty attempt for one would seed a later run with a blank buffer.
    store = _store_with_unit()
    thread = f"{THREAD_ROOT}-solana-properties-comp1-batch1"
    found = await _run(store, _Checkpointer({thread: [{"curr_spec": "   ", "munges": []}]}))

    assert found == 0
    assert await _cached(store, UNIT) is None


@pytest.mark.asyncio
async def test_a_dry_run_reports_without_writing():
    store = _store_with_unit()
    thread = f"{THREAD_ROOT}-solana-properties-comp1-batch1"
    found = await _run(
        store, _Checkpointer({thread: [{"curr_spec": "rule a", "munges": []}]}), dry_run=True
    )

    assert found == 1
    assert await _cached(store, UNIT) is None


def test_the_subcommand_is_wired_with_the_arguments_main_reads():
    parser = argparse.ArgumentParser()
    recover.add_arguments(parser)
    args = parser.parse_args(["run-1", "--draft", "largest", "-n"])

    assert (args.run_id, args.draft, args.dry_run, args.uid) == ("run-1", "largest", True, None)
