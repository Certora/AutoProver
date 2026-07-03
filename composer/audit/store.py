"""
Audit archive backed by the LangGraph store.

Replaces the bespoke PostgreSQL audit DB (file_blobs + run_info / vfs_*
tables) with JSONB values under a small set of namespaces; no
content-addressed blob store, no gzip. Scope is deliberately narrow —
the data needed to *resume* a prior run, plus run-lifecycle metadata for
locating runs by label. (Prover / manual-search / summary trace data is
no longer persisted: its only consumers were the retired trace tools.)

Namespaces (each prefixed by ``user_data_ns(uid)`` for per-user tenancy):

    ("audit_runs",)        / thread_id          → StoredRunMeta
    ("audit", tid)         / "run_info"         → StoredRunInfo
    ("audit", tid)         / "resume_artifact"  → StoredResumeArtifact
    ("audit", tid)         / "vfs_initial"      → StoredVFS
    ("audit", tid)         / "vfs_result"       → StoredVFS

The ``audit_runs`` namespace is intentionally flat (within a user) so callers can list
every registered run without enumerating thread ids — useful for
description-based lookups after a crash, where the thread id is lost but
the human-supplied label survives.

Spec / interface / system-doc contents are inlined into StoredRunInfo
(small, always read together with the filenames, no separate blob store).

Audit-side document handles (``_StoredText``, ``_StoredBinary``,
``ResumeSpecEntry``) satisfy the ``Uploadable`` protocol and nothing
more. On resume, the executor feeds them through its ``FileUploader`` to
materialize real ``Document`` / ``TextDocument`` instances for the active
provider; the audit store has no opinion about the backend.
"""


import base64
import logging
import pathlib
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import cached_property
from typing import Iterable, Iterator, Literal, TypedDict, cast

from langgraph.store.base import BaseStore

from composer.audit.types import RunInput, SpecRunEntry
from composer.core.user import user_data_ns
from composer.input.files import Document, TextDocument


# ---------------------------------------------------------------------------
# Stored value shapes
# ---------------------------------------------------------------------------


class StoredSpecFile(TypedDict):
    vfs_path: str
    basename: str
    contents: str


class StoredSystemBinary(TypedDict):
    """On-disk shape for a binary system document. The text-or-binary
    classification happens once at ``register_run`` time (based on whether
    the source's ``string_contents`` is non-None); on read we dispatch on
    whether ``system`` is a string or a dict."""
    type: Literal["b64"]
    contents: str


class StoredRunInfo(TypedDict):
    spec: StoredSpecFile
    interface_name: str
    interface_contents: str
    system_name: str
    # Text body if the system doc was UTF-8 text upstream; otherwise the
    # binary variant.
    system: str | StoredSystemBinary
    reqs: list[str] | None


class StoredResumeArtifact(TypedDict):
    interface_path: str
    commentary: str


class StoredVFS(TypedDict):
    files: dict[str, str]


class StoredRunMeta(TypedDict):
    """Run-lifecycle metadata distinct from the run's inputs.

    Lives in its own audit slot so additive fields (parent thread,
    completion time, run kind, etc.) can land without bumping
    ``StoredRunInfo``'s version. Optional ``description`` is free-form
    user-supplied text — searchable so a run can be located later by label
    after a crash, even when the thread id has been lost."""
    started_at: str  # ISO 8601, UTC.
    description: str | None


# ---------------------------------------------------------------------------
# In-memory file views — audit-restored ``Uploadable`` carriers.
#
# These satisfy the ``Uploadable`` protocol (basename + bytes_contents +
# optional string_contents) and nothing more. Callers feed them to the
# workflow's ``FileUploader`` to get back real ``Document`` / ``TextDocument``
# instances for the active provider.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _StoredText:
    """Audit-restored text source. Bytes view is the UTF-8 encoding of
    ``string_contents``; satisfies ``TextUploadable``."""

    path: str
    contents: str

    @property
    def basename(self) -> str:
        return pathlib.Path(self.path).name

    @property
    def bytes_contents(self) -> bytes:
        return self.contents.encode("utf-8")

    @property
    def string_contents(self) -> str:
        return self.contents


@dataclass(frozen=True)
class _StoredBinary:
    """Audit-restored binary source. The text-or-binary call was made at
    ``register_run`` time and baked into the storage schema, so this type is
    *honestly* binary — ``string_contents`` always returns ``None``."""

    path: str
    contents_b64: str

    @property
    def basename(self) -> str:
        return pathlib.Path(self.path).name

    @property
    def bytes_contents(self) -> bytes:
        return base64.standard_b64decode(self.contents_b64)

    @property
    def string_contents(self) -> None:
        return None


# ---------------------------------------------------------------------------
# VFS retriever — iterates a flat path→content map
# ---------------------------------------------------------------------------


@dataclass
class VFSRetriever:
    _files: dict[str, str]

    def to_dict(self) -> dict[str, bytes]:
        return {p: c.encode("utf-8") for (p, c) in self._files.items()}

    def __iter__(self) -> Iterator[tuple[str, bytes]]:
        for p, c in self._files.items():
            yield (p, c.encode("utf-8"))

    def get_file(self, p: str) -> _StoredText | None:
        c = self._files.get(p)
        if c is None:
            return None
        return _StoredText(path=p, contents=c)

    def __getitem__(self, p: str) -> _StoredText | None:
        return self.get_file(p)


# ---------------------------------------------------------------------------
# Resume artifact
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResumeSpecEntry:
    """A single spec file as captured on the completed run. ``vfs_path`` is
    what the executor needs to re-overlay the spec into the resumed state;
    ``contents`` is what was there at completion time (before any resume-time
    updates). Satisfies ``TextUploadable``."""
    vfs_path: str
    basename: str
    contents: str

    @property
    def string_contents(self) -> str:
        return self.contents

    @property
    def bytes_contents(self) -> bytes:
        return self.contents.encode("utf-8")


class ResumeArtifact:
    """Bundle of everything needed to resume a prior run: the final interface
    and system views, the spec file that was in play at completion, the full
    final VFS, and the commentary the LLM attached on completion.

    All file-shaped fields satisfy ``Uploadable``; the executor passes them
    through its ``FileUploader`` on resume to rehydrate into ``Document`` /
    ``TextDocument`` instances for the active provider."""

    def __init__(
        self,
        final_intf: _StoredText,
        spec_entry: ResumeSpecEntry,
        system_doc: "_StoredText | _StoredBinary",
        commentary: str,
        intf_path: str,
        vfs_cur: VFSRetriever,
    ):
        self.intf_vfs_handle = final_intf
        self.spec = spec_entry
        self.system_vfs_handle: "_StoredText | _StoredBinary" = system_doc
        self.vfs = vfs_cur
        self.commentary = commentary
        self.interface_path = intf_path

    @cached_property
    def interface_file(self) -> str:
        return self.intf_vfs_handle.string_contents

    @cached_property
    def system_doc(self) -> str | None:
        """Text body of the original system doc, or ``None`` if it was a
        binary input (e.g. PDF)."""
        return self.system_vfs_handle.string_contents


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_dict(
    x: StoredRunInfo | StoredResumeArtifact | StoredRunMeta | StoredVFS,
) -> dict:
    return {**x}


def _decode_text_files(items: Iterable[tuple[str, bytes]]) -> dict[str, str]:
    """Decode VFS bytes as UTF-8, dropping anything binary.

    ``StoredVFS`` is a flat ``{path: str}`` dict — no place for binary blobs.
    Sources routinely contain binary files (images, pdf, git blobs); skip them
    with a log line and keep going. Resume won't restore those binaries, but
    they weren't going to round-trip through a JSONB string column anyway."""
    logger = logging.getLogger(__name__)
    out: dict[str, str] = {}
    skipped: list[str] = []
    for path, content in items:
        try:
            out[path] = content.decode("utf-8")
        except UnicodeDecodeError:
            skipped.append(path)
    if skipped:
        logger.info(
            "Skipped %d binary file(s) when persisting VFS: %s",
            len(skipped),
            ", ".join(skipped[:10]) + (f" (+{len(skipped) - 10} more)" if len(skipped) > 10 else ""),
        )
    return out


_RUN_INFO_KEY = "run_info"
_RUN_META_SUFFIX: tuple[str, ...] = ("audit_runs",)
_VFS_INITIAL_KEY = "vfs_initial"
_VFS_RESULTS_KEY = "vfs_result"
_RESUME_ARTIFACT_KEY = "resume_artifact"


def _system_handle(system_name: str, stored_system: str | StoredSystemBinary) -> "_StoredText | _StoredBinary":
    """Dispatch the stored ``system`` slot to the right ``Uploadable`` carrier."""
    if isinstance(stored_system, str):
        return _StoredText(path=system_name, contents=stored_system)
    return _StoredBinary(path=system_name, contents_b64=stored_system["contents"])


# ---------------------------------------------------------------------------
# AuditStore
# ---------------------------------------------------------------------------


class AuditStore:
    """Async accessor for the audit archive.

    Wraps a ``BaseStore`` (typically the workflow's ``AsyncPostgresStore``);
    all reads/writes use the ``a*`` methods, so callers must be async."""

    def __init__(self, store: BaseStore, uid: str | None = None):
        self._store = store
        self._uid = uid

    def _ns(self, thread_id: str, *extra: str) -> tuple[str, ...]:
        return user_data_ns(self._uid) + ("audit", thread_id, *extra)

    def _run_meta_ns(self) -> tuple[str, ...]:
        return user_data_ns(self._uid) + _RUN_META_SUFFIX

    # -- run registration --------------------------------------------------

    async def register_run(
        self,
        thread_id: str,
        spec_vfs_path: str,
        spec_file: TextDocument,
        interface_file: TextDocument,
        system_doc: Document,
        vfs_init: Iterable[tuple[str, bytes]],
        reqs: list[str] | None,
        description: str | None = None,
    ) -> None:
        """``spec_vfs_path`` is where the spec lives in the VFS (codegen's
        historical convention is ``rules.spec``).

        Spec/interface contents persist as plain strings (text-guaranteed
        upstream). The system doc is classified once here: ``string_contents``
        non-None lands as a plain string, otherwise a ``{"type": "b64", ...}``
        binary record. ``description`` is free-form user-supplied text recorded
        on the ``run_meta`` slot so callers can find a run by name after the
        thread id has been lost."""
        stored_spec: StoredSpecFile = {
            "vfs_path": spec_vfs_path,
            "basename": spec_file.basename,
            "contents": spec_file.string_contents,
        }
        system_text = system_doc.string_contents
        stored_system: str | StoredSystemBinary
        if system_text is not None:
            stored_system = system_text
        else:
            stored_system = {
                "type": "b64",
                "contents": base64.standard_b64encode(system_doc.bytes_contents).decode("utf-8"),
            }
        run_info: StoredRunInfo = {
            "spec": stored_spec,
            "interface_name": interface_file.basename,
            "interface_contents": interface_file.string_contents,
            "system_name": system_doc.basename,
            "system": stored_system,
            "reqs": reqs,
        }
        vfs_payload: StoredVFS = {"files": _decode_text_files(vfs_init)}
        run_meta: StoredRunMeta = {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "description": description,
        }

        await self._store.aput(self._ns(thread_id), _RUN_INFO_KEY, _safe_dict(run_info))
        await self._store.aput(self._run_meta_ns(), thread_id, _safe_dict(run_meta))
        await self._store.aput(self._ns(thread_id), _VFS_INITIAL_KEY, _safe_dict(vfs_payload))

    async def register_complete(
        self,
        thread_id: str,
        vfs: Iterable[tuple[str, bytes]],
        intf: str,
        commentary: str,
    ) -> None:
        vfs_payload: StoredVFS = {"files": _decode_text_files(vfs)}
        await self._store.aput(self._ns(thread_id), _VFS_RESULTS_KEY, _safe_dict(vfs_payload))

        resume: StoredResumeArtifact = {
            "interface_path": intf,
            "commentary": commentary,
        }
        await self._store.aput(self._ns(thread_id), _RESUME_ARTIFACT_KEY, _safe_dict(resume))

    # -- reads -------------------------------------------------------------

    async def get_resume_artifact(self, thread_id: str) -> ResumeArtifact:
        ra_item = await self._store.aget(self._ns(thread_id), _RESUME_ARTIFACT_KEY)
        if ra_item is None:
            raise RuntimeError(f"No resume artifact found for thread {thread_id}")
        ra = cast(StoredResumeArtifact, ra_item.value)

        ri_item = await self._store.aget(self._ns(thread_id), _RUN_INFO_KEY)
        if ri_item is None:
            raise RuntimeError(f"No run info found for thread {thread_id}")
        ri = cast(StoredRunInfo, ri_item.value)

        vfs_item = await self._store.aget(self._ns(thread_id), _VFS_RESULTS_KEY)
        if vfs_item is None:
            raise RuntimeError(f"No vfs_result found for thread {thread_id}")
        vfs_files = cast(StoredVFS, vfs_item.value)["files"]

        intf_contents = vfs_files.get(ra["interface_path"])
        if intf_contents is None:
            raise RuntimeError(
                f"Resume artifact references {ra['interface_path']} but it's not in vfs_result"
            )

        # Pull the registered spec's *final* contents from the completed VFS.
        # Missing is a hard error — it was present at registration time.
        stored_spec = ri["spec"]
        spec_vfs_path = stored_spec["vfs_path"]
        final_spec_contents = vfs_files.get(spec_vfs_path)
        if final_spec_contents is None:
            raise RuntimeError(
                f"vfs_result for thread {thread_id} has no file at {spec_vfs_path!r} "
                f"(registered as the spec at run start)"
            )
        spec_entry = ResumeSpecEntry(
            vfs_path=spec_vfs_path,
            basename=stored_spec["basename"],
            contents=final_spec_contents,
        )

        return ResumeArtifact(
            final_intf=_StoredText(path=ra["interface_path"], contents=intf_contents),
            spec_entry=spec_entry,
            system_doc=_system_handle(ri["system_name"], ri["system"]),
            commentary=ra["commentary"],
            intf_path=ra["interface_path"],
            vfs_cur=VFSRetriever(_files=vfs_files),
        )

    async def get_run_info(self, thread_id: str) -> tuple[RunInput, VFSRetriever]:
        ri_item = await self._store.aget(self._ns(thread_id), _RUN_INFO_KEY)
        if ri_item is None:
            raise RuntimeError(f"Didn't find run info for {thread_id}")
        ri = cast(StoredRunInfo, ri_item.value)

        vfs_item = await self._store.aget(self._ns(thread_id), _VFS_INITIAL_KEY)
        vfs_files: dict[str, str] = {}
        if vfs_item is not None:
            vfs_files = cast(StoredVFS, vfs_item.value)["files"]
        retriever = VFSRetriever(_files=vfs_files)

        stored_spec = ri["spec"]
        run_spec: SpecRunEntry = {
            "vfs_path": stored_spec["vfs_path"],
            "basename": stored_spec["basename"],
            "contents": stored_spec["contents"],
        }
        run_input: RunInput = {
            "interface": _StoredText(path=ri["interface_name"], contents=ri["interface_contents"]),
            "spec": run_spec,
            "system": _system_handle(ri["system_name"], ri["system"]),
            "reqs": ri["reqs"],
        }
        return (run_input, retriever)

    async def get_run_meta(self, thread_id: str) -> StoredRunMeta | None:
        """Run-lifecycle metadata for ``thread_id``, or ``None`` for runs
        registered before the meta slot existed."""
        item = await self._store.aget(self._run_meta_ns(), thread_id)
        if item is None:
            return None
        return cast(StoredRunMeta, item.value)

    async def list_run_meta(self, limit: int = 1000) -> list[tuple[str, StoredRunMeta]]:
        """``(thread_id, meta)`` for every registered run, newest first.

        Cheap cross-run query backed by the flat ``audit_runs`` namespace —
        useful for crash-recovery lookups by description. Bring the data home
        and filter in Python; the namespace is small."""
        items = await self._store.asearch(self._run_meta_ns(), limit=limit)
        out = [(item.key, cast(StoredRunMeta, item.value)) for item in items]
        out.sort(key=lambda pair: pair[1].get("started_at", ""), reverse=True)
        return out
