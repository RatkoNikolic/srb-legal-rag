"""Naive RAG: plain vector top-k, no temporal handling, no validity hints.

Deliberately the dumb baseline — it sees the same corpus but gets no
amendment/repeal metadata in its context, so temporally-wrong confident
answers are *expected* here. That failure mode is the benchmark's point.
"""

from __future__ import annotations

from datetime import date

from lexsr.arms.base import RetrievalArm, RetrievedChunk, Timer, row_to_retrieved

TOP_K = 8


class NaiveArm(RetrievalArm):
    name = "naive"
    annotate_validity = False

    def retrieve(
        self, question: str, *, as_of: date | None, timer: Timer
    ) -> tuple[list[RetrievedChunk], dict]:
        vector = self._embed_query(question, timer)
        with timer.stage("vector_ms"):
            # The dated @version slices are benchmark infrastructure that a
            # real naive deployment would never index — retrieving them lowers
            # this baseline's floor by an artifact (a heavily-amended article's
            # slices crowd out everything else), flattering every engineered
            # arm's delta. Excluded always: naive has no temporal handling, so
            # there is no as_of path that could legitimately want them.
            rows = (
                self.table.search(vector)
                .where("is_version_slice = false")
                .limit(TOP_K)
                .to_list()
            )
        retrieved = [
            # _distance is Lance's *squared* L2; document vectors are unit-norm
            # (verified 2026-07-29), so 1 − d² = 2·cos − 1 — monotone with cosine
            row_to_retrieved(row, score=1 - row.get("_distance", 0.0), stage="vector")
            for row in rows
        ]
        return retrieved, {}
