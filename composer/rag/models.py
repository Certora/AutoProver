import os
import threading
from typing import TYPE_CHECKING, override

from langchain_core.embeddings import Embeddings

# One process-wide gate for every sentence-transformer encode: the model's remote
# code caches positional tensors per sequence length, so concurrent encodes race
# (shape mismatches on CPU, SIGSEGV in torch's MPS shader cache). Shared between
# the sync Embeddings API here and the async ComposerRAGDB wrappers so the two
# paths cannot race each other on the same model instance either.
ENCODE_LOCK = threading.Lock()

# claim we always import ST
if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer
    def get_model() -> SentenceTransformer:
        ...
else:
    try:
        from sentence_transformers import SentenceTransformer #type: ignore

        def get_model() -> SentenceTransformer:
            # COMPOSER_EMBED_DEVICE overrides the auto-picked device. torch's MPS shader
            # cache is not thread-safe under concurrent encodes (SIGSEGV on Apple
            # Silicon), so mac hosts should set it to "cpu".
            return SentenceTransformer(
                'nomic-ai/nomic-embed-text-v1.5',
                trust_remote_code=True,
                device=os.environ.get("COMPOSER_EMBED_DEVICE"),
            )
    except ImportError:
        # for tests (no ST dependency)
        def get_model() -> "SentenceTransformer":
            raise NotImplementedError("Sentence transformers not available")


class DefaultEmbedder(Embeddings):
    def __init__(self, model: "SentenceTransformer | None" = None):
        self.model : "SentenceTransformer" = get_model() if not model else model

    @override
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        with ENCODE_LOCK:
            return self.model.encode_document(
                texts
            ).tolist() #type: ignore

    @override
    def embed_query(self, text: str) -> list[float]:
        with ENCODE_LOCK:
            return self.model.encode_query(
                [text]
            ).tolist()[0] #type: ignore
