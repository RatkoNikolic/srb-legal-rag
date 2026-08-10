"""Benchmark eval runner: golden set × arms → ItemScores + aggregates.

Runs every golden item through the selected arms, scores each answer with
kl_core.evals (reference-guided LLM judge + ALCE-style citation P/R/F1 +
deterministic temporal/abstain checks), persists each run in the telemetry
store (tagged with the item id, so the dashboards and the harness read the
same rows), and writes a report under data/evals/.

Judge (benchmark design doc §4): the dev tier judges with a **cross-family**
model — Claude Opus 5 (`AnthropicLLM`), a different family from the Gemini
generator, so the single biggest LLM-judge objection (Gemini grading Gemini
self-preference) does not apply. Falls back to the same-family Gemini judge only
when `ANTHROPIC_API_KEY` is absent. `--dual-judge` additionally grades every
gold item with a second-family juror (Gemini) and reports Krippendorff's α —
high cross-family agreement means the ranking is robust to judge choice.

Usage:
  .venv/bin/python -m lexsr.evals --arms naive,advanced,graph,agentic
  .venv/bin/python -m lexsr.evals --arms naive --judge opus --dual-judge
  .venv/bin/python -m lexsr.evals --arms context-stuffing --limit 3
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from kl_core.evals import (
    ItemScore,
    TaskItem,
    aggregate,
    citation_keys,
    grade,
    krippendorff_alpha,
    score_item,
)
from kl_core.llm import LLMClient
from kl_core.providers.anthropic import AnthropicLLM
from kl_core.providers.gemini import GeminiEmbedder, GeminiLLM
from lexsr import runstore
from lexsr.arms import ARMS
from lexsr.arms.base import ArmResult, _provenance
from lexsr.config import (
    CORPUS_DIR,
    CURRENT_GOLDEN,
    EVALS_DIR,
    anthropic_api_key,
    gemini_api_key,
)
from lexsr.index import open_table
from lexsr.kg import open_connection

# Map a graded score back to its verdict label — the inverse of VERDICT_SCORE —
# so judge–judge agreement (a nominal coefficient) can reuse the primary judge's
# already-computed score without re-running it.
_SCORE_LABEL = {1.0: "correct", 0.5: "partially_correct", 0.0: "incorrect"}


def make_judge(kind: str) -> LLMClient:
    """Resolve a judge name to a client. `opus` = cross-family Claude Opus 5
    (the default); `gemini` = same-family fallback / second juror."""
    if kind == "opus":
        return AnthropicLLM(anthropic_api_key())
    if kind == "gemini":
        return GeminiLLM(gemini_api_key())
    raise ValueError(f"unknown judge {kind!r}; have 'opus' | 'gemini'")


def _default_judge_kind() -> str:
    """Prefer the cross-family Opus judge; degrade to Gemini only if no key."""
    import os

    return "opus" if os.environ.get("ANTHROPIC_API_KEY") else "gemini"

# Human-facing arm names for reports (slugs stay machine-stable in the API/DB).
ARM_LABELS = {
    "naive": "Naive RAG",
    "advanced": "Advanced RAG",
    "graph": "Advanced + Graph",
    "context-stuffing": "Context-Stuffing",
    "agentic": "Agentic",
}

# Context-Stuffing is run as an ORACLE — the target law is taken from the gold
# annotation (perfect law selection a priori), then the whole law is stuffed in.
# That makes it a diagnostic *ceiling* (the retrieval-vs-reasoning upper bound,
# in the lineage of HotpotQA's gold-supporting-fact "upper bound" — Yang et al.,
# EMNLP 2018 — and KILT's shared-provenance evaluation — Petroni et al., NAACL
# 2021), NOT a deployable system. So it is reported but kept OUT of the
# head-to-head ranking of deployable arms; **Naive RAG is the primary deployable
# baseline** (the floor every engineered arm must beat). See design/benchmark.md.
CEILING_ARMS = frozenset({"context-stuffing"})


def _oracle_slug_map() -> dict[str, str]:
    """`source_id → corpus slug`, read from the manifest — the map that turns a
    gold citation (keyed by ELI source_id) into the `law_slug` the stuffing arm
    filters on. Empty on a fresh checkout with no corpus (tests tolerate it)."""
    try:
        manifest = json.loads((CORPUS_DIR / "manifest.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return {m["source_id"]: m["slug"] for m in manifest if m.get("source_id") and m.get("slug")}


def oracle_law_slugs(item: TaskItem, sid_to_slug: dict[str, str]) -> list[str]:
    """ALL gold-act targets for the context-stuffing ORACLE, deduplicated in
    gold-citation order. An explicit `filters.law_slug` (legacy sets) leads the
    list. Non-izreka acts come first (the governing statutes/bylaws), CC-decision
    acts cited only via their izreka last — the ceiling stuffs *every* act the
    gold rests on, matching its gold-supporting-fact lineage (HotpotQA/KILT
    include all gold facts, not the first). Empty for abstain items (no gold
    citations) or acts absent from the corpus — the arm then falls back to
    vector-top1, recorded as such in extras."""
    slugs: list[str] = []
    explicit = item.query.filters.get("law_slug")
    if explicit:
        slugs.append(explicit)
    resolvable = [
        (cite, slug) for cite in item.gold_citations
        if (slug := sid_to_slug.get(cite.split("::", 1)[0]))
    ]
    for cite, slug in resolvable:
        if not cite.endswith("::izreka") and slug not in slugs:
            slugs.append(slug)
    for _cite, slug in resolvable:  # izreka-only acts (CC decisions) last
        if slug not in slugs:
            slugs.append(slug)
    return slugs


def oracle_law_slug(item: TaskItem, sid_to_slug: dict[str, str]) -> str | None:
    """The PRIMARY gold-act target (first of `oracle_law_slugs`) — kept for the
    single-act call sites and as the arm's headline `law_slug` extra."""
    slugs = oracle_law_slugs(item, sid_to_slug)
    return slugs[0] if slugs else None


def _arm_label(slug: str) -> str:
    return ARM_LABELS.get(slug, slug)


def _rank_arms(arms) -> list:
    """Deployable arms first (alphabetical), oracle ceilings last — the ceiling
    is a reference condition, not a ranked competitor (design/benchmark.md)."""
    return sorted(arms, key=lambda a: (a in CEILING_ARMS, a))


def load_golden(path: Path = CURRENT_GOLDEN) -> list[TaskItem]:
    return [TaskItem(**json.loads(line)) for line in path.open(encoding="utf-8")]


def retrieved_keys(result: ArmResult) -> list[set[str]]:
    """The arm's retrieved pool (everything fed to the generator) keyed exactly
    as cited/gold citations are — so retrieval recall is comparable to citation
    recall. Reuses `_provenance` so the keying is identical to predicted
    citations (which the generator builds the same way), including the
    version-pinned form (`citation_keys`) that makes the match period-aware."""
    return [citation_keys(_provenance(r)) for r in result.retrieved]


def run_eval(
    arm_names: list[str],
    *,
    golden_path: Path = CURRENT_GOLDEN,
    limit: int | None = None,
    batch_id: str | None = None,
    categories: list[str] | None = None,
    item_ids: list[str] | None = None,
    judge: str | None = None,
    dual_judge: bool = False,
    config=None,
    progress=None,
) -> dict:
    items = load_golden(golden_path)
    if categories:
        items = [it for it in items if it.category in categories]
    # Targeted re-run of named items. A generation failure is quarantined as a
    # batch error rather than retried forever, which leaves that (arm, item)
    # cell empty — this repairs the cell in place, under the same batch_id,
    # instead of re-running (and re-paying for) all 300 answers.
    if item_ids:
        want = set(item_ids)
        items = [it for it in items if it.item_id in want]
        missing = want - {it.item_id for it in items}
        if missing:
            raise SystemExit(f"no such item(s) in {golden_path.name}: {sorted(missing)}")
    items = items[:limit]
    golden_name = golden_path.stem  # tags every run so metrics filter back to the set
    total = len(arm_names) * len(items)
    done = 0
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    batch_id = batch_id or f"bench-{stamp}"
    key = gemini_api_key()
    llm, embedder = GeminiLLM(key), GeminiEmbedder(key)
    judge_kind = judge or _default_judge_kind()
    judge_llm = make_judge(judge_kind)
    # The second-family juror (Gemini) for judge–judge agreement. When the primary
    # is already Gemini, a dual run would be same-family — skip it.
    secondary_judge = (
        make_judge("gemini") if (dual_judge and judge_kind != "gemini") else None
    )
    # Per-item judge/juror labels for Krippendorff's α — gold items only.
    # The JOIN to (item, arm) is persisted, not just the label pair: without it
    # α is a scalar nobody can interrogate, so a below-threshold coefficient
    # cannot be attributed to a tier, an arm, or the partial-credit boundary.
    agreement_rows: list[dict] = []
    table, kg = open_table(), open_connection()
    gold_by_id = {it.item_id: it for it in items}
    sid_to_slug = _oracle_slug_map()  # for resolving the stuffing oracle's gold law

    scores: list[ItemScore] = []
    rows: list[dict] = []  # flat (item × arm) records for the raw report
    errors: list[dict] = []
    for arm_name in arm_names:
        arm = ARMS[arm_name](llm, embedder, table, kg, config=config)
        for item in items:
            hints = {}
            if arm_name == "context-stuffing":
                slugs = oracle_law_slugs(item, sid_to_slug)
                if slugs:
                    hints["law_slug"] = slugs  # oracle targets (ALL gold acts)
            try:
                result = arm.answer(item.query.text, as_of=item.query.as_of, **hints)
                result.extras["eval_item_id"] = item.item_id
                run_id = runstore.save_run(result, batch_id=batch_id, golden=golden_name)
                if result.extras.get("generation_failed"):
                    # A generation that stayed unparseable through the retries is
                    # an infrastructure error, not an abstention — route to the
                    # errors list (the run itself IS saved: telemetry survives).
                    errors.append({
                        "arm": arm_name, "item_id": item.item_id, "run_id": run_id,
                        "error": result.answer.metadata.get("abstain_reason")
                        or "generation failed",
                    })
                    print(f"[eval] {arm_name:9s} {item.item_id} "
                          f"! generation failed (run saved, not scored)", flush=True)
                    done += 1
                    if progress:
                        progress(done, total, arm_name, item.item_id)
                    continue
                score = score_item(
                    item,
                    result.answer,
                    arm_name,
                    judge=judge_llm,
                    latency_ms=result.timings_ms.get("total_ms"),
                    cost_usd=result.cost_usd,
                    retrieved_keys=retrieved_keys(result),
                )
                runstore.set_eval(run_id, score.model_dump())  # graded score → telemetry
                # Judge–judge agreement: grade the same answer with the
                # second-family juror and pair the two verdict labels. Gold items
                # only — abstain items are scored deterministically (no judge).
                if secondary_judge is not None and item.gold_answer is not None:
                    # A juror failure must not discard the already-graded item:
                    # the primary score is saved and reported either way, the
                    # item just contributes no agreement pair.
                    try:
                        candidate = (
                            "(abstained)" if result.answer.abstained else result.answer.text
                        )
                        # Same date-awareness as the primary judge, or the two
                        # graders answer different questions on dated items and
                        # α measures that difference instead of agreement.
                        _s, secondary_verdict, _r, _m = grade(
                            secondary_judge, item.query.text, item.gold_answer,
                            candidate, as_of=item.query.as_of,
                        )
                        agreement_rows.append({
                            "item_id": item.item_id, "arm": arm_name,
                            "category": item.category, "capability": item.capability,
                            "dated": item.query.as_of is not None,
                            "primary": _SCORE_LABEL.get(score.correctness),
                            "secondary": secondary_verdict,
                        })
                    except Exception as exc:
                        errors.append({"arm": arm_name, "item_id": item.item_id,
                                       "error": f"secondary judge: {exc}"})
                        print(f"[eval] {arm_name:9s} {item.item_id} "
                              f"! secondary judge error (primary score kept): {exc}",
                              flush=True)
            except Exception as exc:  # one bad item must not kill the matrix
                errors.append({"arm": arm_name, "item_id": item.item_id, "error": str(exc)})
                print(f"[eval] {arm_name:9s} {item.item_id} ! error: {exc}", flush=True)
                done += 1
                if progress:
                    progress(done, total, arm_name, item.item_id)
                continue
            scores.append(score)
            rows.append({**score.model_dump(), "run_id": run_id,
                         "answer": result.answer.text, "abstained": result.answer.abstained})
            mark = "✓" if score.correct else ("~" if score.correctness == 0.5 else "✗")
            print(
                f"[eval] {arm_name:9s} {item.item_id} {mark} "
                f"corr={score.correctness:.1f} fid={score.provenance_fidelity:.2f} "
                f"${score.cost_usd or 0:.4f} run={run_id}",
                flush=True,
            )
            done += 1
            if progress:
                progress(done, total, arm_name, item.item_id)

    summary = aggregate(scores)
    by_category = _aggregate_by_category(scores)
    by_capability = _aggregate_by(scores, "capability")
    # Judge–judge agreement: Krippendorff's α between the primary (cross-family)
    # judge and the second-family juror, over every gold-item grading.
    units = [[r["primary"], r["secondary"]] for r in agreement_rows]
    # Binarised on the decision the ranking actually rests on (`correct` at
    # threshold 1.0): nominal α over three ordered levels penalises a
    # correct-vs-partial disagreement as heavily as correct-vs-incorrect, which
    # understates agreement on the call that decides accuracy.
    binary = [[None if v is None else ("correct" if v == "correct" else "not_correct")
               for v in u] for u in units]
    judge_agreement = {
        "primary_judge": judge_kind,
        "secondary_judge": "gemini" if secondary_judge is not None else None,
        "n_items": len(agreement_rows),
        "krippendorff_alpha": krippendorff_alpha(units) if units else None,
        "krippendorff_alpha_binarised": krippendorff_alpha(binary) if binary else None,
        "per_item": agreement_rows,
    }
    EVALS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = EVALS_DIR / f"eval_{stamp}.json"
    report = {
        "batch_id": batch_id,
        "golden": golden_path.name,
        "items": len(items),
        "arms": arm_names,
        "ablation": config.label() if config is not None else "default",
        "judge": judge_kind,
        "scores": [s.model_dump() for s in scores],
        "errors": errors,
        "aggregates": summary,
        "by_category": by_category,
        "by_capability": by_capability,
        "judge_agreement": judge_agreement,
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    summary_md = _write_summary_md(
        stamp, batch_id, golden_path, scores, by_category, by_capability, errors
    )
    raw_md = _write_raw_md(stamp, batch_id, rows, gold_by_id)

    print(f"\n[eval] report → {report_path}")
    print(f"[eval] summary → {summary_md}")
    print(f"[eval] raw     → {raw_md}")
    print(f"[eval] batch_id = {batch_id}")
    print(f"[eval] judge = {judge_kind}")
    if judge_agreement["secondary_judge"]:
        alpha = judge_agreement["krippendorff_alpha"]
        print(
            f"[eval] judge agreement (α, {judge_kind} vs gemini, "
            f"n={judge_agreement['n_items']}) = "
            f"{'-' if alpha is None else f'{alpha:.3f}'}"
        )
    header = (
        f"{'arm':9s} {'acc':>5s} {'corr':>5s} {'prov':>5s} {'rRec':>5s} "
        f"{'temp':>5s} {'misg':>5s} {'hall':>5s} {'abst':>5s} {'ms':>7s} {'$/q':>8s}"
    )
    print(header)
    for arm_name, agg in summary.items():
        def fmt(v, nd=2):
            return "-" if v is None else f"{v:.{nd}f}"
        print(
            f"{arm_name:9s} {fmt(agg['accuracy']):>5s} {fmt(agg['correctness']):>5s} "
            f"{fmt(agg['provenance_fidelity']):>5s} {fmt(agg['retrieval_recall']):>5s} "
            f"{fmt(agg['temporal_accuracy']):>5s} {fmt(agg['misgrounded_rate']):>5s} "
            f"{fmt(agg['hallucination_rate']):>5s} {fmt(agg['abstain_accuracy']):>5s} "
            f"{fmt(agg['mean_latency_ms'], 0):>7s} {fmt(agg['mean_cost_usd'], 4):>8s}"
        )
    return report


def _mean(xs: list) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


def _fixed_rate(flags: list) -> float | None:
    """Rate over a fixed denominator: `None` counts as "did not occur", not as
    "excluded" — see `kl_core.evals.aggregate` for why ."""
    return _mean([float(bool(f)) for f in flags]) if flags else None


def _rate(flags: list) -> float | None:
    """Fraction of True over non-None boolean flags (None = not applicable)."""
    return _mean([float(f) for f in flags if f is not None])


def _per_arm_metrics(scores: list[ItemScore]) -> dict:
    by: dict[str, list[ItemScore]] = {}
    for s in scores:
        by.setdefault(s.backend, []).append(s)
    out = {}
    for arm, g in sorted(by.items()):
        answerable = [s for s in g if not s.abstain_target]
        recall = _mean([s.citation_recall for s in g if s.citation_recall is not None])
        ret_recall = _mean([s.retrieval_recall for s in g if s.retrieval_recall is not None])
        out[arm] = {
            "items": len(g),
            "accuracy": _mean([float(s.correct) for s in g]),
            "correctness": _mean([s.correctness for s in g]),
            "precision": _mean([s.citation_precision for s in g]),
            "recall": recall,
            "f1": _mean([s.provenance_fidelity for s in g]),
            "temporal": _mean([float(s.temporally_correct) for s in g
                               if s.temporally_correct is not None]),
            "abstain_rate": round(sum(s.abstained for s in g) / len(g), 3),
            # Retriever-side reach + the surfaced-but-not-cited gap (LegalBench-RAG).
            "retrieval_recall": ret_recall,
            "ret_cit_gap": (round(ret_recall - recall, 4)
                            if ret_recall is not None and recall is not None else None),
            # Provenance-safety rates (RegLab "incorrect OR misgrounded"), over a
            # FIXED denominator — every answerable item, not just delivered ones,
            # so arms with different abstention rates stay comparable. Refusal is
            # priced once, in `over_abstention_rate`.
            "misgrounded_rate": _fixed_rate([s.misgrounded for s in answerable]),
            "temp_wrong_cited_rate": _fixed_rate(
                [s.temporally_wrong_and_cited for s in answerable]
            ),
            "hallucination_rate": _fixed_rate([s.hallucinated for s in answerable]),
            "hallucination_rate_delivered": _rate([s.hallucinated for s in answerable]),
            "delivered": sum(1 for s in answerable if not s.abstained),
            "answerable": len(answerable),
            "over_abstention_rate": _rate([s.abstained for s in g if not s.abstain_target]),
            "false_answer_unanswerable_rate": _rate([not s.abstained for s in g
                                                     if s.abstain_target]),
            "latency_ms": _mean([s.latency_ms for s in g]),
            "cost_usd": _mean([s.cost_usd for s in g]),
        }
    return out


def _aggregate_by(scores: list[ItemScore], attr: str) -> dict:
    """Per-(group × arm) accuracy/correctness/F1, where `attr` is 'category'
    (difficulty tier) or 'capability' (lookup/cross_reference/…)."""
    by: dict[str, dict[str, list[ItemScore]]] = {}
    for s in scores:
        key = getattr(s, attr, "") or "(unset)"
        by.setdefault(key, {}).setdefault(s.backend, []).append(s)
    out = {}
    for grp, byb in sorted(by.items()):
        out[grp] = {
            arm: {
                "items": len(g),
                "accuracy": _mean([float(s.correct) for s in g]),
                "correctness": _mean([s.correctness for s in g]),
                "f1": _mean([s.provenance_fidelity for s in g]),
                # Per-typology safety decomposition — where structure should earn
                # its keep, and where flat arms produce confidently-wrong-and-cited
                # answers (design/benchmark.md §4/§5; RegLab hallucination-by-type).
                "retrieval_recall": _mean([s.retrieval_recall for s in g
                                           if s.retrieval_recall is not None]),
                "misgrounded_rate": _rate([s.misgrounded for s in g]),
                "temp_wrong_cited_rate": _rate([s.temporally_wrong_and_cited for s in g]),
            }
            for arm, g in sorted(byb.items())
        }
    return out


def _aggregate_by_category(scores: list[ItemScore]) -> dict:
    return _aggregate_by(scores, "category")


def _fmt(v, nd=2) -> str:
    return "—" if v is None else f"{v:.{nd}f}"


def _graph_stats_lines() -> list[str]:
    """The construction stage of the negative-result decomposition (
    design/benchmark.md §6): graph-structural numbers *independent of any answer*,
    in the GraphRAG-Bench idiom (Node/Edge Count, Average Degree, "Organization" =
    non-isolated fraction — Xiao et al., arXiv 2506.05690). Read straight from
    `graph_stats.json`; surfaced here so the retrieval-stage recall (above) and the
    construction-stage sparsity are read side by side — "structure couldn't help
    because the edges intrinsically don't exist", as a number not a claim."""
    try:
        s = json.loads((CORPUS_DIR / "graph_stats.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return []
    def g(k):
        return s.get(k, "—")
    return [
        "",
        "## Graph construction (structural stats — the negative result's construction stage)",
        "",
        "Independent of any answer — the GraphRAG-Bench construction metrics (Node/Edge Count, "
        "Average Degree, Organization = non-isolated %). Low mean out-degree + high isolated % "
        "is the corpus-intrinsic cross-reference sparsity: the cross-law edges a graph "
        "would traverse largely don't exist, so structure can't earn its keep on cross-reference "
        "(read together with the retrieval-recall split above — design/benchmark.md §6).",
        "",
        "| nodes | semantic edges | mean out-degree | isolated % | cross-law edges | cross-law % | "
        "arm-reachable % |",
        "|--:|--:|--:|--:|--:|--:|--:|",
        f"| {g('nodes')} | {g('semantic_edges')} | {g('mean_out_degree')} | {g('isolated_pct')} | "
        f"{g('cross_law_edges')} | {g('cross_law_pct')} | {g('arm_reachable_pct')} |",
    ]


def _write_summary_md(stamp, batch_id, golden_path, scores, by_category, by_capability, errors) -> Path:
    m = _per_arm_metrics(scores)
    ranked = _rank_arms(m)
    L = [
        f"# Benchmark summary — `{batch_id}`",
        "",
        f"- Golden set: `{golden_path.name}`",
        f"- Run (UTC): {stamp}",
        f"- Scored answers: {len(scores)}  ·  errors: {len(errors)}",
        "",
        "Metrics: **accuracy** = graded-correct rate (LLM judge, reference-guided); "
        "**correctness** = graded credit incl. partial; **precision/recall/F1** = citation "
        "fidelity vs gold articles (ALCE-style); **temporal** = cited units in force on `as_of`.",
        "",
        "## Per-arm (all tiers)",
        "",
        "**Naive RAG is the primary deployable baseline.** Context-Stuffing is run as "
        "an oracle (given the answer law a priori) — an upper-bound *reference* "
        "condition, so it is listed last and **excluded from the deployable "
        "head-to-head ranking**; it measures the retrieval-vs-reasoning gap, not a "
        "deployable system (design/benchmark.md).",
        "",
        "| arm | n | accuracy | correctness | cit.precision | cit.recall | ret.recall | "
        "ret−cit gap | prov.F1 | temporal | abstain | avg ms | avg $/q |",
        "|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for arm in ranked:
        d = m[arm]
        L.append(
            f"| {_arm_label(arm)} | {d['items']} | {_fmt(d['accuracy'])} | {_fmt(d['correctness'])} | "
            f"{_fmt(d['precision'])} | {_fmt(d['recall'])} | {_fmt(d['retrieval_recall'])} | "
            f"{_fmt(d['ret_cit_gap'])} | {_fmt(d['f1'])} | "
            f"{_fmt(d['temporal'])} | {_fmt(d['abstain_rate'])} | "
            f"{_fmt(d['latency_ms'], 0)} | {_fmt(d['cost_usd'], 4)} |"
        )
    L += [
        "",
        "## Provenance & safety (per arm)",
        "",
        "**ret.recall − cit.recall** = gold units the retriever surfaced but the generator "
        "didn't cite (retrieval vs citation failure, LegalBench-RAG). **misgrounded** = "
        "delivered answer that cites inapplicable/out-of-force sources; **temp-wrong-cited** = "
        "cited a unit out of force on `as_of` *as authority* (the civil-law analog of RegLab's "
        "overruled-case misgrounding — invariant 2); **halluc.** = incorrect OR misgrounded "
        "delivered answer (RegLab, JELS 2025). **over-abstain** = refused an answerable item; "
        "**false-answer** = answered an unanswerable (abstain-target) item. Lower is better "
        "for every column here.",
        "",
        "| arm | misgrounded | temp-wrong-cited | halluc. | over-abstain | false-answer (unans.) |",
        "|---|--:|--:|--:|--:|--:|",
    ]
    for arm in ranked:
        d = m[arm]
        L.append(
            f"| {_arm_label(arm)} | {_fmt(d['misgrounded_rate'])} | "
            f"{_fmt(d['temp_wrong_cited_rate'])} | {_fmt(d['hallucination_rate'])} | "
            f"{_fmt(d['over_abstention_rate'])} | {_fmt(d['false_answer_unanswerable_rate'])} |"
        )
    L += ["", "## By tier × arm — accuracy (prov.F1)", ""]
    arms = ranked
    L.append("| tier | " + " | ".join(_arm_label(a) for a in arms) + " |")
    L.append("|---|" + "|".join(["--:"] * len(arms)) + "|")
    for cat, byb in by_category.items():
        cells = []
        for arm in arms:
            d = byb.get(arm)
            cells.append(f"{_fmt(d['accuracy'])} ({_fmt(d['f1'])})" if d else "—")
        L.append(f"| {cat} | " + " | ".join(cells) + " |")
    # The decomposition that makes the GraphRAG result legible: per capability
    # (lookup / cross_reference / delegated_norm / temporal / cc_validity / …),
    # accuracy + the temporally-wrong-and-cited rate side by side — structure
    # should earn its keep exactly where flat arms cite the wrong/stale unit.
    L += ["", "## By capability (typology) × arm — accuracy · temp-wrong-cited", ""]
    L.append("| capability | " + " | ".join(_arm_label(a) for a in arms) + " |")
    L.append("|---|" + "|".join(["--:"] * len(arms)) + "|")
    for cap, byb in by_capability.items():
        cells = []
        for arm in arms:
            d = byb.get(arm)
            cells.append(f"{_fmt(d['accuracy'])} · {_fmt(d['temp_wrong_cited_rate'])}" if d else "—")
        L.append(f"| {cap} | " + " | ".join(cells) + " |")
    # The construction stage of the decomposition — only when a structural arm ran.
    if any(s.backend in ("graph", "agentic") for s in scores):
        L += _graph_stats_lines()
    if errors:
        L += ["", "## Errors", ""]
        for e in errors:
            L.append(f"- `{e['arm']}` / `{e['item_id']}`: {e['error']}")
    path = EVALS_DIR / f"summary_{stamp}.md"
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
    return path


def _write_raw_md(stamp, batch_id, rows, gold_by_id) -> Path:
    L = [f"# Raw results — `{batch_id}`", "", "## Questions", "",
         "| item | tier | question | gold citations |", "|---|---|---|---|"]
    for iid, it in gold_by_id.items():
        cites = "; ".join(it.gold_citations)
        q = it.query.text.replace("|", "\\|")
        L.append(f"| {iid} | {it.category} | {q} | {cites} |")
    L += ["", "## Per-answer scores", "",
          "| item | tier | arm | correct | corr | prec | rec | F1 | $ | ms | judge rationale |",
          "|---|---|---|:-:|--:|--:|--:|--:|--:|--:|---|"]
    for r in sorted(rows, key=lambda r: (r["item_id"], r["backend"])):
        rationale = (r.get("judge_rationale") or "").replace("|", "\\|")[:140]
        mark = "✓" if r["correct"] else ("~" if r["correctness"] == 0.5 else "✗")
        L.append(
            f"| {r['item_id']} | {r['category']} | {_arm_label(r['backend'])} | {mark} | "
            f"{_fmt(r['correctness'], 1)} | {_fmt(r.get('citation_precision'))} | "
            f"{_fmt(r.get('citation_recall'))} | {_fmt(r['provenance_fidelity'])} | "
            f"{_fmt(r.get('cost_usd'), 4)} | {_fmt(r.get('latency_ms'), 0)} | {rationale} |"
        )
    path = EVALS_DIR / f"raw_{stamp}.md"
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arms", default="naive,advanced,graph,context-stuffing,agentic")
    parser.add_argument("--golden", type=Path, default=CURRENT_GOLDEN)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--batch-id", default=None, help="label for this run's batch")
    parser.add_argument("--items", default=None,
                        help="comma-separated item ids to run (repairs a quarantined "
                             "cell in place instead of re-running the whole batch)")
    parser.add_argument("--judge", choices=["opus", "gemini"], default=None,
                        help="grader model (default: opus if ANTHROPIC_API_KEY set, else gemini)")
    parser.add_argument("--dual-judge", action="store_true",
                        help="also grade with the second-family juror + report Krippendorff's α")
    parser.add_argument("--ablate", default=None,
                        help="comma-separated AblationConfig toggles to turn OFF "
                             "(e.g. rerank,temporal_filter,related_law_hop) — for the ablation study")
    args = parser.parse_args()
    arm_names = [a.strip() for a in args.arms.split(",") if a.strip()]
    unknown = [a for a in arm_names if a not in ARMS]
    if unknown:
        print(f"unknown arms {unknown}; have {sorted(ARMS)}", file=sys.stderr)
        return 2
    config = None
    if args.ablate:
        from lexsr.arms.base import AblationConfig
        off = [f.strip() for f in args.ablate.split(",") if f.strip()]
        valid = set(AblationConfig.model_fields)
        bad = [f for f in off if f not in valid]
        if bad:
            print(f"unknown ablation toggles {bad}; have {sorted(valid)}", file=sys.stderr)
            return 2
        config = AblationConfig(**{f: False for f in off})
    item_ids = [i.strip() for i in args.items.split(",") if i.strip()] if args.items else None
    run_eval(arm_names, golden_path=args.golden, limit=args.limit, batch_id=args.batch_id,
             item_ids=item_ids, judge=args.judge, dual_judge=args.dual_judge, config=config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
