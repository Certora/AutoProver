"""Files-API-backed file shapes + uploader + Document protocols.

Public surface:

- ``Document`` / ``TextDocument`` — what downstream consumers depend on.
- ``FileUploader`` — Protocol for the upload+dedup contract.
  ``_UploaderBase`` is the shared upload-or-reuse logic; the concrete
  per-provider impls live in ``composer/llm/{anthropic,openai}.py`` and
  are obtained (lazily seeded) via a ``ModelProvider.uploader()``.
- ``InMemoryTextFile``, ``UploadedFile``, ``UploadedTextFile`` —
  concrete document shapes. Implementation details: callers should
  declare protocol-typed parameters and not branch on these. Each
  carries the provider it was minted under so ``to_dict()`` dispatches
  the right content-block shape internally.

Policy (current): only binary files go through the Files API. Text
files stay inline as ``InMemoryTextFile`` so they remain visible in the
prompt when a conversation is later debugged. Very-large text files
that should still be uploaded can be loaded explicitly via
:meth:`FileUploader.upload_text_file_if_needed`.
"""


import asyncio
import hashlib
import io
import mimetypes
import os
import pathlib
import zlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Protocol, assert_never, Any, overload

from composer.llm.provider import ProviderKind

# ---------------------------------------------------------------------------
# Protocols (the public surface)
# ---------------------------------------------------------------------------


class Uploadable(Protocol):
    """Raw content destined for (re)upload: basename + bytes, with text
    available iff the source is UTF-8 text. Distinct from ``Document`` — it
    carries no ``to_dict``/``to_digest`` and is not directly renderable.
    Audit-restored handles are ``Uploadable``; feed them through
    :meth:`FileUploader.document_from` to get a renderable ``Document`` for
    the active provider."""

    @property
    def basename(self) -> str: ...
    @property
    def bytes_contents(self) -> bytes: ...
    @property
    def string_contents(self) -> str | None: ...


class TextUploadable(Uploadable, Protocol):
    """Refinement of ``Uploadable`` whose body is guaranteed text."""

    @property
    def string_contents(self) -> str: ...


class Document(Uploadable, Protocol):
    """A piece of content destined for an LLM message."""

    def to_dict(self, with_cache: bool = False) -> dict: ...
    def to_digest(self) -> str: ...


class TextDocument(Document, Protocol):
    """Refinement of ``Document`` whose body is guaranteed text."""

    @property
    def string_contents(self) -> str: ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bytes_digest(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


# Suffixes treated as binary regardless of byte content; short-circuits
# the byte-scan heuristic for common cases.
_KNOWN_BINARY_SUFFIXES = {".pdf"}

_KNOWN_TEXT_SUFFIXES = {".md", ".txt", ".sol", ".spec", ".conf"}

# Bytes scanned by the binary heuristic. Standard git/grep-I trick: a
# NUL byte in the first 8 KiB means binary.
_BINARY_SNIFF_BYTES = 8 * 1024


def _is_binary_file(suffix: str, file_data: bytes) -> bool:
    """True if ``path`` should be treated as binary at upload time."""
    suffix = suffix.lower()
    if suffix in _KNOWN_BINARY_SUFFIXES:
        return True
    elif suffix in _KNOWN_TEXT_SUFFIXES:
        return False
    return b"\x00" in file_data[:_BINARY_SNIFF_BYTES]


# ---------------------------------------------------------------------------
# Concrete shapes (implementation details — declare protocol types instead)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InMemoryTextFile:
    """Text content carried inline in the request. Produced by the
    default text-file path so the content stays visible in conversation
    transcripts.

    ``provider`` is the LLM family this body was minted for; the text
    content-part shape happens to be identical on Anthropic and OpenAI,
    but the field is kept here for symmetry with the uploaded shapes."""

    basename: str
    string_contents: str
    provider: ProviderKind

    @property
    def bytes_contents(self) -> bytes:
        return self.string_contents.encode("utf-8")

    def to_dict(self, with_cache: bool = False) -> dict:
        to_ret : dict[str, Any] = {"type": "text", "text": self.string_contents}
        if with_cache:
            to_ret["cache_control"] = {
                "type": "ephemeral",
                "ttl": "5m"
            }
        return to_ret

    def to_digest(self) -> str:
        return _bytes_digest(self.bytes_contents)


@dataclass(frozen=True)
class UploadedFile:
    """A (potentially-binary) file uploaded to the Files API. Bytes are
    cached in memory so ``bytes_contents`` / ``string_contents`` don't
    re-read from disk and survive whatever the local filesystem looks
    like later.

    ``provider`` identifies which provider's Files API minted the
    ``file_id``; ``to_dict`` dispatches the right content-block shape."""

    file_id: str
    basename: str
    contents: bytes
    digest: str
    provider: ProviderKind

    def to_dict(self, with_cache: bool = False) -> dict:
        match self.provider:
            case "anthropic":
                to_ret : dict[str, Any] = {
                    "type": "document",
                    "source": {
                        "type": "file",
                        "file_id": self.file_id,
                    },
                }
                if with_cache:
                    to_ret["cache_control"] = {
                        "type": "ephemeral",
                        "ttl": "5m"
                    }
                return to_ret
            case "openai":
                return {
                    "type": "file",
                    "file": {
                        "file_id": self.file_id,
                    },
                }
            case _:
                assert_never(self.provider)

    def to_digest(self) -> str:
        return self.digest

    @property
    def string_contents(self) -> str | None:
        try:
            return self.contents.decode("utf-8")
        except UnicodeDecodeError:
            return None

    @property
    def bytes_contents(self) -> bytes:
        return self.contents


@dataclass(frozen=True)
class UploadedTextFile(UploadedFile):
    """A Files-API upload that was classified as text at upload time.
    ``string_contents`` is guaranteed non-None. Produced by
    :meth:`FileUploader.upload_text_file_if_needed` for the
    very-large-text case where inlining the body would blow the prompt
    budget."""

    @property
    def string_contents(self) -> str:
        return self.contents.decode("utf-8")


# ---------------------------------------------------------------------------
# Uploader Protocol + shared base
# ---------------------------------------------------------------------------

@dataclass
class _FileData:
    basename: str
    raw_data: bytes
    is_binary: bool
    mime: str
    crc_basename: str
    digest: str

@overload
async def _file_data(
    *,
    path: str | pathlib.Path
) -> _FileData:
    ...

@overload
async def _file_data(
    *,
    basename: str, data: bytes
) -> _FileData:
    ...

async def _file_data(
    path: str | pathlib.Path | None = None,
    basename: str | None = None,
    data: bytes | None = None
) -> _FileData:
    return await asyncio.to_thread(_file_data_impl, path, basename, data)

def _file_data_impl(
    path: str | pathlib.Path | None,
    basename: str | None,
    data: bytes | None
) -> _FileData:
    if path is not None:
        if isinstance(path, str):
            path = pathlib.Path(path)
        basename = path.name
        with open(str(path), "rb") as f:
            data = f.read()
        suffix = path.suffix
    else:
        assert basename is not None
        suffix = pathlib.Path(basename).suffix
    assert data is not None
    guessed, _ = mimetypes.guess_type(basename)
    is_binary = _is_binary_file(suffix, data)
    if guessed is not None:
        if guessed.startswith("text/"):
            mime = "text/plain"
        else:
            mime = guessed
    else:
        mime = "application/octet-stream" if is_binary else "text/plain"
    crc = hex(zlib.crc32(data))
    digest = _bytes_digest(data)
    return _FileData(raw_data=data, is_binary=is_binary, mime=mime, crc_basename=f"{crc}_{basename}", digest=digest, basename=basename)

class FileUploader(Protocol):
    """Upload+dedup contract. Obtain via ``ModelProvider.uploader()`` (``composer.llm``)."""

    provider: ProviderKind

    async def upload_file_if_needed(
        self, file_path: str | pathlib.Path
    ) -> UploadedFile: ...

    async def upload_text_file_if_needed(
        self, file_path: str | pathlib.Path
    ) -> UploadedTextFile: ...

    async def get_document(
        self, path: str | pathlib.Path
    ) -> Document | None: ...


    def text_document_from(self, src: TextUploadable) -> TextDocument:
        ...

    async def document_from(self, src: Uploadable) -> Document:
        ...



class _UploaderBase(ABC):
    """Shared upload-or-reuse logic. Subclasses supply
    :meth:`_upload_bytes` (the provider-specific API call) and set
    ``provider`` at construction so it's stamped onto returned
    documents.

    The dedup cache lives in ``self.uploaded`` (CRC-prefixed filename →
    remote file id) and is seeded by each subclass's ``fresh`` factory
    so we don't reupload a file whose bytes the account has already
    seen."""

    provider: ProviderKind

    @abstractmethod
    async def _upload_bytes(
        self, crc_basename: str, file_data: bytes, mime: str
    ) -> str:
        ...

    async def upload_file_if_needed(
        self, file_path: str | pathlib.Path
    ) -> UploadedFile:
        """Upload ``file_path`` (or reuse cached upload). Intended for
        binary inputs — callers that know they have text should prefer
        :meth:`get_document` (default text-inline) or
        :meth:`upload_text_file_if_needed` (explicit upload of text)."""
        data = await _file_data(path=file_path)
        file_id = await self._upload_bytes(data.crc_basename, data.raw_data, data.mime)
        return UploadedFile(
            file_id=file_id,
            basename=data.basename,
            contents=data.raw_data,
            digest=data.digest,
            provider=self.provider,
        )

    async def upload_text_file_if_needed(
        self, file_path: str | pathlib.Path
    ) -> UploadedTextFile:
        """Upload ``file_path`` and tag the result as text. Use for
        very-large text inputs that would otherwise blow the prompt
        budget if inlined; ordinary text should go through
        :meth:`get_document`, which keeps the content in-prompt for
        transcript debuggability."""
        data = await _file_data(path=file_path)
        file_id = await self._upload_bytes(data.crc_basename, data.raw_data, data.mime)
        return UploadedTextFile(
            file_id=file_id,
            basename=data.basename,
            contents=data.raw_data,
            digest=data.digest,
            provider=self.provider,
        )

    async def get_document(
        self, path: str | pathlib.Path
    ) -> Document | None:
        """Load a document from disk, picking a representation by the
        binary-vs-text heuristic.

        - Text files (no NUL bytes in the first 8 KiB, and no known
          binary suffix) become ``InMemoryTextFile`` so the content
          stays visible in the prompt for transcript debuggability.
        - Binary files go through the Files API as ``UploadedFile``.

        Returns ``None`` if ``path`` doesn't point at a regular file."""

        p = pathlib.Path(path) if isinstance(path, str) else path
        if not p.is_file():
            return None
        data = await _file_data(path=p)
        if data.is_binary:
            file_id = await self._upload_bytes(data.crc_basename, data.raw_data, data.mime)
            return UploadedFile(
                file_id=file_id,
                basename=data.basename,
                contents=data.raw_data,
                digest=data.digest,
                provider=self.provider
            )
        return InMemoryTextFile(
            basename=p.name,
            string_contents=data.raw_data.decode("utf-8"),
            provider=self.provider,
        )

    async def upload_bytes_if_needed(
        self, basename: str, raw: bytes
    ) -> UploadedFile:
        """Upload in-memory ``raw`` bytes (e.g. an audit-restored binary
        document) to the Files API, reusing a cached upload by CRC. The
        bytes-sourced analogue of :meth:`upload_file_if_needed`."""
        data = await _file_data(basename=basename, data=raw)
        file_id = await self._upload_bytes(data.crc_basename, data.raw_data, data.mime)
        return UploadedFile(
            file_id=file_id,
            basename=data.basename,
            contents=data.raw_data,
            digest=data.digest,
            provider=self.provider
        )

    def text_document_from(self, src: TextUploadable) -> TextDocument:
        """Rehydrate a text ``Uploadable`` into an inline ``TextDocument`` (no
        upload — text stays in-prompt for transcript debuggability)."""
        return InMemoryTextFile(basename=src.basename, string_contents=src.string_contents, provider=self.provider)

    async def document_from(self, src: Uploadable) -> Document:
        """Rehydrate an ``Uploadable`` (e.g. an audit-restored handle) into a
        renderable ``Document``: text stays inline as ``InMemoryTextFile``;
        binary goes through the Files API for the active provider."""
        text = src.string_contents
        if text is not None:
            return InMemoryTextFile(basename=src.basename, string_contents=text, provider=self.provider)
        return await self.upload_bytes_if_needed(src.basename, src.bytes_contents)

