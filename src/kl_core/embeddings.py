"""Embedding seam: vectorization behind a protocol, cost accounted.

Embedding cost is part of corpus-build cost, not $/query — but the benchmark
reports both, so the embedder accounts for tokens the same way the LLM does.
"""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel


class EmbeddingBatch(BaseModel):
    vectors: list[list[float]]
    model: str
    total_tokens: int = 0
    cost_usd: float = 0.0


TaskType = Literal["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"]


class Embedder(Protocol):
    dimensions: int

    def embed(self, texts: list[str], *, task_type: TaskType) -> EmbeddingBatch:
        """Embed a batch of texts. Implementations handle provider batch
        limits and rate limiting internally."""
        ...
