"""Shared arm plumbing: context formatting, generation, instrumentation.

Every arm produces an ArmResult with the same shape so runs are comparable:
the kl_core Answer (citations mandatory unless abstained), the retrieved
chunks with scores and the stage that surfaced them, per-stage timings, and
token/cost accounting. The generator model is fixed across arms — the arms
are the treatments, the generator is controlled.
"""

from __future__ import annotations

import json
import time
from datetime import date
from typing import Any

from pydantic import BaseModel, Field

from kl_core.llm import LLMClient, generate_json
from kl_core.types import Answer, Provenance
from lexsr.config import CORPUS_DIR
from lexsr.translit import to_cyrillic

# — smart single-stage retrieval backbone (shared by advanced + the graph seeds) —
# Advanced *is* this pipeline; the graph arm's seeds *are* this pipeline (rag_arms
# §1/§3), so it lives here and both call it — keeping the ladder monotonic (graph
# ⊇ advanced) and the comparison controlled (same retriever + reranker, the
# fair-comparison principle; research/rag_arm_architectures.md).
CANDIDATES_PER_LEG = 24  # per-leg candidate pool (dense + BM25) before fusion
RERANK_POOL = 16  # top-RRF candidates handed to the listwise reranker
RRF_K = 60  # Reciprocal Rank Fusion constant (Cormack et al. 2009)
SMART_TOP_K = 8  # deployable single-stage top-k — advanced's output AND the graph seed count

RERANK_SCHEMA = {
    "type": "object",
    "properties": {"ranked": {"type": "array", "items": {"type": "integer"}}},
    "required": ["ranked"],
}

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "citations": {"type": "array", "items": {"type": "integer"}},
        "abstained": {"type": "boolean"},
        "abstain_reason": {"type": "string"},
    },
    "required": ["answer", "citations", "abstained"],
}

CONDENSE_SCHEMA = {
    "type": "object",
    "properties": {"standalone_query": {"type": "string"}},
    "required": ["standalone_query"],
}

# A conversation turn as the client sends it; kept deliberately minimal so the
# memory mechanism is "the transcript", not a server-side session object.
Turn = dict[str, str]  # {"role": "user"|"assistant", "content": str}
_HISTORY_WINDOW = 6  # turns of context carried into condensation + generation

SYSTEM_PROMPT = """Ти си правни асистент за прописе Републике Србије.
Одговараш ИСКЉУЧИВО на основу приложених извода из прописа (контекст).
Правила:
1. Свака тврдња мора бити поткрепљена наводом — у поље citations стави индексе извода које си користио.
2. Ако контекст не садржи одговор, постави abstained=true и објасни зашто у abstain_reason. НИКАД не измишљај садржај прописа.
3. Пази на временску важност: ако је извод означен као престао да важи (брисан/неважећи), то реци експлицитно — застарели пропис није одговор на питање о важећем праву.
4. Одговарај на српском, прецизно и сажето, са назнаком закона и члана на који се ослањаш."""


class AblationConfig(BaseModel):
    """Per-run ablation toggles — each isolates exactly one retrieval capability
    of the attribution ladder (rag_arms.md §1), so a metric delta between a run
    and its ablated twin is attributable to that one capability.

    Every field defaults to the **production** setting, so the default config
    reproduces each arm's full pipeline unchanged; flipping a field off removes
    that capability. naive (the deliberately-flat floor) and context-stuffing
    (the oracle ceiling) have no toggles by design — ablating the floor/ceiling
    would change what they *are*. Cross-cutting toggles (`temporal_filter`,
    `diversity_cap`) are read by every engineered arm that has the stage; the
    rest are arm-specific. The exact config rides every run's telemetry
    (`extras.ablation` / the `ablation` column) so a result is self-describing.
    """

    model_config = {"extra": "forbid"}

    # — cross-cutting (advanced · graph · agentic) —
    temporal_filter: bool = True  # apply the as_of in-force filter (invariant 2)
    diversity_cap: bool = True  # per-act / MMR diversity cap
    # — advanced —
    hybrid: bool = True  # dense + BM25/FTS leg; off → dense-only (DPR-only)
    rerank: bool = True  # listwise LLM rerank; off → RRF fusion order
    # — graph —
    graph_expand: bool = True  # 2-hop UPUCUJE_NA / DEFINED_IN expansion
    bylaw_hop: bool = True  # law→bylaw PRAVNI_OSNOV_ZA hop + scoped sub-search
    delegation_gate: bool = True  # gate the bylaw hop on delegates_to_bylaw (§6)
    cc_path: bool = True  # OCENA_USTAVNOSTI constitutional-validity path
    related_law_hop: bool = True  # POVEZAN_SA related-law hop (the experiment)
    # — agentic —
    seed_search: bool = True  # seed the pool with one raw-question search before
    # the loop — the agent refines instead of re-deriving retrieval from its own
    # reformulations (which measurably miss golds the raw question finds); makes
    # the agentic arm a retrieval superset of the flat leg, mirroring the

    def nondefault(self) -> dict[str, bool]:
        """The fields that differ from production defaults — the ablation's identity."""
        base = AblationConfig().model_dump()
        return {k: v for k, v in self.model_dump().items() if v != base[k]}

    def label(self) -> str:
        """Compact human-readable id for telemetry/UI: `default`, or e.g.
        `no-rerank,no-temporal_filter` (all toggles default True → off reads `no-`)."""
        nd = self.nondefault()
        if not nd:
            return "default"
        return ",".join((f"no-{k}" if v is False else f"{k}=on") for k, v in nd.items())


# The ablation surface as data — the single source the API exposes (`/api/ablations`)
# so the UI renders toggles generically and stays in sync with AblationConfig.
ABLATION_SPEC: list[dict] = [
    {"key": "temporal_filter", "label": "Temporal filter", "group": "cross-cutting",
     "arms": ["advanced", "graph", "agentic"],
     "description": "Apply the as_of in-force filter (drop out-of-force units). Invariant 2."},
    {"key": "diversity_cap", "label": "Diversity cap", "group": "cross-cutting",
     "arms": ["advanced", "graph", "agentic"],
     "description": "Per-act MMR cap on the uncapped bylaw layer."},
    {"key": "hybrid", "label": "Hybrid (BM25 leg)", "group": "advanced", "arms": ["advanced", "graph"],
     "description": "Dense + sparse (BM25) hybrid; off → dense-only retrieval "
                    "(graph inherits via the shared backbone + scoped sub-search)."},
    {"key": "rerank", "label": "Listwise rerank", "group": "advanced", "arms": ["advanced", "graph"],
     "description": "Listwise LLM rerank of the RRF pool; off → keep RRF order "
                    "(graph inherits via the shared backbone seeds)."},
    {"key": "graph_expand", "label": "2-hop expansion", "group": "graph", "arms": ["graph"],
     "description": "Bounded 2-hop UPUĆUJE_NA / DEFINED_IN traversal off the seeds."},
    {"key": "bylaw_hop", "label": "Law→bylaw hop", "group": "graph", "arms": ["graph"],
     "description": "PRAVNI_OSNOV_ZA hop into bylaws + scoped sub-search."},
    {"key": "delegation_gate", "label": "Delegation gate", "group": "graph", "arms": ["graph"],
     "description": "Fire the bylaw hop only from delegates_to_bylaw seeds; off → all seeds (§6)."},
    {"key": "cc_path", "label": "CC-validity path", "group": "graph", "arms": ["graph"],
     "description": "OCENA_USTAVNOSTI → the Constitutional-Court holding's izreka."},
    {"key": "related_law_hop", "label": "Related-law hop", "group": "graph", "arms": ["graph"],
     "description": "Guarded POVEZAN_SA cross-law hop (the experiment)."},
    {"key": "seed_search", "label": "Raw-question seed search", "group": "agentic",
     "arms": ["agentic"],
     "description": "Seed the pool with one search of the raw question before the ReAct loop; "
                    "off → the loop retrieves only via its own reformulated queries."},
]


class RetrievedChunk(BaseModel):
    """One retrieved unit, as the arm saw it."""

    chunk_id: str
    score: float
    stage: str  # which retrieval stage surfaced it: vector / fts / rerank / graph-expand
    source_title: str
    fragment: str
    text: str
    valid_from: str = ""  # entry into force (stupanje na snagu, Q8)
    valid_to: str = ""
    applies_from: str = ""  # PIS start-of-application date (početak primene)
    published: str = ""  # base-gazette publication date (objavljivanje)
    status: str = ""  # in_force | repealed
    structural_path: str = ""  # heading breadcrumb (Глава › пододељак)
    article_heading: str = ""  # naziv člana
    repealed: bool = False
    url: str = ""
    version: str = ""
    vrsta: str = ""  # act type (zakon/ustav/podzakonski_akt…) — drives the bylaw-aware diversity cap
    delegates_to_bylaw: bool = False  # article delegates detail to a bylaw → gates the law→bylaw hop (P1)


class ArmResult(BaseModel):
    arm: str
    question: str
    as_of: str | None = None
    answer: Answer
    retrieved: list[RetrievedChunk]
    timings_ms: dict[str, float] = Field(default_factory=dict)
    prompt_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""
    extras: dict[str, Any] = Field(default_factory=dict)


def format_context(retrieved: list[RetrievedChunk], *, annotate_validity: bool) -> str:
    blocks = []
    for i, r in enumerate(retrieved):
        header = f"[{i}] {r.source_title} — {r.fragment}"
        if r.structural_path:
            header += f" [{r.structural_path}]"
        if annotate_validity:
            if r.repealed or r.valid_to:
                header += f" (ПРЕСТАО ДА ВАЖИ {r.valid_to or '— датум у изводу'})"
            elif r.valid_from:
                header += f" (на снази од {r.valid_from}"
                header += f", измењен {r.version}" if r.version else ""
                # начело примене (vacatio legis) кад се разликује од ступања на снагу
                if r.applies_from and r.applies_from > r.valid_from:
                    header += f", у примени од {r.applies_from}"
                header += ")"
        blocks.append(f"{header}\n{r.text}")
    return "\n\n".join(blocks)


def _render_history(history: list[Turn]) -> str:
    """Compact, role-labelled transcript of the recent conversation."""
    labels = {"user": "Корисник", "assistant": "Асистент"}
    lines = []
    for turn in history[-_HISTORY_WINDOW:]:
        role = labels.get(turn.get("role", ""), turn.get("role", ""))
        lines.append(f"{role}: {turn.get('content', '')[:600]}")
    return "\n".join(lines)


def condense_query(
    llm: LLMClient, history: list[Turn], question: str, timer: Timer
) -> tuple[str, dict]:
    """Rewrite a follow-up into a standalone retrieval query using the
    conversation so far — „а за стране држављане?" → „Колико дана годишњег
    одмора имају страни држављани запослени у Србији?". Single-turn questions
    are returned unchanged. Cost is folded into the run's accounting."""
    convo = _render_history(history)
    prompt = (
        "Дат је досадашњи разговор и наставно питање корисника. Преформулиши "
        "наставно питање у САМОСТАЛНО питање на српском које се може разумети "
        "без разговора — разреши заменице и изостављене појмове користећи разговор. "
        "Пример: претходно питање о годишњем одмору, наставно „а за стране "
        "држављане?“ → „Колики је годишњи одмор за стране држављане?“. Ако је "
        "питање већ самостално, врати га непромењено. Не одговарај на питање.\n\n"
        f"Разговор:\n{convo}\n\nНаставно питање: {question}\n\n"
        "Врати JSON са пољем standalone_query."
    )
    # 4096: a thinking model spends part of a short budget on thoughts (256 and
    # then 1024 truncated in practice); retries + the cap make truncation rare.
    with timer.stage("condense_ms"):
        parsed, acct = generate_json(
            llm, prompt, json_schema=CONDENSE_SCHEMA, max_output_tokens=4096
        )
    standalone = ((parsed or {}).get("standalone_query") or "").strip()
    info = {
        "prompt_tokens": acct.prompt_tokens,
        "output_tokens": acct.output_tokens,
        "cost_usd": acct.cost_usd,
        "llm_io": acct.io_summary(),
    }
    return (standalone or question), info


def generate_answer(
    llm: LLMClient,
    question: str,
    retrieved: list[RetrievedChunk],
    *,
    as_of: date | None,
    annotate_validity: bool,
    history: list[Turn] | None = None,
) -> tuple[Answer, dict]:
    context = format_context(retrieved, annotate_validity=annotate_validity)
    date_clause = (
        f"\nПитање се односи на стање прописа на дан {as_of.isoformat()}." if as_of else ""
    )
    history_clause = (
        f"Претходни разговор (питање може да се ослања на њега):\n{_render_history(history)}\n\n"
        if history
        else ""
    )
    prompt = (
        f"{history_clause}Контекст (изводи из прописа):\n\n{context}\n\n"
        f"Питање: {question}{date_clause}"
    )
    # 32768: thinking models spend part of the output budget on thoughts; 2048
    # and then 8192 truncated long answers mid-JSON (observed on ZOO questions;
    # e20 runaway). Paired with the providers' 300 s timeout ceiling; the retry
    # helper never blind-retries a truncated (deterministic) generation.
    parsed, acct = generate_json(
        llm, prompt, system=SYSTEM_PROMPT, json_schema=ANSWER_SCHEMA,
        max_output_tokens=32768,
    )
    generation_failed = parsed is None
    if generation_failed:
        # Unparseable after retries — an abstention-by-failure in the answer
        # shape (UI-safe), but flagged so evals route it to errors, not to the
        # abstention metrics.
        parsed = {
            "answer": "",
            "citations": [],
            "abstained": True,
            "abstain_reason": (
                f"generation failed after {acct.attempts} attempts "
                f"(finish_reason={acct.finish_reason}, {acct.output_tokens} output tokens)"
            ),
        }

    cited_indices = [i for i in parsed.get("citations", []) if 0 <= i < len(retrieved)]
    citations = [_provenance(retrieved[i]) for i in cited_indices]
    abstained = bool(parsed.get("abstained", False))
    if not citations and not abstained:
        # kl_core invariant: no claim without provenance. An answer that
        # cites nothing is recorded as an abstention-by-failure, not a claim.
        abstained = True
        parsed["abstain_reason"] = parsed.get("abstain_reason") or "no citations returned"
    text = parsed.get("answer", "")
    abstain_reason = parsed.get("abstain_reason")
    if abstained:
        # An abstention must always state *why* — never a blank bubble. Guarantee
        # a reason and surface it in the visible answer text when the model left
        # the answer empty (the agentic arm in particular returns no prose).
        abstain_reason = abstain_reason or "Контекст не садржи податке потребне за одговор."
        if not text.strip():
            text = f"Уздржавам се од одговора. Разлог: {abstain_reason}"
    answer = Answer(
        text=text,
        citations=citations,
        abstained=abstained,
        metadata={
            "abstain_reason": abstain_reason,
            "cited_indices": cited_indices,
        },
    )
    gen_info = {
        "llm_ms": acct.latency_ms,
        "prompt_tokens": acct.prompt_tokens,
        "output_tokens": acct.output_tokens,
        "cost_usd": acct.cost_usd,
        "model": acct.model if acct.result is not None else getattr(llm, "model", "unknown"),
        "llm_io": acct.io_summary(),
        "generation_failed": generation_failed,
    }
    return answer, gen_info


def in_force_on(row: dict, as_of: date) -> bool:
    iso = as_of.isoformat()
    valid_from = row.get("valid_from") or ""
    valid_to = row.get("valid_to") or ""
    return (not valid_from or valid_from <= iso) and (not valid_to or iso < valid_to)


# — shared retrieval primitives (diversity, as_of slice routing) —
PER_ACT_CAP = 3  # at most N chunks per *secondary* (bylaw) act in a result set
# Primary legislation is exempt from the diversity cap: one statute legitimately
# supplies several articles to a single answer (multi-article lookups — a rule +
# its exception + a definition), so capping it starves recall. The crowding the
# cap exists to prevent is the *uncapped bylaw layer* (Carbonell &
# Goldstein 1998 — diversity is anti-redundancy, not a relevance ceiling on the
# governing act). So the cap applies only to bylaws and other secondary acts.
PRIMARY_VRSTA = frozenset({"zakon", "ustav"})


def _act_of(chunk_id: str) -> str:
    """The owning act's source_id, stripped of the chunk suffix."""
    for sep in ("/clan-", "/prilog-", "/holding"):
        if sep in chunk_id:
            return chunk_id.split(sep)[0]
    return chunk_id


def cap_per_act(items: list, *, cap: int = PER_ACT_CAP, id_of=lambda x: x, kind_of=None) -> list:
    """Diversity cap: keep at most `cap` chunks per *secondary* source act, order
    preserved — stops the uncapped bylaw layer from crowding top-k with
    near-duplicate implementing text. **Primary legislation (zakon/ustav)
    is exempt**: a single statute can legitimately contribute many articles to one
    answer, so capping it would drop gold articles on multi-article questions.
    `id_of` extracts the chunk_id; `kind_of` returns an item's act type (`vrsta`)
    — when None the cap applies uniformly (back-compat for callers without type)."""
    seen: dict[str, int] = {}
    out = []
    for it in items:
        if kind_of is not None and kind_of(it) in PRIMARY_VRSTA:
            out.append(it)  # never cap the governing statute's own articles
            continue
        k = _act_of(id_of(it))
        if seen.get(k, 0) >= cap:
            continue
        seen[k] = seen.get(k, 0) + 1
        out.append(it)
    return out


def route_slices(rows: list[dict], as_of: date | None) -> list[dict]:
    """A član has one current row + dated `@version` slice rows sharing its
    number. With no `as_of`, keep only the current text (drop slices) so top-k
    isn't doubled; with an `as_of`, `in_force_on` already selects the right slice."""
    if as_of is not None:
        return rows
    return [r for r in rows if not r.get("is_version_slice")]


def row_to_retrieved(row: dict, *, score: float, stage: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=row["chunk_id"],
        score=score,
        stage=stage,
        source_title=row.get("source_title", ""),
        fragment=row.get("fragment", ""),
        text=row.get("text", ""),
        valid_from=row.get("valid_from", "") or "",
        valid_to=row.get("valid_to", "") or "",
        applies_from=row.get("applies_from", "") or "",
        published=row.get("published", "") or "",
        status=row.get("status", "") or "",
        structural_path=row.get("structural_path", "") or "",
        article_heading=row.get("article_heading", "") or "",
        repealed=bool(row.get("repealed", False)),
        url=row.get("url", "") or "",
        version=row.get("version", "") or "",
        vrsta=row.get("vrsta", "") or "",
        delegates_to_bylaw=bool(row.get("delegates_to_bylaw", False)),
    )


# delegation-chain provenance. A materialized bylaw chunk stores no parent
# (its law_slug points at itself); the law→bylaw link lives only in the
# `PRAVNI_OSNOV_ZA` edges. We denormalize those edges into a {bylaw_source_id →
# (parent_slug, parent_title)} map *at render time* so a bylaw citation renders
# as "Pravilnik X · донет на основу: Закон Y" for every arm — not just the
# hierarchical ones that happened to traverse the edge. No re-embed: the map is
# built from plain-git corpus files, cached once. The delegating *article* (čl. Z)
# is not captured in the edge data and is deferred.
_PARENT_MAP: dict[str, tuple[str, str]] | None = None


def _parent_map() -> dict[str, tuple[str, str]]:
    global _PARENT_MAP
    if _PARENT_MAP is None:
        out: dict[str, tuple[str, str]] = {}
        try:
            manifest = json.loads((CORPUS_DIR / "manifest.json").read_text(encoding="utf-8"))
            title_by_slug = {m["slug"]: m.get("title", "") for m in manifest}
            with open(CORPUS_DIR / "references.jsonl", encoding="utf-8") as f:
                for line in f:
                    e = json.loads(line)
                    if e.get("edge") != "PRAVNI_OSNOV_ZA":
                        continue
                    bylaw_sid, parent_slug = e.get("to"), e.get("from_slug")
                    # first parent wins — most bylaws delegate from a single law,
                    # and the map is for display, not exhaustive lineage.
                    if bylaw_sid and parent_slug and bylaw_sid not in out:
                        out[bylaw_sid] = (parent_slug, title_by_slug.get(parent_slug, parent_slug))
        except FileNotFoundError:
            pass  # no corpus yet (fresh checkout / tests that don't touch it)
        _PARENT_MAP = out
    return _PARENT_MAP


def _provenance(r: RetrievedChunk) -> Provenance:
    # _act_of, not a bare "/clan-" rsplit: holding (CC izreka) and prilog
    # (annex) chunks must also strip to the act — otherwise their citation key
    # becomes "ref/…/holding::izreka", which can never match the gold
    # "ref/…::izreka" and the cc_validity tier mis-scores for every arm.
    source_id = _act_of(r.chunk_id)
    parent = _parent_map().get(source_id)
    return Provenance(
        source_id=source_id,
        source_title=r.source_title,
        fragment=r.fragment,
        version=r.version or None,
        valid_from=date.fromisoformat(r.valid_from) if r.valid_from else None,
        valid_to=date.fromisoformat(r.valid_to) if r.valid_to else None,
        url=r.url or None,
        parent_source_id=parent[0] if parent else None,
        parent_title=parent[1] if parent else None,
    )


class Timer:
    """Accumulates named stage timings in milliseconds."""

    def __init__(self) -> None:
        self.stages: dict[str, float] = {}
        self._started = time.perf_counter()

    def stage(self, name: str):
        timer = self

        class _Ctx:
            def __enter__(self):
                self._t0 = time.perf_counter()

            def __exit__(self, *exc):
                timer.stages[name] = timer.stages.get(name, 0.0) + (
                    (time.perf_counter() - self._t0) * 1000
                )

        return _Ctx()

    def total(self) -> float:
        return (time.perf_counter() - self._started) * 1000


class RetrievalArm:
    """Base class: subclasses implement retrieve(); answer() is shared."""

    name = "base"
    annotate_validity = True

    def __init__(
        self, llm: LLMClient, embedder, table, kg_conn=None,
        config: AblationConfig | None = None,
    ) -> None:
        self.llm = llm
        self.embedder = embedder
        self.table = table
        self.kg_conn = kg_conn
        # The ablation config is always present (default = full production pipeline)
        # so arms read self.config.<toggle> without None-checks.
        self.config = config or AblationConfig()

    def retrieve(
        self, question: str, *, as_of: date | None, timer: Timer
    ) -> tuple[list[RetrievedChunk], dict]:
        raise NotImplementedError

    def answer(
        self,
        question: str,
        *,
        as_of: date | None = None,
        history: list[Turn] | None = None,
        **hints,
    ) -> ArmResult:
        # hints are arm-specific retrieve() kwargs (e.g. the stuffing arm's
        # oracle law_slug); arms that don't declare them reject them loudly.
        # `history` (prior conversation turns) makes follow-ups work: the
        # follow-up is condensed into a standalone retrieval query, while the
        # original question + transcript steer generation.
        timer = Timer()
        retrieval_query, condense_info = question, {}
        if history:
            retrieval_query, condense_info = condense_query(self.llm, history, question, timer)
        retrieved, extras = self.retrieve(retrieval_query, as_of=as_of, timer=timer, **hints)
        answer, gen_info = generate_answer(
            self.llm, question, retrieved,
            as_of=as_of, annotate_validity=self.annotate_validity, history=history,
        )
        timer.stages["llm_ms"] = gen_info["llm_ms"]
        timer.stages["total_ms"] = timer.total()
        if history:
            extras.setdefault("condensed_query", retrieval_query)
        # Full-process LLM observability (one chokepoint): per-stage attempt
        # counts, failures, and finish reasons ride every run's telemetry.
        llm_io: dict = {"answer": gen_info["llm_io"]}
        if condense_info.get("llm_io"):
            llm_io["condense"] = condense_info["llm_io"]
        rerank_io = extras.pop("_llm_io_rerank", None)
        if rerank_io:
            llm_io["rerank"] = rerank_io
        decision_io = extras.pop("_llm_io_decision", None)
        if decision_io:
            llm_io["decision"] = decision_io
        extras["llm_io"] = llm_io
        if gen_info.get("generation_failed"):
            # top-level flag: evals route these to errors, runstore excludes
            # them from abstention metrics
            extras["generation_failed"] = True
        # Make every run self-describing: the exact ablation config that produced
        # it rides the telemetry (one chokepoint, uniform across all arms).
        extras["ablation"] = self.config.model_dump()
        extras["ablation_label"] = self.config.label()
        return ArmResult(
            arm=self.name,
            question=question,
            as_of=as_of.isoformat() if as_of else None,
            answer=answer,
            retrieved=retrieved,
            timings_ms=timer.stages,
            prompt_tokens=(
                gen_info["prompt_tokens"]
                + extras.pop("_extra_prompt_tokens", 0)
                + condense_info.get("prompt_tokens", 0)
            ),
            output_tokens=(
                gen_info["output_tokens"]
                + extras.pop("_extra_output_tokens", 0)
                + condense_info.get("output_tokens", 0)
            ),
            cost_usd=(
                gen_info["cost_usd"]
                + extras.pop("_extra_cost_usd", 0.0)
                + condense_info.get("cost_usd", 0.0)
            ),
            model=gen_info["model"],
            extras=extras,
        )

    def _embed_query(self, question: str, timer: Timer) -> list[float]:
        with timer.stage("embed_query_ms"):
            return self.embedder.embed([question], task_type="RETRIEVAL_QUERY").vectors[0]

    # — shared smart-retrieval backbone (advanced + graph seeds) —

    def _hybrid_candidates(
        self, question: str, vector: list[float], *, as_of: date | None, timer: Timer
    ) -> tuple[list[dict], dict]:
        """Dense + BM25 (hybrid) → RRF fuse → version-route → temporal in-force
        filter. Returns `(fused_rows, info)` where info carries `scores`, `legs`
        (which leg surfaced each cid), and the temporal diagnostics. The per-act
        diversity cap and the listwise rerank are applied by the caller — they
        differ per arm. `hybrid`/`temporal_filter` ablations are honored here."""
        # at the source: with no as_of, dated @version slices are never
        # wanted — post-hoc routing (route_slices) is not enough, because a
        # heavily-amended article floods the top-k with near-identical slices
        # and pushes the *current* rows out of the candidate window entirely
        # (the paušal regression: porez-dohodak čl. 40–42 slices filled all 16
        # vector slots, routing emptied the leg, the arm abstained).
        slice_filter = None if as_of is not None else "is_version_slice = false"
        with timer.stage("vector_ms"):
            vq = self.table.search(vector)
            if slice_filter:
                vq = vq.where(slice_filter)
            vec_rows = vq.limit(CANDIDATES_PER_LEG).to_list()
        with timer.stage("fts_ms"):
            if not self.config.hybrid:
                fts_rows = []
            else:
                try:
                    # corpus is Cyrillic; transliterate Latin-script queries so the
                    # BM25 leg matches (the vector leg gets the original text)
                    fq = self.table.search(to_cyrillic(question), query_type="fts")
                    if slice_filter:
                        fq = fq.where(slice_filter)
                    fts_rows = fq.limit(CANDIDATES_PER_LEG).to_list()
                except Exception:
                    fts_rows = []  # FTS tokenization can choke; vector leg carries
        fused: dict[str, dict] = {}
        scores: dict[str, float] = {}
        legs: dict[str, set[str]] = {}
        for leg, rows in (("vector", vec_rows), ("fts", fts_rows)):
            for rank, row in enumerate(rows):
                cid = row["chunk_id"]
                fused.setdefault(cid, row)
                scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
                legs.setdefault(cid, set()).add(leg)
        candidates = sorted(fused.values(), key=lambda r: scores[r["chunk_id"]], reverse=True)
        candidates = route_slices(candidates, as_of)  # one version per article
        dropped, starved = 0, False
        if as_of is not None and self.config.temporal_filter:
            kept = [r for r in candidates if in_force_on(r, as_of)]
            dropped = len(candidates) - len(kept)
            # Don't starve generation: if the filter empties the pool, keep the top
            # RRF candidates (validity annotated in context so the model can still
            # say „… на дан X није било на снази") rather than abstaining outright.
            if kept:
                candidates = kept
            else:
                starved = True
        return candidates, {
            "scores": scores, "legs": legs,
            "dropped_by_temporal_filter": dropped, "temporal_filter_starved": starved,
        }

    def _listwise_rerank(
        self, question: str, candidates: list[dict], *, timer: Timer
    ) -> tuple[list[int], dict]:
        """Zero-shot listwise LLM rerank (Sun et al., EMNLP 2023) of fused candidate
        rows. Returns `(order, info)` where order indexes `candidates` best-first.
        Order-sensitive (Tang et al. 2024), so used only to re-order an
        already-recalled pool — never to manufacture recall."""
        if len(candidates) <= 1:
            return list(range(len(candidates))), {}
        listing = "\n\n".join(
            f"[{i}] {r.get('source_title', '')} — {r.get('fragment', '')}\n{r.get('text', '')[:500]}"
            for i, r in enumerate(candidates)
        )
        prompt = (
            f"Питање: {question}\n\nКандидати (изводи из прописа):\n\n{listing}\n\n"
            f"Поређај индексе кандидата од најрелевантнијег ка најмање релевантном за ово питање. "
            f"Врати JSON са пољем ranked."
        )
        with timer.stage("rerank_ms"):
            parsed, acct = generate_json(
                self.llm, prompt, json_schema=RERANK_SCHEMA, max_output_tokens=2048
            )
        raw_ranked = (parsed or {}).get("ranked") or []
        ranked = [i for i in raw_ranked
                  if isinstance(i, int) and 0 <= i < len(candidates)]
        seen = set(ranked)
        order = ranked + [i for i in range(len(candidates)) if i not in seen]
        return order, {
            "model": acct.model, "prompt_tokens": acct.prompt_tokens,
            "output_tokens": acct.output_tokens, "cost_usd": acct.cost_usd,
            "llm_io": acct.io_summary(),
        }

    def _smart_retrieve(
        self, question: str, vector: list[float], *, as_of: date | None, timer: Timer,
        top_k: int = SMART_TOP_K,
    ) -> tuple[list[RetrievedChunk], dict]:
        """The full single-stage smart pipeline — advanced's retrieval, and the
        graph arm's *seeds*: hybrid_candidates → rerank-pool → listwise rerank →
        diversity cap → top_k. Structure-free by construction, so the graph arm is
        exactly this + structural expansion (rag_arms §1/§3). The rerank is skipped
        when the pool already fits `top_k` (nothing to re-order) — which also keeps
        the no-LLM unit tests honest."""
        candidates, cinfo = self._hybrid_candidates(question, vector, as_of=as_of, timer=timer)
        scores, legs = cinfo["scores"], cinfo["legs"]
        pool = candidates[:RERANK_POOL]
        rerank_info: dict = {}
        if self.config.rerank and len(pool) > top_k:
            order, rerank_info = self._listwise_rerank(question, pool, timer=timer)
            pool = [pool[i] for i in order]
        if self.config.diversity_cap:
            pool = cap_per_act(
                pool, id_of=lambda r: r["chunk_id"], kind_of=lambda r: r.get("vrsta", "")
            )
        suffix = "+rerank" if (self.config.rerank and rerank_info) else ""
        retrieved = [
            row_to_retrieved(
                row, score=scores[row["chunk_id"]],
                stage="+".join(sorted(legs[row["chunk_id"]])) + suffix,
            )
            for row in pool[:top_k]
        ]
        info = {
            "dropped_by_temporal_filter": cinfo["dropped_by_temporal_filter"],
            "temporal_filter_starved": cinfo["temporal_filter_starved"],
            # per-run leg counts: the FTS/vector legs fail SILENTLY by design
            # (except→[] fallbacks), so a persisted counter is the only way a
            # dashboard can see hybrid degrade to single-leg (vector-store
            # reassessment §3.3). fts=0 on a Cyrillic query is the tripwire.
            "leg_counts": {
                "vector": sum(1 for s in legs.values() if "vector" in s),
                "fts": sum(1 for s in legs.values() if "fts" in s),
            },
            "rerank_model": rerank_info.get("model"),
            "_extra_prompt_tokens": rerank_info.get("prompt_tokens", 0),
            "_extra_output_tokens": rerank_info.get("output_tokens", 0),
            "_extra_cost_usd": rerank_info.get("cost_usd", 0.0),
            "_llm_io_rerank": rerank_info.get("llm_io"),
        }
        return retrieved, info
