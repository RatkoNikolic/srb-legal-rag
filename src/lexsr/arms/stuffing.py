"""Long-context stuffing: the no-retrieval baseline — the whole law in context.

The arm every practitioner reaches for first ("the law fits in the window,
why retrieve?"). In the benchmark it runs as an *oracle* condition: the
target acts come from the gold annotation — ALL gold-cited acts, stuffed
whole in gold order (the gold-supporting-fact ceiling of HotpotQA/KILT
includes every gold fact) — so the arm measures the ceiling of full-text
generation with perfect act selection; retrieval errors are not in its
error budget, and results are labeled accordingly. Interactively (no gold
label available) it falls back to the law of the top-1 vector hit,
recorded in extras as law_selection="vector-top1".

Deliberately naive everywhere else: current consolidation only, no validity
annotation, no as_of handling — temporal failures are the expected, designed
result, and prompt size makes latency and $/query the visible cost.
"""

from __future__ import annotations

import re
from datetime import date

from lexsr.arms.base import RetrievalArm, RetrievedChunk, Timer, row_to_retrieved

SCAN_LIMIT = 5000  # > largest law in the corpus (ZOO: 1097 članova)
# Interactive (non-oracle) cost guard: an out-of-corpus question whose top-1
# vector hit lands in a huge law would otherwise stuff the whole thing (a
# $0.32 query was observed). The oracle/benchmark path is never truncated —
# whole-law-in-context is its measured identity.
MAX_INTERACTIVE_STUFF_CHARS = 120_000  # ~30k tokens

# Accepts both a corpus slug (`zakon-o-radu`) and a materialized-bylaw slug
# (`ref/432868`) — the oracle target resolves to either. The `/` is safe in the
# `where law_slug = '…'` clause (still no quote/space/`;`), so this stays an
# injection guard, just one that no longer rejects bylaw slugs.
_SLUG_RE = re.compile(r"^[a-z0-9/-]+$")
_CLAN_NUM_RE = re.compile(r"/clan-(\d+)(.*)$")


def _clan_order(chunk_id: str) -> tuple[int, str]:
    """Document order: 'clan-2' before 'clan-10'; suffixed (12а) after base."""
    m = _CLAN_NUM_RE.search(chunk_id)
    return (int(m.group(1)), m.group(2)) if m else (10**6, chunk_id)


class StuffingArm(RetrievalArm):
    name = "context-stuffing"
    annotate_validity = False

    def retrieve(
        self,
        question: str,
        *,
        as_of: date | None,
        timer: Timer,
        law_slug: str | list[str] | None = None,
    ) -> tuple[list[RetrievedChunk], dict]:
        # Oracle path: ALL gold-cited acts (a list), stuffed whole, in gold
        # order — the ceiling's gold-supporting-fact lineage (HotpotQA/KILT)
        # includes every gold fact, and 40/60 situational items are multi-act,
        # so single-act stuffing understated the ceiling on exactly those.
        targets: list[str] = (
            [law_slug] if isinstance(law_slug, str) else list(law_slug or [])
        )
        selection = "oracle"
        if not targets:
            selection = "vector-top1"
            vector = self._embed_query(question, timer)
            with timer.stage("law_select_ms"):
                hits = self.table.search(vector).limit(1).to_list()
            if hits:
                targets = [hits[0]["law_slug"]]
        targets = [t for t in targets if t and _SLUG_RE.match(t)]
        extras: dict = {
            "law_selection": selection,
            "law_slug": targets[0] if targets else "",
            "law_slugs": targets,
        }
        if not targets:
            return [], extras

        with timer.stage("stuff_ms"):
            # "current consolidation only" (module contract): the dated @version
            # slice rows are benchmark infrastructure — without this filter
            # an amended law stuffs ~3× the tokens with contradictory historical
            # article texts (zakon-o-radu: 1,488 rows, 1,190 of them slices).
            in_list = ", ".join(f"'{t}'" for t in targets)
            rows = (
                self.table.search()
                .where(f"law_slug IN ({in_list}) AND is_version_slice = false")
                .limit(SCAN_LIMIT)
                .to_list()
            )
        act_rank = {t: i for i, t in enumerate(targets)}
        rows.sort(key=lambda r: (act_rank.get(r["law_slug"], len(targets)),
                                 _clan_order(r["chunk_id"])))
        retrieved = [row_to_retrieved(row, score=0.0, stage="stuffed") for row in rows]
        # Cost guard for the interactive path only (oracle stays whole-law).
        if selection == "vector-top1":
            kept, chars, truncated = [], 0, False
            for r in retrieved:
                if chars + len(r.text) > MAX_INTERACTIVE_STUFF_CHARS and kept:
                    truncated = True
                    break
                kept.append(r)
                chars += len(r.text)
            retrieved, extras["truncated"] = kept, truncated
        extras["stuffed_chunks"] = len(retrieved)
        extras["stuffed_chars"] = sum(len(r.text) for r in retrieved)
        return retrieved, extras
