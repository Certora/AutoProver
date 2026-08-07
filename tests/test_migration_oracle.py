"""Unit tests for the edit-migration machinery.

Two units under test:

- ``mk_oracle`` (``edit_oracle``): the conservative portability oracle. Any
  content difference between the two source views marks a finding stale, with
  the changed files named in the reason; content-identical views are
  up-to-date. The edit store is an id→StoredEdit dict; ``project_root`` is a
  real ``tmp_path`` for the V0 read-through case.

- ``VersionedAgentIndex`` (``versioned_index``): ``asearch_versioned`` /
  ``aget`` / ``aput``, driven over a *real* langgraph ``InMemoryStore`` with
  the conftest mock embedder and a real ``AgentIndex`` as the V0/base
  ``_wrapped`` pool — so version-scoped vector search, layer merging and
  staleness marking are genuinely exercised. The oracle is a call-recording
  ``CountingOracle``: search staleness never consults it (version mismatch is
  stale, full stop); ``aget`` consults it exactly once per cross-version
  retrieval.
"""

import types
from typing import Any, Callable, cast

import pytest
import pytest_asyncio

from langgraph.store.memory import InMemoryStore

from composer.kb.knowledge_base import DefaultEmbedder
from composer.spec.agent_index import AgentIndex, AgentIndexConfig
from composer.spec.source.munge.edit_oracle import mk_oracle
from composer.spec.source.munge.edit_store import StoredEdit
from composer.spec.source.versioned_index import (
    VersionedAgentIndex, AnswerPortability, Stale, UpToDate,
)

from .conftest import QnATransformer, EMBEDDING_DIM


BASE_NS: tuple[str, ...] = ("base",)
TARGET_NS: tuple[str, ...] = ("versioned",)


# ===========================================================================
# Oracle (mk_oracle) — edit store mocked, project_root a real tmp_path
# ===========================================================================

class FakeEditStore:
    """``id -> StoredEdit`` with the async ``read`` the oracle uses."""
    def __init__(self, edits: dict[str, StoredEdit]) -> None:
        self.edits = edits
        self.read_calls: list[str] = []

    async def read(self, id: str) -> StoredEdit | None:
        self.read_calls.append(id)
        return self.edits.get(id)


def _sc(project_root) -> Any:
    return types.SimpleNamespace(project_root=str(project_root))


def _stored(vfs: dict[str, str]) -> StoredEdit:
    return StoredEdit(vfs=vfs, executive_summary="summary", why_sound="sound")


@pytest.mark.asyncio
class TestOracle:
    async def test_identical_views_are_up_to_date(self, tmp_path):
        store = FakeEditStore({"v0": _stored({"a.sol": "SAME"}), "v1": _stored({"a.sol": "SAME"})})
        oracle = mk_oracle(cast(Any, store), _sc(tmp_path))

        res = await oracle(start_version="v0", end_version="v1", question="q", answer="a")

        assert res == UpToDate(status="ok")

    async def test_changed_file_is_stale_naming_the_file(self, tmp_path):
        store = FakeEditStore({"v0": _stored({"a.sol": "OLD"}), "v1": _stored({"a.sol": "NEW"})})
        oracle = mk_oracle(cast(Any, store), _sc(tmp_path))

        res = await oracle(start_version="v0", end_version="v1", question="q", answer="a")

        assert res["status"] == "stale"
        assert "a.sol" in cast(Stale, res)["reason"]

    async def test_only_changed_files_are_named(self, tmp_path):
        store = FakeEditStore({
            "v0": _stored({"a.sol": "SAME", "b.sol": "OLD"}),
            "v1": _stored({"a.sol": "SAME", "b.sol": "NEW"}),
        })
        oracle = mk_oracle(cast(Any, store), _sc(tmp_path))

        res = await oracle(start_version="v0", end_version="v1", question="q", answer="a")

        reason = cast(Stale, res)["reason"]
        assert "b.sol" in reason and "a.sol" not in reason

    async def test_v0_reads_base_fs_layer(self, tmp_path):
        # start_version=None → the old side reads through to project_root on disk.
        (tmp_path / "a.sol").write_text("DISK BODY")
        store = FakeEditStore({"v1": _stored({"a.sol": "EDITED BODY"})})
        oracle = mk_oracle(cast(Any, store), _sc(tmp_path))

        res = await oracle(start_version=None, end_version="v1", question="q", answer="a")

        assert res["status"] == "stale"
        assert "a.sol" in cast(Stale, res)["reason"]

    async def test_v0_overlay_matching_disk_is_up_to_date(self, tmp_path):
        # An overlay entry whose content equals the on-disk file is not a change.
        (tmp_path / "a.sol").write_text("DISK BODY")
        store = FakeEditStore({"v1": _stored({"a.sol": "DISK BODY"})})
        oracle = mk_oracle(cast(Any, store), _sc(tmp_path))

        res = await oracle(start_version=None, end_version="v1", question="q", answer="a")

        assert res == UpToDate(status="ok")

    async def test_added_file_absent_from_old_is_stale(self, tmp_path):
        # A new-overlay path in neither the start overlay nor on disk: old
        # resolves to None (a pure addition), still a real change.
        store = FakeEditStore({
            "v0": _stored({"a.sol": "X"}),
            "v1": _stored({"a.sol": "X", "harness.sol": "contract H {}"}),
        })
        oracle = mk_oracle(cast(Any, store), _sc(tmp_path))

        res = await oracle(start_version="v0", end_version="v1", question="q", answer="a")

        assert res["status"] == "stale"
        assert "harness.sol" in cast(Stale, res)["reason"]


# ===========================================================================
# VersionedAgentIndex — real store + real base pool
# ===========================================================================

class CountingOracle:
    """Records (start, end) calls; returns a preset verdict (fixed or fn)."""
    def __init__(self, result: AnswerPortability | Any = UpToDate(status="ok")) -> None:
        self._result = result
        self.calls: list[tuple[str | None, str]] = []

    async def __call__(
        self, *, start_version: str | None, end_version: str, question: str, answer: str
    ) -> AnswerPortability:
        self.calls.append((start_version, end_version))
        return self._result(start_version, end_version) if callable(self._result) else self._result


@pytest_asyncio.fixture
async def store(qna_factory: Callable[[], QnATransformer]) -> InMemoryStore:
    qna = qna_factory()
    for q in ("Q_base", "Q_base2", "Q_stored", "Q_probe"):
        qna.register(q, [q])
    return InMemoryStore(index={
        "embed": DefaultEmbedder(model=qna.as_transformer),
        "dims": EMBEDDING_DIM,
        "fields": None,
    })


def _index(store: InMemoryStore, oracle: CountingOracle | None = None) -> VersionedAgentIndex:
    return VersionedAgentIndex(
        _wrapped=AgentIndex(store, AgentIndexConfig(base_layer=BASE_NS)),
        _store=store,
        _target_ns=TARGET_NS,
        _migration_oracle=cast(Any, oracle or CountingOracle()),
    )


# ---------------------------------------------------------------------------
# asearch_versioned — the live explorer's read surface
# ---------------------------------------------------------------------------

def _by_answer(results: list) -> dict[str, bool]:
    """answer -> stale, over a list-shaped asearch_versioned result."""
    return {r["answer"]: r["stale"] for r in results}


@pytest.mark.asyncio
class TestAsearchVersioned:
    async def test_empty_versions_lifts_wrapped_results(self, store):
        idx = _index(store)
        await idx.aput("Q_base", "A_base", versions=[])
        await idx.aput("Q_base2", "A_base2", versions=[])

        res = await idx.asearch_versioned("Q_probe", [])

        assert isinstance(res, list)
        stale = _by_answer(res)
        assert {"A_base", "A_base2"} <= set(stale)
        assert all(v is False for v in stale.values())  # base entries never stale here

    async def test_exact_version_hit_returns_cached_keyed_result(self, store):
        idx = _index(store)
        ref = await idx.aput("Q_stored", "cached answer", versions=["v1"])

        res = await idx.asearch_versioned("Q_stored", ["v1"])

        assert isinstance(res, dict)  # KeyedAgentResult, not the list path
        assert res["answer"] == "cached answer"
        assert res["ref_string"] == ref

    async def test_versioned_hit_at_latest_not_stale(self, store):
        idx = _index(store)
        await idx.aput("Q_stored", "ans_v2", versions=["v1", "v2"])  # version_key v2

        # Probe question differs, so the exact-key path misses → vector path.
        res = await idx.asearch_versioned("Q_probe", ["v1", "v2"])

        assert isinstance(res, list)
        assert _by_answer(res)["ans_v2"] is False

    async def test_versioned_hit_at_older_version_is_stale(self, store):
        idx = _index(store)
        await idx.aput("Q_stored", "ans_v1", versions=["v1"])  # version_key v1

        res = await idx.asearch_versioned("Q_probe", ["v1", "v2"])

        assert _by_answer(cast(list, res))["ans_v1"] is True  # v1 != latest v2

    async def test_v0_base_hit_always_stale_under_versions(self, store):
        idx = _index(store)
        await idx.aput("Q_base", "A_base", versions=[])  # base pool, no version_key

        res = await idx.asearch_versioned("Q_probe", ["v1", "v2"])

        assert _by_answer(cast(list, res))["A_base"] is True

    async def test_search_never_consults_the_oracle(self, store):
        oracle = CountingOracle()
        idx = _index(store, oracle)
        await idx.aput("Q_base", "A_base", versions=[])
        await idx.aput("Q_stored", "ans_v1", versions=["v1"])

        await idx.asearch_versioned("Q_probe", ["v1", "v2"])

        assert oracle.calls == []


# ---------------------------------------------------------------------------
# aput / aget — version-scoped storage + retrieval
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestAput:
    async def test_empty_versions_delegates_to_base_pool(self, store):
        idx = _index(store)
        ref = await idx.aput("Q_base", "A_base", versions=[])
        assert ref is not None
        # Landed in the base namespace, retrievable via the wrapped pool.
        item = await store.aget(BASE_NS, ref)
        assert item is not None and item.value["answer"] == "A_base"
        # Not under the versioned namespace.
        assert await store.aget(TARGET_NS, ref) is None

    async def test_versioned_stored_under_target_ns_with_version_key(self, store):
        idx = _index(store)
        ref = await idx.aput("q", "the answer", versions=["v0", "v1"])
        assert ref == idx._versioned_key("q", "v1")
        item = await store.aget(TARGET_NS, ref)
        assert item is not None
        assert item.value["version_key"] == "v1"
        assert item.value["answer"] == "the answer"


@pytest.mark.asyncio
class TestAget:
    async def test_base_pool_hit_consults_oracle_from_birth(self, store):
        # A base (V0) answer under version history → the oracle judges the whole
        # None→latest range. Oracle says ok → no caveat.
        oracle = CountingOracle(UpToDate(status="ok"))
        idx = _index(store, oracle)
        ref = await idx.aput("Q_base", "A_base", versions=[])
        assert ref is not None

        res = await idx.aget(ref, versions=["v1", "v2"])

        assert res is not None
        assert res["answer"] == "A_base"
        assert res["caveat"] is None
        assert oracle.calls == [(None, "v2")]

    async def test_base_pool_hit_stale_surfaces_caveat(self, store):
        idx = _index(store, CountingOracle(Stale(status="stale", reason="moved")))
        ref = await idx.aput("Q_base", "A_base", versions=[])
        assert ref is not None

        res = await idx.aget(ref, versions=["v1"])

        assert res is not None and res["caveat"] == "moved"

    async def test_empty_versions_no_caveat_no_oracle(self, store):
        oracle = CountingOracle()
        idx = _index(store, oracle)
        ref = await idx.aput("Q_base", "A_base", versions=[])
        assert ref is not None

        res = await idx.aget(ref, versions=[])

        assert res is not None and res["caveat"] is None
        assert oracle.calls == []

    async def test_versioned_exact_version_no_caveat_no_oracle(self, store):
        oracle = CountingOracle()
        idx = _index(store, oracle)
        ref = await idx.aput("q", "ans", versions=["v1", "v2"])  # version_key v2

        res = await idx.aget(ref, versions=["v1", "v2"])

        assert res is not None
        assert res["answer"] == "ans"
        assert res["caveat"] is None
        assert oracle.calls == []

    async def test_versioned_older_version_in_history_consults_oracle(self, store):
        oracle = CountingOracle(Stale(status="stale", reason="outdated"))
        idx = _index(store, oracle)
        ref = await idx.aput("q", "ans", versions=["v1"])  # version_key v1

        res = await idx.aget(ref, versions=["v1", "v2"])  # now viewing at v2

        assert res is not None
        assert res["caveat"] == "outdated"
        assert oracle.calls == [("v1", "v2")]

    async def test_versioned_version_not_in_history_returns_none(self, store):
        idx = _index(store)
        ref = await idx.aput("q", "ans", versions=["v9"])  # version_key v9

        res = await idx.aget(ref, versions=["v1", "v2"])  # v9 unreachable

        assert res is None

    async def test_total_miss_returns_none(self, store):
        idx = _index(store)
        assert await idx.aget("nonexistent-key", versions=["v1"]) is None


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_versioned_key_stable_and_version_sensitive(self):
        k1 = VersionedAgentIndex._versioned_key("who calls foo", "v1")
        k2 = VersionedAgentIndex._versioned_key("who calls foo", "v1")
        k3 = VersionedAgentIndex._versioned_key("who calls foo", "v2")
        assert k1 == k2 and k1 != k3

    def test_caveat_ok_none_stale_reason(self):
        assert VersionedAgentIndex._caveat(UpToDate(status="ok")) is None
        assert VersionedAgentIndex._caveat(Stale(status="stale", reason="r")) == "r"
