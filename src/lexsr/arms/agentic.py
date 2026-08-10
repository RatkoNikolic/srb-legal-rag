"""Agentic RAG: a bounded single-agent ReAct loop over typed legal tools
(see the arms design doc).

The other arms run a fixed retrieval pipeline; this one *decides* what to
retrieve, step by step. Per the agentic-RAG taxonomy (Singh et al., 2025) the
benchmark wants single-agent agentic RAG — a ReAct loop (Yao et al., ICLR
2023) with retrieval-on-demand. Orchestration is the treatment: same corpus,
same generator, same ANSWER_SCHEMA-constrained output as every other arm, so
the citation/abstain/provenance invariants are inherited unchanged.

Seven tools, all thin wrappers over functions the other arms already use — the
superset, strictly ⊇ every other arm's capability:
  search_corpus(query)            — the advanced arm's hybrid (vector+BM25/RRF) leg, incl. the diversity cap
  get_clan(law_slug, broj)        — deterministic article fetch (follow „члан 52", don't re-search)
  get_bylaws(law_slug, query)     — the graph arm's PRAVNI_OSNOV_ZA law→bylaw hop as a callable
  expand_references(chunk_id)     — the graph arm's KG 1-hop over UPUĆUJE_NA
  check_constitutionality(law_slug) — OCENA_USTAVNOSTI → the CC decision's izreka (was a provision struck?)
  law_timeline(law_slug)          — gazette chain + validity intervals (the temporal tool)
  list_laws()                     — corpus scope → calibrated abstention

The loop runs at most MAX_STEPS tool calls, then is forced to answer-or-abstain
over everything it gathered. Every tool call is logged to extras.tool_trace
(name, args, latency, result size) → agent-trace drill-down in the UI + honest
$/query.
"""

from __future__ import annotations

import json
from datetime import date

from kl_core.llm import generate_json
from lexsr.arms.base import (
    RetrievalArm,
    RetrievedChunk,
    Timer,
    cap_per_act,
    in_force_on,
    route_slices,
    row_to_retrieved,
)
from lexsr.arms.graph import _BYLAW_QUERY, _CC_QUERY, _REF_QUERY, _TITLE_QUERY
from lexsr.config import CORPUS_DIR
from lexsr.translit import to_cyrillic

# Retuned for the uncapped corpus + the extra bylaw hop: more steps and a
# bigger pool so a deliberately-fetched bylaw can't be evicted before answering.
MAX_STEPS = 10

# sentinel observation for a search that ran but matched nothing — the research
# guardrail distinguishes it from error/empty-query observations
NO_RESULTS = "нема резултата"
SEARCH_K = 6
RRF_K = 60
CANDIDATES_PER_LEG = 16
POOL_CAP = 24  # chunks carried into the final answer (most recent wins within a class)
BYLAW_ACTS = 4  # bylaw acts expanded per get_bylaws call (= graph's BYLAW_PER_LAW)
BYLAW_SUB_K = 3  # članci pulled per bylaw act by get_bylaws' scoped sub-search
BYLAW_QUOTA = 6  # pool slots guaranteed to the newest bylaw sub-search results
OBS_SNIPPET = 320  # chars of article text shown back to the agent per chunk
_SEED_ROLES = {"seed", "hub", "foundational"}  # list_laws scope

TOOLS_DOC = """Доступни алати:
- search_corpus(query): хибридна претрага корпуса (вектор + BM25); враћа најрелевантније чланове.
- get_clan(law_slug, broj): прецизно дохвата одређени члан закона (нпр. за „у складу са чланом 52").
- get_bylaws(law_slug, query): подзаконски акти (правилници/уредбе) донети на основу закона — користи кад закон делегира детаље („ближе прописује министар"); враћа релевантне чланове из тих аката. ОБАВЕЗНО проследи `query` (кључне речи за претрагу унутар аката); алат не прима chunk_id.
- expand_references(chunk_id): чланови на које дохваћени члан упућује (унакрсне референце).
- check_constitutionality(law_slug): одлуке Уставног суда против закона (да ли је нека одредба оборена као неуставна) — користи кад питање зависи од важности после оцене уставности.
- law_timeline(law_slug): хронологија измена закона и интервали важности (службени гласници, датуми).
- list_laws(): списак закона у корпусу (за проверу да ли питање уопште спада у корпус).
- finish: заврши и формулиши коначан одговор на основу прикупљених извода."""

ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "thought": {"type": "string"},
        "tool": {
            "type": "string",
            "enum": [
                "search_corpus",
                "get_clan",
                "get_bylaws",
                "expand_references",
                "check_constitutionality",
                "law_timeline",
                "list_laws",
                "finish",
            ],
        },
        "query": {"type": "string"},
        "law_slug": {"type": "string"},
        "broj": {"type": "string"},
        "chunk_id": {"type": "string"},
    },
    "required": ["thought", "tool"],
}

AGENT_SYSTEM = """Ти си правни истраживач за прописе Републике Србије. Имаш алате
за претрагу и навигацију кроз корпус закона. У сваком кораку размисли (thought)
шта ти треба, па позови ТАЧНО ЈЕДАН алат.

Општи поступак (примени по потреби, није обавезан редослед):
1) Нађи меродаван члан закона (search_corpus; get_clan за изричите референце „члан N").
2) Ако закон делегира детаље на подзаконски акт („ближе прописује министар/Влада",
   „услови/начин/образац/тарифа прописује се правилником"), позови get_bylaws да
   нађеш правилник/уредбу са конкретним детаљем.
3) Прати унакрсне референце (expand_references) кад одговор зависи од другог члана.
4) Провери временску важност (law_timeline; пази на измењене/брисане чланове). Ако
   питање зависи од оцене уставности, позови check_constitutionality да видиш да ли
   је Уставни суд оборио неку одредбу.
5) Пре уздржавања провери опсег корпуса (list_laws).

Прикупи довољно извора да поткрепиш сваку тврдњу. Пре него што позовеш
`finish`, провери да ли за СВАКИ део питања (кад их има више) имаш конкретну
одредбу (пропис + члан) која га покрива — ако за неки део немаш, прво је
дохвати. Када имаш довољно, позови `finish`. Ако питање не спада у корпус,
позови `finish` и објасни зашто се уздржаваш. Не измишљај садржај прописа.""" + "\n\n" + TOOLS_DOC


class AgenticArm(RetrievalArm):
    name = "agentic"
    annotate_validity = True

    def __init__(self, llm, embedder, table, kg_conn=None, config=None) -> None:
        super().__init__(llm, embedder, table, kg_conn, config)
        self._manifest: list[dict] | None = None

    # — corpus metadata (list_laws / law_timeline) —
    def _laws(self) -> list[dict]:
        if self._manifest is None:
            path = CORPUS_DIR / "manifest.json"
            self._manifest = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        return self._manifest

    def retrieve(
        self, question: str, *, as_of: date | None, timer: Timer
    ) -> tuple[list[RetrievedChunk], dict]:
        pool: list[RetrievedChunk] = []
        seen: set[str] = set()
        transcript: list[str] = []
        tool_trace: list[dict] = []
        steps = 0
        extra_prompt = extra_output = 0
        extra_cost = 0.0

        def add(chunk: RetrievedChunk) -> bool:
            if chunk.chunk_id in seen:
                return False
            seen.add(chunk.chunk_id)
            pool.append(chunk)
            return True

        decision_attempts = 0
        decision_giveups = 0
        search_ok = False  # ≥1 search_corpus that ran (results or a real empty)
        forced_search = False
        seen_calls: set[tuple] = set()  # (tool, frozen args) — repeat-call guard

        with timer.stage("agent_ms"):
            # Seed the pool with one search of the RAW question (ablation toggle,
            # default on): the loop then refines instead of re-deriving retrieval
            # from self-authored reformulations — which measurably miss golds the
            # raw question finds (e20/h07-class failures, guardrail-run report §5).
            # Makes the agentic arm a retrieval superset of the flat search leg —
            # the seed chunks carry their own stage (`agent-seed`) so `_cap_pool`
            # can keep that guarantee under floods.
            if self.config.seed_search:
                observation, new_chunks = self._dispatch(
                    "search_corpus", {"query": question}, as_of, question
                )
                for c in new_chunks:
                    c.stage = "agent-seed"
                if new_chunks or observation == NO_RESULTS:
                    search_ok = True
                added = sum(1 for c in new_chunks if add(c))
                seen_calls.add(("search_corpus", json.dumps({"query": question},
                                                           sort_keys=True, ensure_ascii=False)))
                tool_trace.append({
                    "step": 0, "tool": "search_corpus", "seed": True,
                    "args": {"query": question},
                    "result_chars": len(observation), "chunks_added": added,
                })
                transcript.append(
                    f"Корак 0 (почетна претрага корпуса за цело питање)"
                    f" — search_corpus({question!r})\nРезултат: {observation}"
                )

            for steps in range(1, MAX_STEPS + 1):
                last_step = steps == MAX_STEPS
                prompt = self._decision_prompt(question, as_of, transcript, last_step)
                # 4096 (was 1024): a thinking model spends part of the budget on
                # thoughts; retries cover transient parse/shape failures, and
                # every failed attempt is logged into the trace (never silent).
                action, acct = generate_json(
                    self.llm, prompt, system=AGENT_SYSTEM,
                    json_schema=ACTION_SCHEMA, max_output_tokens=4096,
                )
                extra_prompt += acct.prompt_tokens
                extra_output += acct.output_tokens
                extra_cost += acct.cost_usd
                decision_attempts += acct.attempts
                for failure in acct.failures:
                    tool_trace.append({
                        "step": steps, "tool": "__decision_retry__",
                        "error": failure["error"],
                        "finish_reason": failure["finish_reason"],
                        "raw_preview": failure["raw_preview"],
                    })
                if action is None:
                    decision_giveups += 1
                    break  # the post-loop conclusion guard handles the rest
                tool = action.get("tool", "finish")
                if tool == "finish":
                    tool_trace.append({"step": steps, "tool": "finish",
                                       "thought": action.get("thought", "")})
                    break

                call_args = {k: action[k] for k in ("query", "law_slug", "broj", "chunk_id")
                             if action.get(k)}
                call_key = (tool, json.dumps(call_args, sort_keys=True, ensure_ascii=False))
                repeated = call_key in seen_calls
                seen_calls.add(call_key)
                observation, new_chunks = self._dispatch(tool, action, as_of, question)
                if tool == "search_corpus" and (new_chunks or observation == NO_RESULTS):
                    search_ok = True
                if repeated:
                    # identical call already made — annotate so the policy stops
                    # burning its step budget on duplicates (e20: same get_clan 3×)
                    observation = (
                        "НАПОМЕНА: исти алат са истим аргументима је већ позван — "
                        "резултат је непромењен. Изабери ДРУГИ корак.\n" + observation
                    )
                added = sum(1 for c in new_chunks if add(c))
                tool_trace.append({
                    "step": steps,
                    "tool": tool,
                    "args": {k: action[k] for k in ("query", "law_slug", "broj", "chunk_id")
                             if action.get(k)},
                    "thought": action.get("thought", ""),
                    "result_chars": len(observation),
                    "chunks_added": added,
                })
                transcript.append(
                    f"Корак {steps} — {tool}({self._args_str(action)})\nРезултат: {observation}"
                )

            # No conclusion without research (the abstention mirror of invariant
            # 1): if the loop concluded — finish, decision give-up, or step
            # exhaustion — with an empty pool and no real search, the harness
            # runs one seeded search on the question itself. Without it, an
            # early-quitting policy produces "no information in the provided
            # context" abstentions whose context is empty because nothing ever
            # looked (the 2026-08-05 shift analysis, §4/§5).
            if not pool and not search_ok:
                observation, new_chunks = self._dispatch(
                    "search_corpus", {"query": question}, as_of, question
                )
                for c in new_chunks:
                    c.stage = "agent-seed"
                added = sum(1 for c in new_chunks if add(c))
                forced_search = True
                tool_trace.append({
                    "step": steps, "tool": "search_corpus", "forced": True,
                    "args": {"query": question},
                    "result_chars": len(observation), "chunks_added": added,
                })

        final_pool = self._cap_pool(pool)
        extras = {
            "tool_trace": tool_trace,
            "steps_used": steps,
            "tools_called": [t["tool"] for t in tool_trace],
            "pool_size": len(final_pool),
            "forced_search": forced_search,
            "_llm_io_decision": {
                "attempts": decision_attempts,
                "giveups": decision_giveups,
                "failures": [t for t in tool_trace if t["tool"] == "__decision_retry__"],
            },
            "_extra_prompt_tokens": extra_prompt,
            "_extra_output_tokens": extra_output,
            "_extra_cost_usd": extra_cost,
        }
        return final_pool, extras

    # Pool retention is class-based (the 2026-08-06 eviction-bug fix wave; the
    # h06 forensic: one get_bylaws flood used to evict every search chunk,
    # including the seed — and unbounded protected fetches could mirror-flood
    # the other way, pushing the pool past POOL_CAP):
    #   1. seed chunks (`agent-seed`, ≤ SEARCH_K) — always kept: they are the
    #      arm's flat-search-superset guarantee;
    #   2. deliberate fetches (get_clan / expand / cc) — newest-first, bounded;
    #   3. refined searches — newest-first in the room deliberate left;
    #   4. bylaw sub-search bulk (`agent-bylaw`) — newest-first in the room
    #      that remains, but classes 2–3 are bounded so that BYLAW_QUOTA slots
    #      survive whenever bylaw chunks exist (delegated-norm answers need
    #      the statute AND the pravilnik in the pool together).
    # Total never exceeds POOL_CAP; the filter is order-preserving (citations
    # are pool indices and format_context serializes in pool order).
    _SEED_STAGE = "agent-seed"
    _BYLAW_STAGE = "agent-bylaw"
    _DELIBERATE = {"agent-get_clan", "agent-expand", "agent-cc"}

    @classmethod
    def _cap_pool(cls, pool: list[RetrievedChunk]) -> list[RetrievedChunk]:
        if len(pool) <= POOL_CAP:
            return pool
        seeds = [c for c in pool if c.stage == cls._SEED_STAGE]
        bylaw = [c for c in pool if c.stage == cls._BYLAW_STAGE]
        deliberate = [c for c in pool if c.stage in cls._DELIBERATE]
        searches = [c for c in pool
                    if c.stage not in cls._DELIBERATE
                    and c.stage not in (cls._SEED_STAGE, cls._BYLAW_STAGE)]
        research_room = POOL_CAP - len(seeds) - min(len(bylaw), BYLAW_QUOTA)
        kept_deliberate = deliberate[-research_room:] if research_room > 0 else []
        search_room = research_room - len(kept_deliberate)
        kept_searches = searches[-search_room:] if search_room > 0 else []
        bylaw_room = (POOL_CAP - len(seeds)
                      - len(kept_deliberate) - len(kept_searches))
        kept_bylaw = bylaw[-bylaw_room:] if bylaw_room > 0 else []
        keep = ({id(c) for c in seeds} | {id(c) for c in kept_deliberate}
                | {id(c) for c in kept_searches} | {id(c) for c in kept_bylaw})
        return [c for c in pool if id(c) in keep]

    # — the ReAct decision prompt —
    def _decision_prompt(
        self, question: str, as_of: date | None, transcript: list[str], last_step: bool
    ) -> str:
        date_clause = f"\n(Питање се односи на стање на дан {as_of.isoformat()}.)" if as_of else ""
        history = "\n\n".join(transcript) if transcript else "(још нема корака)"
        force = (
            "\n\nДостигнут је лимит корака — ОБАВЕЗНО позови `finish` сада."
            if last_step
            else ""
        )
        return (
            f"Питање: {question}{date_clause}\n\n"
            f"Досадашњи кораци:\n{history}{force}\n\n"
            f"Одлучи следећи корак. Врати JSON по схеми (thought, tool, и аргументе алата)."
        )

    # — tool dispatch —
    def _dispatch(
        self, tool: str, action: dict, as_of: date | None, question: str = ""
    ) -> tuple[str, list[RetrievedChunk]]:
        try:
            if tool == "search_corpus":
                return self._search_corpus(action.get("query") or "", as_of)
            if tool == "get_clan":
                return self._get_clan(action.get("law_slug") or "", action.get("broj") or "")
            if tool == "get_bylaws":
                # Malformed-call fallback (h06/h10/h15 forensics): the policy
                # sometimes passes chunk_id instead of query; embedding the law
                # slug (the old fallback) is a degenerate sub-search that
                # returns each bylaw's generic head articles. Fall back to the
                # question and say so, mirroring the repeat-call guard.
                query = action.get("query") or ""
                note = ""
                if not query:
                    query = question
                    note = (
                        "НАПОМЕНА: get_bylaws је позван без `query` — коришћено "
                        "је цело питање као упит. Проследи `query` (кључне речи) "
                        "за циљанију претрагу подзаконских аката.\n"
                    )
                observation, chunks = self._get_bylaws(
                    action.get("law_slug") or "", query, as_of
                )
                return note + observation, chunks
            if tool == "expand_references":
                return self._expand_references(action.get("chunk_id") or "")
            if tool == "check_constitutionality":
                return self._check_constitutionality(action.get("law_slug") or "")
            if tool == "law_timeline":
                return self._law_timeline(action.get("law_slug") or ""), []
            if tool == "list_laws":
                return self._list_laws(), []
        except Exception as exc:  # a tool error is an observation, not a crash
            return f"грешка у алату: {exc}", []
        return f"непознат алат: {tool}", []

    def _search_corpus(
        self, query: str, as_of: date | None
    ) -> tuple[str, list[RetrievedChunk]]:
        if not query.strip():
            return "празан упит", []
        vector = self.embedder.embed([query], task_type="RETRIEVAL_QUERY").vectors[0]
        # same slice-flood guard as base._hybrid_candidates: no as_of → exclude
        # dated @version slices at the search level, not post-hoc
        slice_filter = None if as_of is not None else "is_version_slice = false"
        vq = self.table.search(vector)
        if slice_filter:
            vq = vq.where(slice_filter)
        vec_rows = vq.limit(CANDIDATES_PER_LEG).to_list()
        try:
            fq = self.table.search(to_cyrillic(query), query_type="fts")
            if slice_filter:
                fq = fq.where(slice_filter)
            fts_rows = fq.limit(CANDIDATES_PER_LEG).to_list()
        except Exception:
            fts_rows = []
        fused: dict[str, dict] = {}
        scores: dict[str, float] = {}
        for rows in (vec_rows, fts_rows):
            for rank, row in enumerate(rows):
                cid = row["chunk_id"]
                fused.setdefault(cid, row)
                scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
        ranked = sorted(fused.values(), key=lambda r: scores[r["chunk_id"]], reverse=True)
        ranked = route_slices(ranked, as_of)
        # Ablation `temporal_filter`: off → skip the as_of in-force filter.
        if as_of is not None and self.config.temporal_filter:
            ranked = [r for r in ranked if in_force_on(r, as_of)]
        # diversity — kind-aware: primary legislation (zakon/ustav) is exempt
        # so the governing statute's own articles are never capped. Without
        # kind_of the search tool would drop the 4th+ article of the governing law
        # on multi-article lookups — the exact recall starvation the diversity cap fixed.
        # Ablation `diversity_cap`: off → no per-act cap.
        if self.config.diversity_cap:
            ranked = cap_per_act(
                ranked, id_of=lambda r: r["chunk_id"], kind_of=lambda r: r.get("vrsta", "")
            )
        chunks = [
            row_to_retrieved(r, score=scores[r["chunk_id"]], stage="agent-search")
            for r in ranked[:SEARCH_K]
        ]
        return self._format_chunks(chunks), chunks

    def _get_clan(self, law_slug: str, broj: str) -> tuple[str, list[RetrievedChunk]]:
        if not (law_slug and broj) or self.kg_conn is None:
            return "недостаје law_slug или broj", []
        # `law_slug` may arrive as the corpus slug (zakon-o-radu) or as the ELI
        # source_id prefix the agent read off a chunk_id — resolve both to the
        # chunk_id and match on that, so the article is fetched either way.
        # `broj` suffixes are Cyrillic in chunk ids (clan-14а) but the policy
        # sometimes emits Latin ("14a") — normalize before building the id.
        source_id = law_slug if "/" in law_slug else self._source_id(law_slug)
        cid = f"{source_id}/clan-{to_cyrillic(broj)}"
        q = (
            "SELECT c.chunk_id, c.law_slug, c.number, c.fragment, c.text, "
            "c.valid_from, c.valid_to, c.repealed, c.version, "
            "c.delegates_to_bylaw, z.vrsta, "
            "z.title AS source_title, z.eli AS url "
            "FROM clan c JOIN akt z ON z.slug = c.law_slug "
            "WHERE c.chunk_id = :cid"
        )
        rows = self._kg_rows(q, {"cid": cid})
        if not rows:
            return f"члан {broj} закона {law_slug} није пронађен", []
        chunks = [row_to_retrieved(r, score=1.0, stage="agent-get_clan") for r in rows]
        return self._format_chunks(chunks), chunks

    def _source_id(self, slug: str) -> str:
        law = next((law for law in self._laws() if law["slug"] == slug), None)
        return law["source_id"] if law else slug

    def _get_bylaws(
        self, law_slug: str, query: str, as_of: date | None
    ) -> tuple[str, list[RetrievedChunk]]:
        """The graph arm's law→bylaw hop as a callable: PRAVNI_OSNOV_ZA →
        scoped vector sub-search within each bylaw for `query`."""
        if not law_slug or self.kg_conn is None:
            return "недостаје law_slug", []
        if not query.strip():
            return "недостаје query (кључне речи за претрагу подзаконских аката)", []
        sid = law_slug if "/" in law_slug else self._source_id(law_slug)
        # BYLAW_ACTS × BYLAW_SUB_K = 12 ≤ POOL_CAP/2: the flood can no longer
        # wipe the pool (h06), and matches the graph arm's BYLAW_PER_LAW fan-out.
        bylaws = self._kg_rows(_BYLAW_QUERY, {"sid": sid, "lim": BYLAW_ACTS})
        if not bylaws:
            return f"закон {law_slug} нема подзаконских аката у корпусу", []
        vector = self.embedder.embed([query], task_type="RETRIEVAL_QUERY").vectors[0]
        chunks: list[RetrievedChunk] = []
        for b in bylaws:
            safe = b["source_id"].replace("'", "''")
            where = f"source_id = '{safe}'"
            if as_of is None:
                where += " AND is_version_slice = false"  # slice-flood guard
            try:
                sub = (
                    self.table.search(vector)
                    .where(where)
                    .limit(BYLAW_SUB_K)
                    .to_list()
                )
            except Exception:
                sub = []
            for r in sub:
                if as_of is not None and self.config.temporal_filter and not in_force_on(r, as_of):
                    continue
                r["source_title"] = b.get("title") or r.get("source_title", "")
                r["url"] = b.get("eli") or ""
                chunks.append(row_to_retrieved(r, score=0.7, stage="agent-bylaw"))
        if self.config.diversity_cap:
            chunks = cap_per_act(
                chunks, id_of=lambda c: c.chunk_id, kind_of=lambda c: c.vrsta
            )  # kind-aware (bylaws are never primary, so the cap shape is unchanged here)
        if not chunks:
            return f"нема релевантних чланова у подзаконским актима закона {law_slug}", []
        return self._format_chunks(chunks), chunks

    def _check_constitutionality(self, law_slug: str) -> tuple[str, list[RetrievedChunk]]:
        """P2: Constitutional-Court holdings against a law (OCENA_USTAVNOSTI →
        the decision's izreka). Surfaces whether a provision was struck."""
        if not law_slug or self.kg_conn is None:
            return "недостаје law_slug", []
        sid = law_slug if "/" in law_slug else self._source_id(law_slug)
        rows = self._kg_rows(_CC_QUERY, {"sid": sid, "lim": 4})
        if not rows:
            return f"нема одлука Уставног суда против закона {law_slug} у корпусу", []
        chunks = [row_to_retrieved(r, score=0.8, stage="agent-cc") for r in rows]
        return self._format_chunks(chunks), chunks

    def _expand_references(self, chunk_id: str) -> tuple[str, list[RetrievedChunk]]:
        if not chunk_id or self.kg_conn is None:
            return "недостаје chunk_id", []
        rows = self._kg_rows(_REF_QUERY, {"cid": chunk_id, "lim": 8})
        chunks = []
        for r in rows:
            meta = self._kg_rows(_TITLE_QUERY, {"slug": r.get("law_slug", "")})
            r["source_title"] = meta[0]["title"] if meta else r.get("law_slug", "")
            r["url"] = meta[0]["eli"] if meta else ""
            chunks.append(row_to_retrieved(r, score=0.8, stage="agent-expand"))
        if not chunks:
            return f"члан {chunk_id} не упућује ни на један члан у корпусу", []
        return self._format_chunks(chunks), chunks

    def _law_timeline(self, law_slug: str) -> str:
        # Accept the corpus slug OR the ELI source_id (the id the policy reads
        # off chunk_ids) — every other tool resolves both; without this the
        # timeline was a dead tool in exactly the temporal traces (e18/h02).
        law = next(
            (law for law in self._laws()
             if law["slug"] == law_slug or law.get("source_id") == law_slug),
            None,
        )
        if law is None:
            return f"закон {law_slug} није у корпусу"
        chain = ", ".join(law.get("gazette_chain", []))
        valid_to = law.get("law_valid_to") or ""
        repealed = law.get("law_repealed_by")
        status = (
            f"ПРЕСТАО ДА ВАЖИ {valid_to} ({repealed})"
            if valid_to
            else "на снази"
        )
        # the three temporal dates kept distinct: publication,
        # entry into force, start of application
        published = law.get("law_published") or "?"
        eif = law.get("law_valid_from") or "?"
        applies = law.get("applies_from") or eif
        return (
            f"{law['title']} — гласници: [{chain}]; чланова: {law.get('main_clanovi', '?')} "
            f"(брисаних: {law.get('repealed_clanovi', 0)}); објављен: {published}; "
            f"на снази од: {eif}; у примени од: {applies}; статус: {status}"
        )

    def _list_laws(self) -> str:
        # scope to seed + backbone laws, not the hundreds of referenced
        # bylaws/stubs — the agent's corpus-scope check (for calibrated
        # abstention) should reason over the laws, not their bylaw periphery.
        laws = [law for law in self._laws() if law.get("role") in _SEED_ROLES]
        return "; ".join(f"{law['slug']} ({law['title']})" for law in laws)

    # — helpers —
    def _kg_rows(self, query: str, params: dict) -> list[dict]:
        return self.kg_conn.query(query, params)

    @staticmethod
    def _format_chunks(chunks: list[RetrievedChunk]) -> str:
        if not chunks:
            return NO_RESULTS
        out = []
        for c in chunks:
            validity = f" [ПРЕСТАО ДА ВАЖИ {c.valid_to}]" if (c.repealed or c.valid_to) else ""
            # P1: flag delegating articles so the agent knows to call get_bylaws
            deleg = " [делегира детаље на подзаконски акт → get_bylaws]" if c.delegates_to_bylaw else ""
            text = c.text
            # CC holdings open with ~430 chars of court-composition boilerplate;
            # start the snippet at the operative part so it reaches the policy
            # (keyed on the chunk, not the tool — holdings also arrive via search).
            if c.chunk_id.endswith("/holding"):
                op = text.find("Утврђује се")
                if op > 0:
                    text = text[op:]
            out.append(
                f"• {c.source_title} — {c.fragment}{validity}{deleg} (id={c.chunk_id})\n"
                f"  {text[:OBS_SNIPPET]}"
            )
        return "\n".join(out)

    @staticmethod
    def _args_str(action: dict) -> str:
        return ", ".join(
            f"{k}={action[k]!r}"
            for k in ("query", "law_slug", "broj", "chunk_id")
            if action.get(k)
        )
