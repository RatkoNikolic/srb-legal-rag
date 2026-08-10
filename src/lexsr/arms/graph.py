"""GraphRAG: vector seeds → typed-KG expansion → temporal slice → annotated
context.

The graph buys what vector search can't see: an article whose answer lives in
the article it *references* (or in the definitions it relies on) ranks low
lexically but is a hop or two away structurally. Expansion is **bounded
2-hop** over UPUĆUJE_NA (cross-references) and DEFINED_IN (the definitions an
article uses), and it **never expands into repealed articles** — a repealed
neighbour is noise, not authority (a repealed article is still reachable as a
*seed* when the question is about the repeal itself). Expanded chunks are
marked "graph-expand" / "graph-2hop" so the drill-down shows what the graph
added. (Sparsity caveat: benchmark.md §6 / knowledge_base.md §9 — most articles
have <1 outgoing edge, so expansion only fires on the linked minority.)
"""

from __future__ import annotations

from datetime import date

from lexsr.arms.base import (
    RRF_K,
    SMART_TOP_K,
    RetrievalArm,
    RetrievedChunk,
    Timer,
    cap_per_act,
    in_force_on,
    row_to_retrieved,
)
from lexsr.translit import to_cyrillic

# Graph = the advanced smart backbone (its seeds) + structural expansion. Seeds
# are advanced's *full* output (SEED_K == SMART_TOP_K), so with every structure
# toggle off the graph arm reproduces advanced exactly (the superset/equivalence
# property — rag_arms.md §1/§3, research/rag_arm_architectures.md). The structural
# layer is then additive: STRUCT_BUDGET units on top of the seeds.
SEED_K = SMART_TOP_K  # 8 — seeds ARE advanced's output (hybrid+RRF+temporal+rerank+cap)
EXPAND_LIMIT = 8
MAX_HOPS = 2
STRUCT_BUDGET = 6  # structural units (2-hop / bylaw / cc / related) appended on top
TOP_K = SEED_K + STRUCT_BUDGET  # 14 — seeds + room for the structure layer
# Hierarchical law→bylaw hop: from a hit article's act, follow the
# authoritative PRAVNI_OSNOV_ZA edge to its bylaws, then a *scoped* vector
# sub-search within each bylaw (not a dump of all its članci — that would
# re-crowd). Hub edges (USTAVNI_OSNOV/POVEZAN_SA) are deliberately NOT traversed
# (the down-weight policy realized as exclusion).
BYLAW_LAWS = 6  # distinct seed-laws whose bylaws we expand into
BYLAW_PER_LAW = 4  # bylaw acts per seed-law
# Candidate pool the scoped sub-search pulls from each bylaw. Set ≥ PER_ACT_CAP
# (the diversity cap keeps ≤3 per bylaw act) so the cap chooses from a real
# hybrid-ranked pool rather than being starved — the old SUB_K=2 < cap=3
# under-filled its own budget *and* used a weaker (vector-only) reader than the
# arm's backbone. The sub-search is now hybrid (`_scoped_hybrid`), matching the
# backbone, so the structural hop is no longer handicapped relative to the seeds.
BYLAW_SUB_K = 6

# P2 — constitutional-validity structural path: from a seed law, surface any
# Constitutional-Court holding against it (OCENA_USTAVNOSTI → the decision's
# materialized izreka). Rare (21 edges corpus-wide) so near-zero normal cost,
# but it directly serves the hardest capability (cc_validity) + invariant 2.
CC_LAWS = 6
CC_PER_LAW = 2

# P4 (experiment) — related-law hop. Cross-law UPUĆUJE_NA is intrinsically sparse
# (264 edges); POVEZAN_SA (2,453) is the densest law↔law relation we have but is
# noisy/hub-heavy. Traverse it *guarded* (non-down-weighted, materialized, other
# primary legislation) + a scoped sub-search, to test whether it turns the
# cross-law negative result positive on the multi-law tier. Gated by the
# `related_law_hop` ablation toggle (AblationConfig; default on) so the cross-law
# delta is cleanly attributable — run with and without it for the ablation.
RELATED_LAWS = 3
RELATED_PER_LAW = 3
RELATED_SUB_K = 4  # scoped hybrid pool per related law — THE per-law budget
# (related laws are primary legislation, which the diversity cap exempts, so
# the bound must be enforced here, not deferred to cap_per_act)

# Every truncated (LIMIT) query carries an explicit deterministic ORDER BY —
# clan-level expansions by `seq` (document order), act-level hops by edge
# insertion order (references.jsonl order) — so which rows survive a cap never
# depends on engine-internal row order.
_BYLAW_QUERY = """
SELECT b.source_id, b.title, b.eli
FROM pravni_osnov_za e JOIN akt b ON b.source_id = e.dst
WHERE e.src = :sid AND b.materialized = 1
GROUP BY b.source_id ORDER BY min(e.rowid)
LIMIT :lim
"""

# UPUĆUJE_NA carries `mention` and is traversed UNDIRECTED (cites ∪ cited-by —
# hence the two-way UNION over the endpoint columns); DEFINED_IN is directed
# and carries no mention — query them separately so the shapes stay honest.
# Both skip repealed targets at the DB level. Every query joins `akt` for
# `vrsta` (+ selects `delegates_to_bylaw`): without them, KG-sourced rows
# defaulted to vrsta="" and the per-act diversity cap wrongly capped expanded
# articles of *primary* statutes (inverting its own exemption), while the
# agent's delegation nudge could never fire on a get_clan-fetched article.
_CLAN_COLS = (
    "b.chunk_id, b.law_slug, b.number, b.fragment, b.text, b.chapter, "
    "b.structural_path, b.article_heading, b.status, b.valid_from, b.valid_to, "
    "b.applies_from, b.published, b.repealed, b.version, b.delegates_to_bylaw, "
    "av.vrsta"
)
# GROUP BY dedupes a neighbour reached via several mention variants (or both
# directions) — DISTINCT alone kept one row per (neighbour, mention) pair,
# wasting the LIMIT budget on duplicates.
_REF_QUERY = f"""
SELECT {_CLAN_COLS}, min(e.mention) AS mention, b.seq
FROM (SELECT dst AS other, mention FROM upucuje_na WHERE src = :cid
      UNION ALL
      SELECT src AS other, mention FROM upucuje_na WHERE dst = :cid) e
JOIN clan b ON b.chunk_id = e.other
JOIN akt av ON av.source_id = b.source_id
WHERE b.repealed = 0
GROUP BY b.chunk_id
ORDER BY b.seq
LIMIT :lim
"""
_DEF_QUERY = f"""
SELECT DISTINCT {_CLAN_COLS}, '' AS mention, b.seq
FROM defined_in e
JOIN clan b ON b.chunk_id = e.dst
JOIN akt av ON av.source_id = b.source_id
WHERE e.src = :cid AND b.repealed = 0
ORDER BY b.seq
LIMIT :lim
"""

_TITLE_QUERY = "SELECT title, eli FROM akt WHERE slug = :slug LIMIT 1"

# P2 — CC holding for a law: the law -[ocena_ustavnosti]-> decision act, whose
# sadrzi izreka chunk is the holding (edge direction is law→decision in the KG).
_CC_QUERY = f"""
SELECT DISTINCT {_CLAN_COLS}, av.title AS source_title, av.eli AS url, b.seq
FROM ocena_ustavnosti o
JOIN sadrzi s ON s.src = o.dst
JOIN clan b ON b.chunk_id = s.dst
JOIN akt av ON av.source_id = b.source_id
WHERE o.src = :sid
ORDER BY b.seq
LIMIT :lim
"""

# P4 — related primary legislation via the (guarded, UNDIRECTED) povezan_sa
# relation; `down_weight` rides the edge.
_RELATED_QUERY = """
SELECT b.source_id, b.title, b.eli
FROM (SELECT dst AS other, down_weight, rowid AS r FROM povezan_sa WHERE src = :sid
      UNION ALL
      SELECT src AS other, down_weight, rowid AS r FROM povezan_sa WHERE dst = :sid) e
JOIN akt b ON b.source_id = e.other
WHERE b.materialized = 1 AND e.down_weight = 0
  AND b.source_id <> :sid AND b.vrsta IN ('zakon', 'ustav')
GROUP BY b.source_id ORDER BY min(e.r)
LIMIT :lim
"""


class GraphArm(RetrievalArm):
    name = "graph"
    annotate_validity = True

    def retrieve(
        self, question: str, *, as_of: date | None, timer: Timer
    ) -> tuple[list[RetrievedChunk], dict]:
        vector = self._embed_query(question, timer)
        # — seeds = the advanced smart backbone (hybrid + RRF + temporal in-force
        #   filter + listwise rerank + diversity cap). This is the fix that makes
        #   graph ⊇ advanced: the old seed path was a bare vector top-k (the naive
        #   primitive) with no rerank and *no as_of filter on the seeds* — the
        #   latter caused the temporal invariant-2 miss. Structure is added on top. —
        retrieved, seed_info = self._smart_retrieve(
            question, vector, as_of=as_of, timer=timer, top_k=SEED_K
        )
        seen = {r.chunk_id for r in retrieved}
        from lexsr.arms.base import _act_of

        seed_laws = list(dict.fromkeys(_act_of(r.chunk_id) for r in retrieved))
        # P1: only hop into bylaws from acts whose seed article actually delegates
        # detail to a bylaw ("ближе прописује министар") — the hop is precise and
        # cheap there, noise everywhere else. (delegates_to_bylaw rides the index
        # row → the vector seeds; KG-sourced expansions default False.)
        # Ablation `delegation_gate`: off → fire the bylaw hop from *all* seed laws
        # (the §6 recall/precision question — trades precision for delegated-norm reach).
        if self.config.delegation_gate:
            delegating_laws = {_act_of(r.chunk_id) for r in retrieved if r.delegates_to_bylaw}
            bylaw_target_laws = [s for s in seed_laws if s in delegating_laws][:BYLAW_LAWS]
        else:
            bylaw_target_laws = seed_laws[:BYLAW_LAWS]
        law_meta: dict[str, dict] = {}
        expansion_edges = []

        def _title(slug: str) -> dict:
            if slug not in law_meta:
                rows = self._rows(_TITLE_QUERY, {"slug": slug})
                law_meta[slug] = rows[0] if rows else {"title": slug, "eli": ""}
            return law_meta[slug]

        # Structural stages run PRECISION-FIRST — cc → bylaw → 2-hop expansion —
        # sharing the STRUCT_BUDGET room above the seeds. Previously the generic
        # 2-hop expansion ran first and could fill the whole budget, after which
        # the cc/bylaw rows were appended and then silently dropped by the final
        # [:TOP_K] truncation — the cc_path toggle produced nothing on exactly
        # the well-connected seed laws where cc_validity questions live. The
        # precise, gated hops (few, high-value rows) now claim room first; the
        # expansion fills what remains. 2-hop frontier = SEEDS only (unchanged —
        # hop rows must not become expansion sources).
        seed_ids = [r.chunk_id for r in retrieved]

        # — P2: constitutional-validity path — surface CC holdings against the
        # seed laws (no as_of filter: a holding *reports* a strike, it isn't a
        # provision in force) —
        # Ablation `cc_path`: off → no constitutional-validity traversal.
        cc_added = 0
        with timer.stage("graph_cc_ms"):
            for sid in (seed_laws[:CC_LAWS] if self.config.cc_path else []):
                if len(retrieved) >= TOP_K:
                    break
                for row in self._rows(_CC_QUERY, {"sid": sid, "lim": CC_PER_LAW}):
                    ncid = row["chunk_id"]
                    if ncid in seen or len(retrieved) >= TOP_K:
                        continue
                    retrieved.append(row_to_retrieved(row, score=0.75, stage="graph-cc"))
                    seen.add(ncid)
                    cc_added += 1

        # — hierarchical law→bylaw hop: act-level edge + scoped sub-search,
        # gated on delegating seed articles (P1) —
        # Ablation `bylaw_hop`: off → no law→bylaw PRAVNI_OSNOV_ZA traversal.
        bylaw_added = 0
        with timer.stage("graph_bylaw_ms"):
            for sid in (bylaw_target_laws if self.config.bylaw_hop else []):
                if len(retrieved) >= TOP_K:
                    break
                for b in self._rows(_BYLAW_QUERY, {"sid": sid, "lim": BYLAW_PER_LAW}):
                    bsid = b["source_id"]
                    sub = self._scoped_hybrid(question, vector, bsid, k=BYLAW_SUB_K, as_of=as_of)
                    for row in sub:
                        ncid = row["chunk_id"]
                        if ncid in seen or len(retrieved) >= TOP_K:
                            continue
                        if as_of is not None and self.config.temporal_filter and not in_force_on(row, as_of):
                            continue
                        row["source_title"] = b.get("title") or row.get("source_title", "")
                        row["url"] = b.get("eli") or row.get("url", "")
                        retrieved.append(
                            row_to_retrieved(row, score=0.7, stage="graph-bylaw")
                        )
                        seen.add(ncid)
                        bylaw_added += 1

        # Ablation `graph_expand`: off → no 2-hop UPUĆUJE_NA/DEFINED_IN expansion.
        with timer.stage("graph_ms"):
            frontier = seed_ids if self.config.graph_expand else []
            for hop in range(MAX_HOPS):
                next_frontier: list[str] = []
                for cid in frontier:
                    if len(retrieved) >= TOP_K:
                        break
                    for query in (_REF_QUERY, _DEF_QUERY):
                        for row in self._rows(query, {"cid": cid, "lim": EXPAND_LIMIT}):
                            ncid = row["chunk_id"]
                            expansion_edges.append(
                                {"from": cid, "to": ncid, "mention": row.get("mention", "")}
                            )
                            if ncid in seen:
                                continue
                            if as_of is not None and self.config.temporal_filter and not in_force_on(row, as_of):
                                continue
                            meta = _title(row["law_slug"])
                            row["source_title"] = meta["title"]
                            row["url"] = meta["eli"]
                            stage = "graph-expand" if hop == 0 else "graph-2hop"
                            retrieved.append(
                                row_to_retrieved(row, score=0.9 ** (hop + 1), stage=stage)
                            )
                            seen.add(ncid)
                            next_frontier.append(ncid)
                frontier = next_frontier
                if not frontier or len(retrieved) >= TOP_K:
                    break

        # — P4 (experiment): guarded related-law hop over POVEZAN_SA + sub-search —
        # Ablation `related_law_hop`: the toggle for the cross-law ablation.
        related_added = 0
        if self.config.related_law_hop:
            with timer.stage("graph_related_ms"):
                for sid in seed_laws[:RELATED_LAWS]:
                    if len(retrieved) >= TOP_K:
                        break
                    for b in self._rows(_RELATED_QUERY, {"sid": sid, "lim": RELATED_PER_LAW}):
                        sub = self._scoped_hybrid(
                            question, vector, b["source_id"], k=RELATED_SUB_K, as_of=as_of
                        )
                        for row in sub:
                            ncid = row["chunk_id"]
                            if ncid in seen or len(retrieved) >= TOP_K:
                                continue
                            if as_of is not None and self.config.temporal_filter and not in_force_on(row, as_of):
                                continue
                            row["source_title"] = b.get("title") or row.get("source_title", "")
                            row["url"] = b.get("eli") or row.get("url", "")
                            retrieved.append(
                                row_to_retrieved(row, score=0.6, stage="graph-related")
                            )
                            seen.add(ncid)
                            related_added += 1

        # diversity: cap per source act (bylaws only — primary-statute seeds
        # are exempt so 2-hop/bylaw expansion can't evict a gold seed), then TOP_K.
        # Ablation `diversity_cap`: off → no per-act cap (still truncated to TOP_K).
        if self.config.diversity_cap:
            retrieved = cap_per_act(
                retrieved, id_of=lambda c: c.chunk_id, kind_of=lambda c: c.vrsta
            )
        retrieved = retrieved[:TOP_K]
        extras = {
            "expansion_edges": expansion_edges,
            "expanded_count": sum(1 for r in retrieved if r.stage.startswith("graph-")),
            "two_hop_count": sum(1 for r in retrieved if r.stage == "graph-2hop"),
            "bylaw_count": sum(1 for r in retrieved if r.stage == "graph-bylaw"),
            "cc_count": sum(1 for r in retrieved if r.stage == "graph-cc"),
            "related_count": sum(1 for r in retrieved if r.stage == "graph-related"),
            "bylaw_hop_gated_laws": len(bylaw_target_laws),
            # seed-backbone diagnostics + cost (the seed rerank is a real LLM call)
            "dropped_by_temporal_filter": seed_info["dropped_by_temporal_filter"],
            "temporal_filter_starved": seed_info["temporal_filter_starved"],
            "rerank_model": seed_info.get("rerank_model"),
            "_extra_prompt_tokens": seed_info["_extra_prompt_tokens"],
            "_extra_output_tokens": seed_info["_extra_output_tokens"],
            "_extra_cost_usd": seed_info["_extra_cost_usd"],
        }
        return retrieved, extras

    def _rows(self, query: str, params: dict) -> list[dict]:
        return self.kg_conn.query(query, params)

    def _scoped_hybrid(
        self, question: str, vector: list[float], source_id: str, *, k: int,
        as_of: date | None = None,
    ) -> list[dict]:
        """Hybrid (vector + BM25, RRF-fused) retrieval scoped to ONE act — the
        bylaw / related-law hop's sub-search. Uses the **same retrieval method as
        the arm's backbone** (dense + lexical, RRF), just filtered to the act the
        graph edge selected, instead of the prior vector-only top-k. So the
        structural hop is no longer handicapped relative to the seeds, and a hop
        gain is attributable to the *scoping* (the graph edge narrowing ~25k chunks
        to one bylaw), not to a weaker reader. Honors the `hybrid` ablation; the
        per-act diversity cap downstream enforces the final per-bylaw budget."""
        safe = source_id.replace("'", "''")
        where = f"source_id = '{safe}'"
        if as_of is None:
            where += " AND is_version_slice = false"  # slice-flood guard (base._hybrid_candidates)
        try:
            vec_rows = self.table.search(vector).where(where).limit(k).to_list()
        except Exception:
            vec_rows = []
        fts_rows: list[dict] = []
        if self.config.hybrid:
            try:
                fts_rows = (
                    self.table.search(to_cyrillic(question), query_type="fts")
                    .where(where)
                    .limit(k)
                    .to_list()
                )
            except Exception:
                fts_rows = []  # FTS+filter unsupported/failed → dense leg carries
        fused: dict[str, dict] = {}
        scores: dict[str, float] = {}
        for rows in (vec_rows, fts_rows):
            for rank, row in enumerate(rows):
                cid = row["chunk_id"]
                fused.setdefault(cid, row)
                scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
        return sorted(fused.values(), key=lambda r: scores[r["chunk_id"]], reverse=True)[:k]
