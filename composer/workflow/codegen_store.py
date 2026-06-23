"""Structured, user-scoped per-run codegen persistence.

One typed ``BaseStore`` wrapper for the data a codegen run stashes — extracted
requirements (keyed by thread id) and crash-recovery snapshots (keyed by an
opaque recovery key) — each under its own sub-namespace, all prefixed by
``user_data_ns`` so codegen doesn't assume it owns the whole keyspace (the
spec / autoprove workflows scope their store data the same way). Replaces the
ad-hoc top-level splats (``(thread_id,)`` for reqs, ``("crash_recovery",)`` for
snapshots); new run-scoped state goes through here too.
"""

from dataclasses import dataclass

from langgraph.store.base import BaseStore

from composer.core.user import user_data_ns


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

    async def save_recovery(self, key: str, vfs: dict[str, str]) -> None:
        await self.store.aput(self._ns(_RECOVERY_SUFFIX), key, {"vfs": vfs})

    async def recovery(self, key: str) -> dict[str, str] | None:
        """The VFS snapshot saved under ``key``, or ``None`` if absent."""
        item = await self.store.aget(self._ns(_RECOVERY_SUFFIX), key)
        return None if item is None else item.value["vfs"]
