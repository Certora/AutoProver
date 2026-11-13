import asyncio
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, cast

from numpy import ndarray

from composer.rag.models import ENCODE_LOCK
from composer.rag.types import BlockChunk, ManualRef, ManualSectionHit

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer


class ComposerRAGDB(ABC):
    """A RAG corpus: embedded chunks for semantic search, plus whole manual sections for keyword
    search and exact retrieval by header path."""

    def __init__(self, model: "SentenceTransformer"):
        self.tr = model

    @abstractmethod
    async def add_chunks_batch(self, chunks: list[BlockChunk]) -> None:
        """Add chunks to the database in batches."""
        ...

    @abstractmethod
    async def add_manual_section(self, ch: BlockChunk) -> None:
        """Add a manual section for keyword search and exact retrieval."""
        ...

    @abstractmethod
    async def find_refs(self, query: str, similarity_cutoff: float = 0.5,
                  top_k: int = 10, manual_section: list[str] = []) -> list[ManualRef]:
        """Search for similar documents using semantic similarity."""
        ...

    @abstractmethod
    async def search_manual_keywords(self, query: str, *, min_depth: int = 0,
                               limit: int = 10) -> list[ManualSectionHit]:
        """Search sections by keywords using full-text search."""
        ...

    @abstractmethod
    async def get_manual_section(self, headers: list[str]) -> str | None:
        """Retrieve full section content by exact headers."""
        ...

    @abstractmethod
    async def open(self) -> None:
        """Connect, and fail now if the corpus is unreachable or unusable. Every other method
        connects on first use, so calling this first is optional."""
        ...

    @abstractmethod
    async def aclose(self) -> None:
        ...

    # Encodes race when concurrent (see ENCODE_LOCK in composer.rag.models — the lock
    # is shared with the sync DefaultEmbedder so neither path can race the other).
    def _encode_query_locked(self, query: str) -> ndarray:
        with ENCODE_LOCK:
            return cast(ndarray, self.tr.encode_query(query, show_progress_bar=False))

    def _encode_docs_locked(self, docs: list[str]) -> list[ndarray]:
        with ENCODE_LOCK:
            return cast(list[ndarray], self.tr.encode_document(docs, show_progress_bar=False))

    async def embed_query(
        self, query: str
    ) -> ndarray:
        return await asyncio.to_thread(
            self._encode_query_locked, f"search_query: {query}"
        )

    async def embed_docs(
        self, doc: list[BlockChunk]
    ) -> list[ndarray]:
        return await asyncio.to_thread(
                self._encode_docs_locked, [f"search_document: {d.chunk}" for d in doc]
            )
