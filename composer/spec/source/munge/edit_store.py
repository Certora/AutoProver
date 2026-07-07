from dataclasses import dataclass
import hashlib
from typing import cast
from langgraph.store.base import BaseStore


@dataclass(frozen=True)
class StoredEdit:
    """A committed edit: the full VFS snapshot plus the editor's account of it.
    The description fields ride into the edit-history log and the final
    deliverable, so a reader can tell why the source was changed without
    reconstructing the diff."""
    vfs: dict[str, str]
    executive_summary: str
    why_sound: str


@dataclass
class EditStore:
    _store: BaseStore
    _target_ns: tuple[str, ...]

    async def read(self, id: str) -> StoredEdit | None:
        res = await self._store.aget(self._target_ns, id)
        if res is None:
            return None
        v = res.value
        return StoredEdit(
            vfs=cast(dict[str, str], v["vfs"]),
            executive_summary=cast(str, v["executive_summary"]),
            why_sound=cast(str, v["why_sound"]),
        )

    @classmethod
    def _deterministic_hash(cls, vfs: dict[str, str]) -> str:
        sorted_keys = sorted(vfs.keys())
        hasher = hashlib.sha256()
        for nm in sorted_keys:
            hasher.update(vfs[nm].encode("utf-8"))
            hasher.update(b'\0')
        return hasher.hexdigest()

    async def commit(
        self, vfs: dict[str, str], *, executive_summary: str, why_sound: str
    ) -> str:
        id = self._deterministic_hash(vfs)
        await self._store.aput(self._target_ns, id, {
            "vfs": {**vfs},
            "executive_summary": executive_summary,
            "why_sound": why_sound,
        })
        return id
