from dataclasses import dataclass
import hashlib
from typing import cast
from langgraph.store.base import BaseStore

@dataclass
class EditStore:
    _store: BaseStore
    _target_ns: tuple[str, ...]

    async def read(self, id: str) -> dict[str, str] | None:
        res =  await self._store.aget(self._target_ns, id)
        if res is None:
            return None
        return cast(dict[str, str], res.value)

    @classmethod
    def deterministic_hash(cls, vfs: dict[str, str]) -> str:
        sorted_keys = sorted(vfs.keys())
        hasher = hashlib.sha256()
        for nm in sorted_keys:
            hasher.update(vfs[nm].encode("utf-8"))
            hasher.update(b'\0')
        return hasher.hexdigest()

    async def commit(self, vfs: dict[str, str]) -> str:
        id = self.deterministic_hash(vfs)
        await self._store.aput(self._target_ns, id, {**vfs})
        return id
