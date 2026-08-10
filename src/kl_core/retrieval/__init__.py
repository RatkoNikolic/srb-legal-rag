"""Retrieval backends — the pluggable seam the benchmark compares.

Each backend answers the same `Query` so the eval harness can report
accuracy, latency, cost, and provenance fidelity side by side across
architectures (naive RAG, advanced RAG, typed-KG/GraphRAG, agentic).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from kl_core.types import Query, RetrievalResult


@runtime_checkable
class RetrievalBackend(Protocol):
    """The contract every architecture implements."""

    name: str

    def retrieve(self, query: Query) -> RetrievalResult:
        """Return the chunks this architecture selects for the query.

        Implementations must populate latency/cost instrumentation —
        the benchmark treats missing numbers as a failure, not as free.
        """
        ...
