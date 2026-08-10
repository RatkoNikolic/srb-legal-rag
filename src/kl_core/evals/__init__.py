"""Task-grounded eval harness.

Scores what the system *did for the task*, not whether the right chunk came
back. Scoring protocol (v0, against a gold reference per item):

- **Answer correctness** — reference-guided LLM-as-a-judge (Zheng et al.,
  2023, "Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena", NeurIPS):
  the judge compares the candidate answer to the gold answer fact-by-fact
  and returns correct / partially_correct / incorrect → 1.0 / 0.5 / 0.0,
  with a rationale. Graded (non-binary) scoring follows the same convention
  as RAGAS answer correctness (Es et al., EACL 2024).
- **Provenance fidelity** — citation precision/recall/F1 against gold
  citations, following the citation-quality protocol of ALCE (Gao et al.,
  EMNLP 2023). Deterministic: a cited unit either is or is not among the
  gold provenance ids.
- **Temporal correctness** — for items with an `as_of` date: every cited
  unit's validity interval must cover the date. Deterministic from
  `Provenance.valid_from`/`valid_to`.
- **Abstain accuracy** — items with no gold answer are abstain targets:
  correct iff the system abstained. No judge call.

Latency and $/query are carried through from the instrumented arms, not
recomputed here. One harness, every backend, every domain skin — the judge
prompt is domain-free by design (kl_core invariant).
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from kl_core.llm import LLMClient, generate_json
from kl_core.types import Answer, Provenance, Query

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["correct", "partially_correct", "incorrect"],
        },
        "rationale": {"type": "string"},
    },
    "required": ["verdict", "rationale"],
}

JUDGE_SYSTEM = """You are an impartial grader for a question-answering benchmark.
You will be given a question, a REFERENCE answer (ground truth), and a CANDIDATE answer.
Grade the candidate strictly against the reference, fact by fact (reference-guided grading):
- "correct": every factual claim required by the reference is present and consistent; \
no claim in the candidate contradicts the reference.
- "partially_correct": the key fact is present but a material detail is missing, \
or a secondary claim is wrong.
- "incorrect": the candidate contradicts the reference, misses the key fact, \
or declines to answer when the reference contains an answer.
Judge factual agreement only — ignore style, verbosity, formatting, and language \
(the candidate may answer in a different language than the reference).
Return JSON: {"verdict": ..., "rationale": one short sentence}."""

VERDICT_SCORE = {"correct": 1.0, "partially_correct": 0.5, "incorrect": 0.0}
CORRECT_THRESHOLD = 1.0  # `correct` is strict; `correctness` keeps the graded credit


class TaskItem(BaseModel):
    """One benchmark item: a query, its category, and (gold) ground truth."""

    item_id: str
    query: Query
    category: str  # difficulty tier: easy | medium | hard
    # The *capability* the item exercises — orthogonal to the difficulty
    # tier above: lookup | cross_reference | temporal | amendment_diff |
    # delegated_norm | abstain. Heuristic today; expert-validated in Phase 2.
    capability: str = ""
    # Temporal-grading mode: "in_force" — every cited unit must be in force
    # on `as_of` (the default); "reports_repeal" — the item legitimately cites an
    # out-of-force article to *report* its repeal/invalidation, so out-of-force
    # citations are not penalized (distinguishes authority from subject-of-claim).
    temporal_mode: str = "in_force"
    gold_answer: str | None = None  # None for abstain items
    # citation_keys() format: `source::fragment`, optionally `@version`-pinned
    gold_citations: list[str] = Field(default_factory=list)
    # Construction diagnostic, NOT a filter: True when flat top-k
    # retrieval on the question surfaces *every* gold citation (all-of), False
    # when at least one required unit is missed, None when uncomputed or not
    # applicable (abstain). All-of (not any-of) is deliberate: a multi-cite item
    # only "gives graph no chance" when flat already has every required unit.
    # Reported in methods, never used to include/exclude an item
    # (design/benchmark.md §5 — read both ways: per-item leakage *and* set-level
    # "graph never got a chance").
    reachable_by_flat_topk: bool | None = None
    # The reviewer-legible evidence behind that flag: which gold citations flat
    # top-k surfaced (`hits`) vs missed (`misses`). None when not applicable.
    flat_topk: dict | None = None


class ItemScore(BaseModel):
    item_id: str
    backend: str
    category: str = ""
    capability: str = ""
    correct: bool
    correctness: float  # graded judge score in {0, 0.5, 1}
    provenance_fidelity: float  # citation F1 vs gold (ALCE-style)
    citation_precision: float | None = None
    citation_recall: float | None = None
    temporally_correct: bool | None = None
    abstained: bool = False
    # True when the item is an abstain target (no gold answer); lets the
    # aggregates split answerable vs unanswerable robustly instead of keying on
    # the difficulty tier (which carries no "abstain" value).
    abstain_target: bool = False
    # Retriever-side recall/precision: did the arm's *retrieved pool* (pre-
    # generation) surface the gold units, regardless of whether the generator
    # then cited them. Compared against the citation_recall above, the gap
    # isolates retrieval failure from citation failure (LegalBench-RAG, Pipitone
    # & Alami 2024). None for abstain targets or when the pool isn't supplied.
    retrieval_recall: float | None = None
    retrieval_precision: float | None = None
    # Provenance-safety axis (design/benchmark.md §4; Magesh et al., RegLab, JELS
    # 2025 — "hallucinated = incorrect OR misgrounded"). Deterministic, scoped to
    # *delivered* answers (abstentions are refusals, not hallucinations) → None
    # for abstain targets / abstained responses.
    temporally_wrong_and_cited: bool | None = None  # cited an out-of-force unit as authority
    misgrounded: bool | None = None  # cites sources that don't support / are inapplicable
    hallucinated: bool | None = None  # incorrect OR misgrounded delivered answer
    latency_ms: float | None = None
    cost_usd: float | None = None
    judge_rationale: str | None = None
    judge_model: str | None = None


def citation_key(p: Provenance) -> str:
    """Canonical id a citation is graded on: source + fragment, normalized.

    Period-*un*pinned by design: an article and every one of its version slices
    share this key. Use `citation_keys` for grading — this stays the plain
    article identity (rendering, dedup, and the unpinned gold form)."""
    return f"{p.source_id}::{(p.fragment or '').strip().lower()}"


def citation_keys(p: Provenance) -> set[str]:
    """Every gold-citation form this unit satisfies (period-aware grading).

    A statute article is stored one row per *(član × version-slice)*, and all
    slices share `citation_key` above — so an unpinned key cannot tell the 2015
    text from the 2022 one, which makes the whole provenance family blind to the
    axis this project treats as its novelty. Grading therefore uses subsumption:

    - an **unpinned** gold citation (``source::fragment``) matches *any* version
      of that article — the right default, since most articles were never
      amended and pinning them would be noise;
    - a **pinned** gold citation (``source::fragment@version``) matches exactly
      one slice, and is how a temporal item states which text it means.

    Keeping unpinned gold citations valid is what makes this a non-breaking
    change: previously-authored goldens keep their existing semantics and stay
    comparable across campaigns."""
    base = citation_key(p)
    return {base, f"{base}@{p.version}"} if p.version else {base}


def _in_force(p: Provenance, as_of: date) -> bool:
    after_start = p.valid_from is None or p.valid_from <= as_of
    before_end = p.valid_to is None or as_of < p.valid_to
    return after_start and before_end


def _citation_prf(
    predicted: list[set[str]], gold: list[str]
) -> tuple[float, float, float]:
    """Precision/recall/F1 under the subsumption rule of `citation_keys`.

    Each predicted unit contributes the *set* of gold forms it satisfies, so an
    unpinned gold citation is matched by any version of the article while a
    pinned one is matched only by its own slice."""
    gold_set = set(gold)
    if not predicted or not gold_set:
        return 0.0, 0.0, 0.0
    hits = sum(1 for keys in predicted if keys & gold_set)
    covered = sum(1 for g in gold_set if any(g in keys for keys in predicted))
    precision = hits / len(predicted)
    recall = covered / len(gold_set)
    f1 = (
        2 * precision * recall / (precision + recall) if precision and recall else 0.0
    )
    return precision, recall, f1


def grade(
    judge: LLMClient, question: str, gold: str, candidate: str,
    *, as_of: date | None = None,
) -> tuple[float, str, str, str]:
    """Reference-guided LLM grading of one answer. Returns
    (graded_score in {0,0.5,1}, verdict_label, rationale, judge_model).

    `as_of` makes the grader date-aware on dated items. Without it the judge
    compares substance against a dateless reference and cannot penalise an
    answer for describing the wrong period — with a temporally-naive arm a grader
    rationale read "though it oddly cites a 2018 date, the substance matches"
    and scored the item correct, so answer correctness and temporal correctness
    were measured by two disjoint mechanisms, one blind to time
   .

    Public so a second-family juror can be run for judge–judge agreement
    (design/benchmark.md §4) without re-implementing the prompt/parse loop."""
    dated = (
        f"\n\nThis question is asked AS OF {as_of.isoformat()}. The reference "
        "states the law in force on that date. Treat the period as a fact that "
        "must agree: if the candidate answers about a different date, or states "
        "a rule that the reference shows was not in force then, that is a "
        "factual contradiction — grade it incorrect even if the wording is "
        "otherwise close."
        if as_of is not None else ""
    )
    prompt = (
        f"Question:\n{question}{dated}\n\n"
        f"REFERENCE answer (ground truth):\n{gold}\n\n"
        f"CANDIDATE answer:\n{candidate or '(empty answer)'}"
    )
    parsed, acct = generate_json(
        judge, prompt, system=JUDGE_SYSTEM, json_schema=JUDGE_SCHEMA,
        max_output_tokens=16384,
    )
    # A parsed dict with no "verdict" is a degenerate generation (e.g. a forced
    # tool call truncated to `{}`) — treat as failure, never a silent 0.0.
    if parsed is None or "verdict" not in parsed:
        raise RuntimeError(
            f"judge output unparseable after {acct.attempts} attempts "
            f"(finish_reason={acct.finish_reason}; failures={len(acct.failures)})"
        )
    verdict = parsed["verdict"]
    score = VERDICT_SCORE.get(verdict, 0.0)
    return score, verdict, parsed.get("rationale", ""), acct.model


def _judge(judge: LLMClient, question: str, gold: str, candidate: str,
           *, as_of: date | None = None) -> tuple[float, str, str]:
    score, _verdict, rationale, model = grade(
        judge, question, gold, candidate, as_of=as_of
    )
    return score, rationale, model


def krippendorff_alpha(units: list[list[str | None]]) -> float | None:
    """Nominal Krippendorff's α over reliability data — the judge–judge (and,
    at the freeze, judge–human) agreement coefficient (Krippendorff, *Content
    Analysis*; design/benchmark.md §4). `units` is one entry per graded item, each
    a list of categorical labels (one per juror; `None` = that juror abstained).

    α = 1 − D_observed / D_expected over the coincidence matrix. Returns 1.0 on
    perfect agreement, 0.0 at chance, <0 worse than chance; `None` when α is
    undefined (fewer than two paired ratings, or no disagreement *possible*
    because every rating is the same single category)."""
    # Coincidence matrix: each unit with m≥2 ratings contributes every ordered
    # pair of its ratings, weighted 1/(m-1).
    coincidence: dict[tuple[str, str], float] = {}
    for ratings in units:
        present = [r for r in ratings if r is not None]
        m = len(present)
        if m < 2:
            continue  # single (or no) rating carries no pairing information
        w = 1.0 / (m - 1)
        for i, c in enumerate(present):
            for j, k in enumerate(present):
                if i != j:
                    coincidence[(c, k)] = coincidence.get((c, k), 0.0) + w

    if not coincidence:
        return None
    marginals: dict[str, float] = {}
    for (c, _k), v in coincidence.items():
        marginals[c] = marginals.get(c, 0.0) + v
    n = sum(marginals.values())
    if n <= 1 or len(marginals) < 2:
        return None  # all ratings identical → no disagreement is even possible

    observed = sum(v for (c, k), v in coincidence.items() if c != k)
    expected = sum(
        nc * nk for c, nc in marginals.items() for k, nk in marginals.items() if c != k
    ) / (n - 1)
    if expected == 0:
        return None
    return round(1.0 - observed / expected, 4)


def score_item(
    item: TaskItem,
    answer: Answer,
    backend: str,
    *,
    judge: LLMClient | None = None,
    latency_ms: float | None = None,
    cost_usd: float | None = None,
    retrieved_keys: list[str] | list[set[str]] | None = None,
) -> ItemScore:
    """Score one answer against one gold item. `judge` is required for items
    with a gold answer (abstain items are scored deterministically).

    `retrieved_keys` is the arm's *retrieved pool* in `citation_keys()` format
    (every unit fed to the generator, pre-citation). When supplied, retrieval
    recall/precision are computed against the gold citations so the retriever's
    reach can be told apart from the generator's citation discipline."""
    candidate_text = "(abstained)" if answer.abstained else answer.text
    abstain_target = item.gold_answer is None

    retrieval_recall = retrieval_precision = None
    if abstain_target:  # abstain target
        correctness = 1.0 if answer.abstained else 0.0
        rationale, judge_model = None, None
        precision = recall = None
        fidelity = correctness  # correctly abstaining is the only faithful move
    else:
        if judge is None:
            raise ValueError(f"item {item.item_id!r} has a gold answer — a judge is required")
        correctness, rationale, judge_model = _judge(
            judge, item.query.text, item.gold_answer, candidate_text,
            as_of=item.query.as_of,
        )
        predicted = [citation_keys(p) for p in answer.citations]
        precision, recall, fidelity = _citation_prf(predicted, item.gold_citations)
        if retrieved_keys is not None:
            # Same set comparison as citations, but against the *pool*: did the
            # gold unit ever reach the context window (vs being cited)? Bare
            # strings are accepted as the unpinned form so callers that key the
            # pool themselves keep working.
            pool = [{k} if isinstance(k, str) else k for k in retrieved_keys]
            retrieval_precision, retrieval_recall, _ = _citation_prf(
                pool, item.gold_citations
            )

    temporally_correct = None
    temporally_wrong_and_cited = None
    # Gated on *citations*, not on delivery: an answer that hedges ("I can't
    # determine this, but see član X") while pointing at a repealed article is
    # the invariant-2 hazard in its purest form. Scoping this to non-abstained
    # answers made those citations unscoreable — 5 of the 12 over-abstentions in
    # an earlier campaign carried citations and were invisible to the whole safety
    # family.
    if item.query.as_of is not None and answer.citations:
        if item.temporal_mode == "reports_repeal":
            # the item cites an out-of-force article to *report* its repeal;
            # correct iff the answer surfaced an out-of-force unit (don't penalize
            # the out-of-force citation as if it were claimed current authority).
            temporally_correct = any(
                not _in_force(p, item.query.as_of) for p in answer.citations
            )
        else:
            temporally_correct = all(_in_force(p, item.query.as_of) for p in answer.citations)
            # The publishable safety event: an in-force-mode answer that rests on
            # an out-of-force unit cited *as authority* — the civil-law analog of
            # RegLab's "overruled case = misgrounded" (design/benchmark.md §4).
            temporally_wrong_and_cited = not temporally_correct

    # Provenance-safety (RegLab "incorrect OR misgrounded").
    #
    # `misgrounded` is a *grounding* judgement and is kept independent of the
    # temporal axis: folding `temporally_wrong_and_cited` in here made the two
    # non-independent, so a defect in either contaminated both — in the v1.1
    # campaign the oracle's entire misgrounding score was the temporal flag and
    # nothing else. Report them as separate axes.
    misgrounded = hallucinated = None
    if not abstain_target and answer.citations:
        # misgrounded = *every* cited unit is outside the gold set (precision
        # == 0). Deterministic and conservative: partial-precision answers are
        # deliberately not flagged. Scored whenever the answer cites, including
        # a citing abstention (M-9) — pointing at the wrong authority is a
        # grounding failure whether or not the answer was delivered.
        misgrounded = precision == 0.0
    if not abstain_target and not answer.abstained:
        # incorrect (flatly, not partial) OR misgrounded — scoped to *delivered*
        # answers, since a refusal is not a false statement.
        hallucinated = (correctness == 0.0) or bool(misgrounded)

    return ItemScore(
        item_id=item.item_id,
        backend=backend,
        category=item.category,
        capability=item.capability,
        correct=correctness >= CORRECT_THRESHOLD,
        correctness=correctness,
        provenance_fidelity=fidelity,
        citation_precision=precision,
        citation_recall=recall,
        temporally_correct=temporally_correct,
        abstained=answer.abstained,
        abstain_target=abstain_target,
        retrieval_recall=retrieval_recall,
        retrieval_precision=retrieval_precision,
        temporally_wrong_and_cited=temporally_wrong_and_cited,
        misgrounded=misgrounded,
        hallucinated=hallucinated,
        latency_ms=latency_ms,
        cost_usd=cost_usd,
        judge_rationale=rationale,
        judge_model=judge_model,
    )


def aggregate(scores: list[ItemScore]) -> dict:
    """Per-backend summary over a list of item scores."""
    by_backend: dict[str, list[ItemScore]] = {}
    for s in scores:
        by_backend.setdefault(s.backend, []).append(s)

    def _mean(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 4) if values else None

    def _rate(flags: list) -> float | None:
        """Rate over the items where the flag is *defined* (None = skipped)."""
        flags = [f for f in flags if f is not None]
        return _mean([float(f) for f in flags])

    def _fixed_rate(flags: list) -> float | None:
        """Rate over a fixed denominator: `None` counts as "did not occur", not
        as "excluded". Keeps arms with different abstention rates comparable."""
        return _mean([float(bool(f)) for f in flags]) if flags else None

    out: dict[str, dict] = {}
    for backend, group in sorted(by_backend.items()):
        temporal = [s.temporally_correct for s in group if s.temporally_correct is not None]
        # Abstain targets are keyed on the gold (no answer), not on the difficulty
        # tier — `category` carries no "abstain" value, so the old category=="abstain"
        # read silently matched nothing on tier-tagged goldens.
        answerable = [s for s in group if not s.abstain_target]
        unanswerable = [s for s in group if s.abstain_target]
        ret_recall = _mean([s.retrieval_recall for s in group if s.retrieval_recall is not None])
        cit_recall = _mean([s.citation_recall for s in group if s.citation_recall is not None])
        out[backend] = {
            "items": len(group),
            "accuracy": _mean([float(s.correct) for s in group]),
            "correctness": _mean([s.correctness for s in group]),
            "provenance_fidelity": _mean([s.provenance_fidelity for s in group]),
            "temporal_accuracy": _mean([float(t) for t in temporal]),
            "abstain_accuracy": _mean([float(s.correct) for s in unanswerable]),
            # Retriever vs citation: the gap = surfaced-but-not-cited (LegalBench-RAG).
            "retrieval_recall": ret_recall,
            "citation_recall": cit_recall,
            "retrieval_citation_gap": (
                round(ret_recall - cit_recall, 4)
                if ret_recall is not None and cit_recall is not None
                else None
            ),
            # Provenance-safety rates (RegLab), over a **fixed denominator**:
            # every answerable item, not just the ones the arm chose to deliver.
            # A delivered-answer base let an arm improve its safety numbers by
            # refusing more — with a temporally-naive arm the hallucination denominator
            # ranged 51…59 of 60 across arms, so the rates were not comparable
            #. Refusal is penalised in exactly one
            # place, `over_abstention_rate` below, and never priced twice.
            "temporally_wrong_and_cited_rate": _fixed_rate(
                [s.temporally_wrong_and_cited for s in answerable]
            ),
            "misgrounded_rate": _fixed_rate([s.misgrounded for s in answerable]),
            "hallucination_rate": _fixed_rate([s.hallucinated for s in answerable]),
            # The delivered-answer base kept alongside, so the two are legible
            # together and neither has to be recomputed from raw scores.
            "hallucination_rate_delivered": _rate([s.hallucinated for s in answerable]),
            "delivered": sum(1 for s in answerable if not s.abstained),
            "answerable": len(answerable),
            # Abstention quality, split (RegLab: models over-trust + under-refuse).
            "over_abstention_rate": _rate([s.abstained for s in answerable]),
            "false_answer_rate_unanswerable": _rate([not s.abstained for s in unanswerable]),
            "mean_latency_ms": _mean([s.latency_ms for s in group if s.latency_ms is not None]),
            "mean_cost_usd": _mean([s.cost_usd for s in group if s.cost_usd is not None]),
        }
    return out
