from dataclasses import dataclass
import hashlib
from typing import cast
from langgraph.store.base import BaseStore


@dataclass(frozen=True)
class MungeEditor:
    """The munge editor sub-agent, commissioned through the author's
    edit-request tool. Every record written before attribution existed has
    this provenance — it was the only committer."""


@dataclass(frozen=True)
class PluginEditor:
    """A pipeline plugin proposed this edit through the edit store staged into
    its contributed tools; the CVL author still decides whether to apply it."""
    plugin: str


type EditAttribution = MungeEditor | PluginEditor


def _attribution_payload(a: EditAttribution) -> dict:
    match a:
        case MungeEditor():
            return {"kind": "munge-editor"}
        case PluginEditor(plugin=plugin):
            return {"kind": "plugin", "plugin": plugin}


def _attribution_of(v: object) -> EditAttribution:
    match v:
        case None:
            return MungeEditor()
        case {"kind": "munge-editor"}:
            return MungeEditor()
        case {"kind": "plugin", "plugin": str(plugin)}:
            return PluginEditor(plugin=plugin)
        case _:
            raise ValueError(f"Unrecognized edit attribution payload: {v!r}")


@dataclass(frozen=True)
class StoredEdit:
    """A committed edit: the full VFS snapshot, the committer's account of it,
    and who committed it. The description fields ride into the edit-history log
    and the final deliverable, so a reader can tell why the source was changed
    without reconstructing the diff; ``attribution`` says on whose authority."""
    vfs: dict[str, str]
    executive_summary: str
    why_sound: str
    attribution: EditAttribution


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
            attribution=_attribution_of(v.get("attribution")),
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
        self, vfs: dict[str, str], *, executive_summary: str, why_sound: str,
        attribution: EditAttribution,
    ) -> str:
        id = self._deterministic_hash(vfs)
        await self._store.aput(self._target_ns, id, {
            "vfs": {**vfs},
            "executive_summary": executive_summary,
            "why_sound": why_sound,
            "attribution": _attribution_payload(attribution),
        })
        return id
