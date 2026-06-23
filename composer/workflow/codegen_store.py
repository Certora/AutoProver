"""Structured, user-scoped per-run codegen persistence.

One typed ``BaseStore`` wrapper for the data a codegen run stashes — extracted
requirements (keyed by thread id) and crash-recovery snapshots (keyed by an
opaque recovery key) — each under its own sub-namespace, all prefixed by
``user_data_ns`` so codegen doesn't assume it owns the whole keyspace (the
spec / autoprove workflows scope their store data the same way). Replaces the
ad-hoc top-level splats (``(thread_id,)`` for reqs, ``("crash_recovery",)`` for
snapshots); new run-scoped state goes through here too.
"""

import uuid
from dataclasses import dataclass
from typing import TypedDict, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.store.base import BaseStore

from composer.core.state import AIComposerState
from composer.core.user import user_data_ns


class VFSRecovery(TypedDict):
    """A crash-recovery snapshot: the agent-authored VFS overlay plus any
    in-flight working-spec draft. Not the message history / langgraph
    internals — the checkpoint remains the source of truth for a full resume;
    this is the "salvage uncommitted work" path."""
    vfs: dict[str, str]
    working_spec: str | None


_REQUIREMENTS_SUFFIX: tuple[str, ...] = ("codegen", "requirements")
_RECOVERY_SUFFIX: tuple[str, ...] = ("codegen", "crash_recovery")


@dataclass(frozen=True)
class CodegenStore:
    """Typed ``BaseStore`` wrapper for a codegen run's persisted state, scoped
    under ``user_data_ns(uid)`` (``uid=None`` resolves to the current user)."""

    store: BaseStore
    uid: str | None = None

    def _ns(self, suffix: tuple[str, ...]) -> tuple[str, ...]:
        return user_data_ns(self.uid) + suffix

    # -- extracted requirements (keyed by thread id) -------------------------

    async def record_requirements(self, thread_id: str, reqs: list[str] | None) -> None:
        await self.store.aput(self._ns(_REQUIREMENTS_SUFFIX), thread_id, {"reqs": reqs})

    async def requirements(self, thread_id: str) -> list[str] | None:
        """Recorded requirements for ``thread_id``, or ``None`` to (re)compute —
        covering both "never extracted" and a stored ``None`` (``--skip-reqs``),
        which is cheap and deterministic to recompute."""
        item = await self.store.aget(self._ns(_REQUIREMENTS_SUFFIX), thread_id)
        return None if item is None else item.value["reqs"]

    # -- crash-recovery VFS snapshots (keyed by recovery key) ----------------

    async def save_recovery(self, key: str, recovery: VFSRecovery) -> None:
        await self.store.aput(self._ns(_RECOVERY_SUFFIX), key, {**recovery})

    async def recovery(self, key: str) -> VFSRecovery | None:
        """The crash snapshot saved under ``key``, or ``None`` if absent
        (stale link, already cleaned up, etc.)."""
        item = await self.store.aget(self._ns(_RECOVERY_SUFFIX), key)
        if item is None:
            return None
        return {
            "vfs": item.value["vfs"],
            "working_spec": item.value.get("working_spec"),
        }

    async def recovery_from_thread(
        self, checkpointer: BaseCheckpointSaver, thread_id: str
    ) -> str | None:
        """Pull the latest checkpoint's VFS overlay + working-spec draft for
        ``thread_id``, mint a fresh resume key, stash the snapshot, and return
        the key (or ``None`` if there's no checkpoint to recover from)."""
        ct = await checkpointer.aget_tuple({"configurable": {"thread_id": thread_id}})
        if ct is None:
            return None
        channel_values = cast(AIComposerState, ct.checkpoint["channel_values"])
        resume_key = f"crash_{thread_id}_{uuid.uuid4().hex[:8]}"
        await self.save_recovery(resume_key, {
            "vfs": channel_values["vfs"],
            "working_spec": channel_values.get("working_spec"),
        })
        return resume_key
