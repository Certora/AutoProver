"""Chain-walk timeline loader for thread debugging.

Walks a thread's checkpoint chain from an anchor backward, returning a
deduplicated list of messages with ``SummarizationMarker`` entries inserted
at points where the summarizer wiped the message channel.

Used by ``ap-trail view`` (one segment at a time, bounded by the parent
ThreadMeta's checkpoint range), the run exporter, and ``snapshot-viewer``
(entire chain).

Two loading strategies produce the ``(checkpoint_id, messages)`` history the
shared fold consumes:

- ``_load_history_generic`` — portable ``BaseCheckpointSaver`` path. Correct
  everywhere, but materializes the full state of every checkpoint; against
  Postgres that transfers and deserializes every channel blob (the growing
  ``messages`` list, the ``vfs``, ...) once per checkpoint — O(n²) bytes of
  which the fold reads only ``messages``. Kept for in-memory savers.

- ``_load_history_pg`` — Postgres fast path. One metadata-only query for the
  chain structure, then blob fetches for only the message-channel versions
  that can contribute something the anchor snapshot doesn't already contain:
  the anchor's version, plus the (pre, post) pair around every point where a
  ``RemoveMessage`` delta was written (found by a server-side byte scan of
  ``checkpoint_writes`` — lc-serialization embeds the class name as raw UTF-8
  in the msgpack blob) or where the blob size drops (drift insurance should
  the serialization format ever change). Everything not fetched is provably a
  subset of the next fetched snapshot. Validated against the generic path by
  ``scripts/validate_fast_timeline.py`` (identical id sequences, ~23x faster
  on a real run).
"""

from dataclasses import dataclass
from typing import cast

from langchain_core.runnables import RunnableConfig
from langchain_core.messages import BaseMessage
from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver


@dataclass(frozen=True)
class SummarizationMarker:
    """A timeline divider for a checkpoint where summarization wiped the
    message channel. Pre-summary turns above the marker survived the walk
    because the checkpoint chain itself is intact — only the latest-state
    view of ``messages`` loses them.
    """

    checkpoint_id: str


type TimelineItem = BaseMessage | SummarizationMarker

#: ``(checkpoint_id, messages)`` pairs, oldest-first.
type _History = list[tuple[str, list[BaseMessage]]]


async def _load_history_generic(
    checkpointer: BaseCheckpointSaver,
    thread_id: str,
    anchor_checkpoint_id: str | None,
    stop_at_checkpoint_id: str | None,
) -> _History:
    """Portable history loader over the ``BaseCheckpointSaver`` API."""
    anchor_config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    if anchor_checkpoint_id is not None:
        anchor_config["configurable"]["checkpoint_id"] = anchor_checkpoint_id
    anchor = await checkpointer.aget_tuple(anchor_config)
    if anchor is None:
        return []

    # The checkpoint table for a thread is a forest — restarts from a non-tip
    # checkpoint fork new branches that share the thread_id. ``alist`` returns
    # the union, which would surface messages from abandoned branches.
    # Pre-fetch everything by id, then walk the parent chain from the anchor:
    # the alternative (one ``aget_tuple`` per hop) costs an RTT per step.
    list_config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    by_id: dict[str, CheckpointTuple] = {}
    async for ct in checkpointer.alist(list_config):
        if "configurable" not in ct.config:
            continue
        cid = ct.config["configurable"].get("checkpoint_id")
        if cid is not None:
            by_id[cid] = ct

    history: _History = []
    current_ct: CheckpointTuple | None = anchor
    while current_ct is not None:
        cid = current_ct.config.get("configurable", {}).get("checkpoint_id")
        if cid is None:
            break
        ckpt_msgs = current_ct.checkpoint["channel_values"].get("messages", [])
        history.append((cid, ckpt_msgs))
        if cid == stop_at_checkpoint_id:
            break
        parent_cfg = current_ct.parent_config
        if parent_cfg is None:
            break
        parent_cid = parent_cfg.get("configurable", {}).get("checkpoint_id")
        if parent_cid is None:
            break
        # Common case: pre-loaded by ``alist``. Fallback: if ``alist`` was
        # bounded (large windows, partial TTL eviction, exotic checkpointer
        # implementation), pay a one-off RTT to fetch the parent directly.
        # Without this, the walk silently truncates at the alist boundary.
        current_ct = by_id.get(parent_cid)
        if current_ct is None:
            current_ct = await checkpointer.aget_tuple(
                {"configurable": {"thread_id": thread_id, "checkpoint_id": parent_cid}}
            )
    history.reverse()
    return history


# Chain structure only — no blob joins. ``checkpoint_ns = ''`` matches the
# generic path's anchor semantics (``aget_tuple`` defaults the namespace to
# ''); every sub-graph in this codebase runs under a fresh thread_id rather
# than a checkpoint namespace.
_CHAIN_SQL = """
SELECT checkpoint_id, parent_checkpoint_id,
       checkpoint #>> '{channel_versions,messages}' AS messages_version
FROM checkpoints
WHERE thread_id = %s AND checkpoint_ns = ''
"""

_LEN_SQL = """
SELECT version, length(blob) AS len
FROM checkpoint_blobs
WHERE thread_id = %s AND checkpoint_ns = '' AND channel = 'messages'
  AND version = ANY(%s)
"""

# Byte-scan oracle: lc-serialized RemoveMessage deltas (including the
# id="__remove_all__" wipe sentinel) embed the class name as raw UTF-8 in the
# msgpack blob. False positives (a message *body* mentioning the string) just
# cost an extra snapshot fetch; the fold decides everything semantic.
_REMOVAL_SQL = """
SELECT DISTINCT checkpoint_id
FROM checkpoint_writes
WHERE thread_id = %s AND checkpoint_ns = '' AND channel = 'messages'
  AND position(%s IN blob) > 0
"""

_BLOBS_SQL = """
SELECT version, type, blob
FROM checkpoint_blobs
WHERE thread_id = %s AND checkpoint_ns = '' AND channel = 'messages'
  AND version = ANY(%s)
"""


async def _load_history_pg(
    saver: AsyncPostgresSaver,
    thread_id: str,
    anchor_checkpoint_id: str | None,
    stop_at_checkpoint_id: str | None,
) -> _History:
    """Postgres fast path; see module docstring for the strategy."""
    # Diagnostics-only use of the saver's internal cursor helper: it owns the
    # pool-vs-connection choice, the connection lock, and the row factory.
    async with saver._cursor() as cur:
        await cur.execute(_CHAIN_SQL, (thread_id,))
        rows = await cur.fetchall()
        if not rows:
            return []
        by_id = {r["checkpoint_id"]: r for r in rows}

        # UUIDv6 ids: lexicographic max == latest, matching the generic
        # path's anchorless aget_tuple.
        anchor_id = anchor_checkpoint_id if anchor_checkpoint_id is not None else max(by_id)
        if anchor_id not in by_id:
            return []

        # Backward walk over metadata, then chronological order.
        chain: list[tuple[str, str | None]] = []
        cid: str | None = anchor_id
        while cid is not None:
            row = by_id.get(cid)
            if row is None:
                break
            chain.append((cid, row["messages_version"]))
            if cid == stop_at_checkpoint_id:
                break
            cid = row["parent_checkpoint_id"]
        chain.reverse()

        chain_versions = sorted({v for _, v in chain if v is not None})
        if not chain_versions:
            return []

        await cur.execute(_LEN_SQL, (thread_id, chain_versions))
        sizes = {r["version"]: r["len"] async for r in cur}

        await cur.execute(_REMOVAL_SQL, (thread_id, b"RemoveMessage"))
        removal_cids = {r["checkpoint_id"] async for r in cur}

        # Versions to fetch: the anchor's, plus the (pre, post) pair around
        # every point flagged by either selector — the removal oracle (exact)
        # or a blob-size drop (serde-drift insurance).
        index_of = {c: i for i, (c, _) in enumerate(chain)}
        fetch: set[str] = set()
        anchor_ver = chain[-1][1]
        if anchor_ver is not None:
            fetch.add(anchor_ver)

        def flag_pair(i: int) -> None:
            for j in (i, i + 1):
                if 0 <= j < len(chain):
                    ver = chain[j][1]
                    if ver is not None:
                        fetch.add(ver)

        for p_cid in removal_cids:
            i = index_of.get(p_cid)
            if i is not None:
                flag_pair(i)
        for i in range(len(chain) - 1):
            v_a, v_b = chain[i][1], chain[i + 1][1]
            if v_a is None or v_b is None or v_a == v_b:
                continue
            if sizes.get(v_b, 0) < sizes.get(v_a, 0):
                flag_pair(i)

        await cur.execute(_BLOBS_SQL, (thread_id, sorted(fetch)))
        by_version: dict[str, list[BaseMessage]] = {}
        async for r in cur:
            if r["blob"] is None or r["type"] == "empty":
                by_version[r["version"]] = []
            else:
                by_version[r["version"]] = cast(
                    list[BaseMessage],
                    saver.serde.loads_typed((r["type"], r["blob"])),
                )

    # History = fetched snapshots in chronological order, at the oldest
    # checkpoint of each run of consecutive equal versions. Unfetched versions
    # are safe to omit: nothing was removed at them (neither selector fired),
    # so their content is a subset of the next fetched snapshot.
    history: _History = []
    prev_ver: str | None = None
    for c, v in chain:
        if v is not None and v != prev_ver and v in by_version:
            history.append((c, by_version[v]))
        prev_ver = v
    return history


def _fold_history(history: _History) -> list[tuple[TimelineItem, str | None]]:
    """Dedup + summarization-marker fold over an oldest-first history."""
    timeline: list[tuple[TimelineItem, str | None]] = []
    seen_ids: set[str] = set()
    prev_ids: set[str] = set()

    for cid, msgs in history:
        curr_ids = {m_id for m in msgs if (m_id := getattr(m, "id", None)) is not None}

        # Disjoint id sets between non-empty checkpoints == summarization.
        # The intersection check rules out normal checkpoint-to-checkpoint
        # shrinkage (single RemoveMessage), which preserves overlap.
        if prev_ids and curr_ids and prev_ids.isdisjoint(curr_ids):
            timeline.append((SummarizationMarker(checkpoint_id=cid), None))

        for m in msgs:
            mid = getattr(m, "id", None)
            if mid is not None and mid in seen_ids:
                continue
            if mid is not None:
                seen_ids.add(mid)
            timeline.append((m, cid))

        prev_ids = curr_ids

    return timeline


async def load_timeline(
    checkpointer: BaseCheckpointSaver,
    thread_id: str,
    *,
    anchor_checkpoint_id: str | None = None,
    stop_at_checkpoint_id: str | None = None,
) -> list[tuple[TimelineItem, str | None]]:
    """Walk ``thread_id``'s checkpoint chain backward from ``anchor_checkpoint_id``
    (or the latest checkpoint when None), returning a chronological timeline.

    Each entry is paired with the id of the checkpoint whose snapshot first
    surfaced it (``None`` for ``SummarizationMarker``). On the generic path
    that is the checkpoint that first persisted the message; on the Postgres
    fast path it is coarsened to the nearest *fetched* snapshot's checkpoint.
    No current consumer reads this field (the renderers ignore it); marker
    checkpoint ids are exact on both paths.

    ``stop_at_checkpoint_id``: when set, the walk halts at that checkpoint
    (inclusive), so the returned timeline covers exactly the segment that
    began there. Used by ``ap-trail view`` to render one ThreadMeta segment
    at a time when a thread was resumed/re-entered.

    Summarization detection: a checkpoint is a summarization point iff its
    message-id set is disjoint from the previous checkpoint's non-empty
    message-id set. The summarizer ``RemoveMessages`` everything and inserts
    a fresh system+initial+resume triple, so the disjoint-id signature is
    exact. Normal single-message removals (which keep most ids) don't trip it.
    """
    if isinstance(checkpointer, AsyncPostgresSaver):
        history = await _load_history_pg(
            checkpointer, thread_id, anchor_checkpoint_id, stop_at_checkpoint_id
        )
    else:
        history = await _load_history_generic(
            checkpointer, thread_id, anchor_checkpoint_id, stop_at_checkpoint_id
        )
    return _fold_history(history)
