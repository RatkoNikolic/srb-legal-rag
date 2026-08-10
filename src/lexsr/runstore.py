"""Run telemetry: every /api/ask is recorded in full (SQLite).

A run stores everything the drill-down needs to explain *why* the answer
looked the way it did: the retrieved chunks with scores and stages, the
citations, per-stage timings, token counts, and cost. The aggregate view is
computed from the same rows — one source of truth.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from lexsr.arms.base import ArmResult
from lexsr.config import RUNS_DB

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    question TEXT NOT NULL,
    arm TEXT NOT NULL,
    as_of TEXT,
    answer_text TEXT,
    abstained INTEGER NOT NULL DEFAULT 0,
    abstain_reason TEXT,
    citations_json TEXT NOT NULL DEFAULT '[]',
    retrieved_json TEXT NOT NULL DEFAULT '[]',
    timings_json TEXT NOT NULL DEFAULT '{}',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    model TEXT,
    extras_json TEXT NOT NULL DEFAULT '{}',
    conversation_id TEXT,
    batch_id TEXT,
    golden TEXT,
    ablation TEXT,
    eval_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_ts ON runs(ts DESC);
CREATE INDEX IF NOT EXISTS idx_runs_arm ON runs(arm);
CREATE INDEX IF NOT EXISTS idx_runs_conv ON runs(conversation_id);
CREATE INDEX IF NOT EXISTS idx_runs_batch ON runs(batch_id);
"""

# Manual /api/ask runs carry no batch; they group under this synthetic label.
INTERACTIVE_BATCH = "interactive"


# Git-LFS writes a small text stand-in when the content was never fetched. It
# is a perfectly openable file, so `sqlite3.connect` succeeds and the failure
# only surfaces later as a bare "file is not a database" — on a fresh clone
# without git-lfs that is the difference between an actionable message and a
# mystery 500 on the results pages.
_LFS_POINTER_MAGIC = b"version https://git-lfs.github.com/spec/"


def _assert_not_lfs_pointer(path) -> None:
    try:
        head = path.open("rb").read(len(_LFS_POINTER_MAGIC))
    except OSError:
        return  # absent is fine: a fresh working repo creates the DB on first write
    if head == _LFS_POINTER_MAGIC:
        raise RuntimeError(
            f"{path} is an unfetched Git-LFS pointer, not the database. "
            "Run `git lfs install && git lfs pull` to fetch it "
            "(git-lfs must be installed before cloning, or run `git lfs pull` after)."
        )


def _connect() -> sqlite3.Connection:
    RUNS_DB.parent.mkdir(parents=True, exist_ok=True)
    _assert_not_lfs_pointer(RUNS_DB)
    conn = sqlite3.connect(RUNS_DB)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    # Forward-migrate older DBs: `golden` (the benchmark set a batch ran against)
    # was added after the first runs were recorded. CREATE TABLE IF NOT EXISTS
    # never alters an existing table, so add the column on the fly when absent.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
    if "golden" not in cols:
        conn.execute("ALTER TABLE runs ADD COLUMN golden TEXT")
    # `ablation` (the compact label of the run's capability toggles) was added
    # with the ablation-study feature — forward-migrate older DBs the same way.
    if "ablation" not in cols:
        conn.execute("ALTER TABLE runs ADD COLUMN ablation TEXT")
    return conn


def save_run(
    result: ArmResult,
    *,
    conversation_id: str | None = None,
    batch_id: str | None = None,
    golden: str | None = None,
) -> str:
    run_id = uuid.uuid4().hex[:12]
    # The exact ablation rides extras (full dict) + a compact label column so a
    # batch can be grouped/filtered by the config it ran under.
    ablation_label = result.extras.get("ablation_label", "default")
    with _connect() as conn:
        conn.execute(
            "INSERT INTO runs ("
            "id, ts, question, arm, as_of, answer_text, abstained, abstain_reason, "
            "citations_json, retrieved_json, timings_json, prompt_tokens, output_tokens, "
            "cost_usd, model, extras_json, conversation_id, batch_id, golden, ablation"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                result.question,
                result.arm,
                result.as_of,
                result.answer.text,
                int(result.answer.abstained),
                result.answer.metadata.get("abstain_reason"),
                json.dumps([c.model_dump(mode="json") for c in result.answer.citations],
                           ensure_ascii=False),
                json.dumps([r.model_dump() for r in result.retrieved], ensure_ascii=False),
                json.dumps(result.timings_ms),
                result.prompt_tokens,
                result.output_tokens,
                result.cost_usd,
                result.model,
                json.dumps(result.extras, ensure_ascii=False, default=str),
                conversation_id,
                batch_id,
                golden,
                ablation_label,
            ),
        )
    return run_id


def set_eval(run_id: str, score: dict) -> None:
    """Attach an eval ItemScore (correctness, citation P/R/F1, …) to a run so
    the dashboards can read graded accuracy alongside the raw generation."""
    with _connect() as conn:
        conn.execute(
            "UPDATE runs SET eval_json = ? WHERE id = ?",
            (json.dumps(score, ensure_ascii=False, default=str), run_id),
        )


def get_run(run_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return _inflate(row) if row else None


def list_runs(limit: int = 100, *, batch_id: str | None = None) -> list[dict]:
    where, params = "", []
    if batch_id is not None:
        where = "WHERE COALESCE(batch_id, ?) = ?"
        params = [INTERACTIVE_BATCH, batch_id]
    # clamp: a negative LIMIT is "unlimited" in SQLite — /api/runs?limit=-1
    # would dump the whole table
    params.append(max(1, min(limit, 1000)))
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, ts, question, arm, as_of, abstained, cost_usd, timings_json, "
            f"answer_text, conversation_id, batch_id, eval_json FROM runs {where} "
            "ORDER BY ts DESC LIMIT ?",
            params,
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        timings = json.loads(d.pop("timings_json") or "{}")
        d["total_ms"] = timings.get("total_ms", 0)
        d["answer_preview"] = (d.pop("answer_text") or "")[:160]
        d["abstained"] = bool(d["abstained"])
        ev = json.loads(d.pop("eval_json") or "null")
        d["correct"] = ev.get("correct") if ev else None
        d["batch_id"] = d["batch_id"] or INTERACTIVE_BATCH
        out.append(d)
    return out


def stats(*, batch_id: str | None = None) -> dict:
    where, params = "", []
    if batch_id is not None:
        where = "WHERE COALESCE(batch_id, ?) = ?"
        params = [INTERACTIVE_BATCH, batch_id]
    with _connect() as conn:
        rows = conn.execute(
            "SELECT arm, abstained, cost_usd, timings_json, prompt_tokens, output_tokens, ts, "
            "extras_json "
            f"FROM runs {where}", params
        ).fetchall()
    per_arm: dict[str, dict] = {}
    for row in rows:
        arm = per_arm.setdefault(
            row["arm"],
            {"runs": 0, "abstained": 0, "cost_usd": 0.0, "total_ms": [],
             "prompt_tokens": 0, "output_tokens": 0},
        )
        timings = json.loads(row["timings_json"] or "{}")
        arm["runs"] += 1
        # generation-failed runs are errors, not abstentions (see batch_stats)
        gen_failed = bool(
            json.loads(row["extras_json"] or "{}").get("generation_failed")
        )
        arm["abstained"] += 0 if gen_failed else row["abstained"]
        arm["cost_usd"] += row["cost_usd"]
        arm["prompt_tokens"] += row["prompt_tokens"]
        arm["output_tokens"] += row["output_tokens"]
        if timings.get("total_ms"):
            arm["total_ms"].append(timings["total_ms"])

    def _agg(d: dict) -> dict:
        samples = sorted(d.pop("total_ms"))
        n = len(samples)
        return {
            **d,
            "abstain_rate": round(d["abstained"] / d["runs"], 3) if d["runs"] else 0,
            "avg_cost_usd": round(d["cost_usd"] / d["runs"], 6) if d["runs"] else 0,
            "avg_total_ms": round(sum(samples) / n) if n else 0,
            "p50_total_ms": round(samples[n // 2]) if n else 0,
            "p95_total_ms": round(samples[min(n - 1, int(n * 0.95))]) if n else 0,
            "cost_usd": round(d["cost_usd"], 4),
        }

    return {
        "total_runs": len(rows),
        "total_cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
        "per_arm": {arm: _agg(d) for arm, d in per_arm.items()},
    }


def list_batches() -> list[dict]:
    """One row per batch (incl. the synthetic 'interactive' bucket): id, when,
    run count, arms covered, and the golden set(s) it ran against — newest
    first. `golden` is the set name when unambiguous (None for interactive)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT COALESCE(batch_id, ?) AS batch_id, COUNT(*) AS runs, "
            "MIN(ts) AS first_ts, MAX(ts) AS last_ts, "
            "GROUP_CONCAT(DISTINCT arm) AS arms, "
            "GROUP_CONCAT(DISTINCT golden) AS goldens, "
            "GROUP_CONCAT(DISTINCT ablation) AS ablations, "
            "SUM(CASE WHEN eval_json IS NOT NULL THEN 1 ELSE 0 END) AS scored "
            "FROM runs GROUP BY COALESCE(batch_id, ?) ORDER BY last_ts DESC",
            (INTERACTIVE_BATCH, INTERACTIVE_BATCH),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        goldens = sorted(g for g in (r["goldens"] or "").split(",") if g)
        ablations = sorted(a for a in (d.pop("ablations", None) or "").split(",") if a)
        out.append({
            **d,
            "arms": sorted((r["arms"] or "").split(",")) if r["arms"] else [],
            "goldens": goldens,
            "golden": goldens[0] if len(goldens) == 1 else (goldens[-1] if goldens else None),
            "ablations": ablations,
            # the batch's config when unambiguous (most batches run one config)
            "ablation": ablations[0] if len(ablations) == 1 else (ablations[-1] if ablations else None),
        })
    return out


def batch_stats(batch_id: str) -> dict:
    """Per-arm graded analytics for one batch: accuracy, citation precision/
    recall/F1, temporal accuracy, abstain rate, cost, latency."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT arm, abstained, cost_usd, timings_json, eval_json, extras_json FROM runs "
            "WHERE COALESCE(batch_id, ?) = ?",
            (INTERACTIVE_BATCH, batch_id),
        ).fetchall()
    per_arm: dict[str, dict] = {}
    for row in rows:
        a = per_arm.setdefault(row["arm"], {
            "runs": 0, "abstained": 0, "generation_failed": 0,
            "cost_usd": [], "latency_ms": [], "scored": 0,
            "correct": [], "correctness": [], "precision": [], "recall": [], "f1": [],
            "temporal": [],
            # provenance-safety + retriever-side metrics (eval_json carries them)
            "retrieval_recall": [], "twac": [], "misgrounded": [], "hallucinated": [],
            "answerable": 0, "answerable_abstained": 0,
            "unanswerable": 0, "unanswerable_answered": 0,
        })
        a["runs"] += 1
        # A generation-failed run is an infrastructure error, not an abstention
        # (its answer carries the abstention *shape* for UI safety) — keep the
        # Batches tab consistent with the eval report, which routes it to errors.
        gen_failed = bool(
            json.loads(row["extras_json"] or "{}").get("generation_failed")
        )
        a["generation_failed"] += gen_failed
        a["abstained"] += 0 if gen_failed else row["abstained"]
        a["cost_usd"].append(row["cost_usd"])
        timings = json.loads(row["timings_json"] or "{}")
        if timings.get("total_ms"):
            a["latency_ms"].append(timings["total_ms"])
        ev = json.loads(row["eval_json"] or "null")
        if ev:
            a["scored"] += 1
            a["correct"].append(float(ev.get("correct", False)))
            a["correctness"].append(ev.get("correctness", 0.0))
            a["f1"].append(ev.get("provenance_fidelity", 0.0))
            if ev.get("citation_precision") is not None:
                a["precision"].append(ev["citation_precision"])
            if ev.get("citation_recall") is not None:
                a["recall"].append(ev["citation_recall"])
            if ev.get("temporally_correct") is not None:
                a["temporal"].append(float(ev["temporally_correct"]))
            if ev.get("retrieval_recall") is not None:
                a["retrieval_recall"].append(ev["retrieval_recall"])
            for key, field in (("twac", "temporally_wrong_and_cited"),
                               ("misgrounded", "misgrounded"), ("hallucinated", "hallucinated")):
                if ev.get(field) is not None:
                    a[key].append(float(ev[field]))
            # abstention quality split, keyed on the gold (abstain_target)
            if ev.get("abstain_target"):
                a["unanswerable"] += 1
                a["unanswerable_answered"] += int(not ev.get("abstained", False))
            else:
                a["answerable"] += 1
                a["answerable_abstained"] += int(ev.get("abstained", False))

    def _mean(xs):
        return round(sum(xs) / len(xs), 4) if xs else None

    def _gap(a):
        rr, cr = _mean(a["retrieval_recall"]), _mean(a["recall"])
        return round(rr - cr, 4) if rr is not None and cr is not None else None

    def _rate(num, den):
        return round(num / den, 4) if den else None

    return {
        "batch_id": batch_id,
        "per_arm": {
            arm: {
                "runs": d["runs"],
                "scored": d["scored"],
                "generation_failed": d["generation_failed"],
                "accuracy": _mean(d["correct"]),
                "correctness": _mean(d["correctness"]),
                "citation_precision": _mean(d["precision"]),
                "citation_recall": _mean(d["recall"]),
                "provenance_f1": _mean(d["f1"]),
                "temporal_accuracy": _mean(d["temporal"]),
                "abstain_rate": round(
                    d["abstained"] / (d["runs"] - d["generation_failed"]), 3
                ) if d["runs"] > d["generation_failed"] else 0,
                # retriever-side reach + the surfaced-but-not-cited gap, and
                # the provenance-safety rates (misgrounded / temporally-wrong-cited
                # / hallucination) + the abstention-quality split.
                "retrieval_recall": _mean(d["retrieval_recall"]),
                "retrieval_citation_gap": _gap(d),
                "misgrounded_rate": _mean(d["misgrounded"]),
                "temporally_wrong_and_cited_rate": _mean(d["twac"]),
                "hallucination_rate": _mean(d["hallucinated"]),
                "over_abstention_rate": _rate(d["answerable_abstained"], d["answerable"]),
                "false_answer_rate_unanswerable": _rate(
                    d["unanswerable_answered"], d["unanswerable"]
                ),
                "avg_cost_usd": _mean(d["cost_usd"]),
                "avg_latency_ms": round(sum(d["latency_ms"]) / len(d["latency_ms"]))
                if d["latency_ms"] else 0,
            }
            for arm, d in sorted(per_arm.items())
        },
    }


def batch_items(batch_id: str) -> list[dict]:
    """Per-question view of a batch: each golden item with every arm's graded
    score side by side — powers the per-question analytics + comparison chart."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT arm, question, abstained, cost_usd, timings_json, eval_json, extras_json "
            "FROM runs WHERE COALESCE(batch_id, ?) = ?",
            (INTERACTIVE_BATCH, batch_id),
        ).fetchall()
    by_item: dict[str, dict] = {}
    for row in rows:
        extras = json.loads(row["extras_json"] or "{}")
        item_id = extras.get("eval_item_id")
        if not item_id:
            continue  # only golden-graded runs belong to the per-question view
        ev = json.loads(row["eval_json"] or "null") or {}
        timings = json.loads(row["timings_json"] or "{}")
        d = by_item.setdefault(
            item_id, {"item_id": item_id, "question": row["question"],
                      "tier": ev.get("category", ""), "arms": {}}
        )
        if not d["tier"] and ev.get("category"):
            d["tier"] = ev["category"]
        d["arms"][row["arm"]] = {
            "correct": ev.get("correct"),
            "correctness": ev.get("correctness"),
            "provenance_f1": ev.get("provenance_fidelity"),
            "citation_precision": ev.get("citation_precision"),
            "citation_recall": ev.get("citation_recall"),
            "abstained": bool(row["abstained"]),
            "cost_usd": row["cost_usd"],
            "latency_ms": timings.get("total_ms"),
        }
    tier_order = {"easy": 0, "medium": 1, "hard": 2}
    return sorted(by_item.values(), key=lambda d: (tier_order.get(d["tier"], 9), d["item_id"]))


def _inflate(row: sqlite3.Row) -> dict:
    d = dict(row)
    for field in ("citations_json", "retrieved_json", "timings_json", "extras_json"):
        d[field.removesuffix("_json")] = json.loads(d.pop(field) or "null")
    d["eval"] = json.loads(d.pop("eval_json") or "null")
    d["batch_id"] = d.get("batch_id") or INTERACTIVE_BATCH
    d["abstained"] = bool(d["abstained"])
    return d
