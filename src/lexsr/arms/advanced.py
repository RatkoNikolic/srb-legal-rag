"""Advanced RAG: hybrid (vector + BM25, RRF-fused) → temporal filter →
LLM listwise rerank → diversity cap → annotated context.

This is the **smart single-stage** retrieval backbone, and it lives in `base`
(`_smart_retrieve`) because the **graph arm builds on top of it** — graph =
this pipeline (its seeds) + structural expansion (rag_arms.md §1/§3). Keeping
advanced structure-free is what makes the `advanced → graph` delta a clean
"+structure" measurement. The metadata filter is the temporal one — `as_of`
keeps only chunks whose validity interval covers the date (repealed articles
stay retrievable for questions *about* repeal: filtered only when an as_of is
given and they were not in force on it).
"""

from __future__ import annotations

from datetime import date

from lexsr.arms.base import (
    SMART_TOP_K,
    RetrievalArm,
    RetrievedChunk,
    Timer,
)

TOP_K = SMART_TOP_K


class AdvancedArm(RetrievalArm):
    name = "advanced"
    annotate_validity = True

    def retrieve(
        self, question: str, *, as_of: date | None, timer: Timer
    ) -> tuple[list[RetrievedChunk], dict]:
        vector = self._embed_query(question, timer)
        # Advanced *is* the shared smart backbone — nothing structural on top.
        return self._smart_retrieve(question, vector, as_of=as_of, timer=timer, top_k=TOP_K)
