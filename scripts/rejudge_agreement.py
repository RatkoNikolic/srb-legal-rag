"""Re-grade a stored batch with the second-family juror, keeping the join.

Krippendorff's α is only interpretable if you can attribute a disagreement to an
item, an arm, or a verdict boundary. The campaign harness collected bare label
pairs and discarded that join, leaving a scalar nobody could interrogate — this
script recovers it from the stored answers, at the cost of one cheap juror pass.

It changes nothing about the published grades: the primary judge's verdicts are
read from telemetry, never recomputed. Only the juror's side is re-derived.

Usage:
    .venv/bin/python scripts/rejudge_agreement.py [--batch <id>] [--limit N]

Writes data/evals/agreement_<batch>.json with per-item labels plus nominal,
ordinal and binarised α.
"""

from __future__ import annotations

import argparse
import json
import sqlite3

from kl_core.evals import krippendorff_alpha
from lexsr.config import CURRENT_GOLDEN, EVALS_DIR, RUNS_DB
from lexsr.evals import _SCORE_LABEL, load_golden, make_judge
from kl_core.evals import grade

ORDINAL = {"incorrect": 0, "partially_correct": 1, "correct": 2}


def _ordinal_alpha(units: list[list[str | None]]) -> float | None:
    """Krippendorff's α with an interval distance over the ordered verdicts.

    The nominal coefficient treats correct-vs-partial as exactly as far apart as
    correct-vs-incorrect. On an ordered three-level scale that understates
    agreement, so the ordered reading is reported alongside it.
    """
    pairs = [(ORDINAL[a], ORDINAL[b]) for a, b in units if a in ORDINAL and b in ORDINAL]
    if len(pairs) < 2:
        return None
    do = sum((a - b) ** 2 for a, b in pairs) / len(pairs)
    vals = [v for p in pairs for v in p]
    n = len(vals)
    de = sum((x - y) ** 2 for x in vals for y in vals) / (n * (n - 1))
    return None if de == 0 else round(1 - do / de, 4)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", default=None, help="batch_id (default: the only one present)")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    if not RUNS_DB.exists():
        raise SystemExit(
            f"no telemetry store at {RUNS_DB} — this re-grades a batch you have already\n"
            "run locally. Run a campaign first:\n"
            "  python -m lexsr.evals --arms naive,advanced,graph,agentic,context-stuffing --dual-judge"
        )
    conn = sqlite3.connect(f"file:{RUNS_DB}?mode=ro", uri=True)
    batch = args.batch or conn.execute(
        "select batch_id from runs where batch_id is not null group by batch_id "
        "order by count(*) desc limit 1"
    ).fetchone()[0]

    gold = {it.item_id: it for it in load_golden(CURRENT_GOLDEN)}
    rows = conn.execute(
        "select arm, answer_text, abstained, eval_json from runs "
        "where batch_id = ? and eval_json is not null order by arm, id",
        (batch,),
    ).fetchall()

    juror = make_judge("gemini")
    out: list[dict] = []
    for arm, answer_text, abstained, ej in rows:
        e = json.loads(ej)
        item = gold.get(e["item_id"])
        if item is None or item.gold_answer is None:
            continue  # abstain targets are scored deterministically, never judged
        if args.limit and len(out) >= args.limit:
            break
        candidate = "(abstained)" if abstained else (answer_text or "")
        try:
            _s, verdict, _r, _m = grade(
                juror, item.query.text, item.gold_answer, candidate, as_of=item.query.as_of
            )
        except Exception as exc:  # a juror failure must not discard a primary grade
            print(f"  ! {arm}/{e['item_id']}: {exc}")
            continue
        out.append({
            "item_id": e["item_id"], "arm": arm, "category": e.get("category"),
            "capability": e.get("capability"), "dated": item.query.as_of is not None,
            "primary": _SCORE_LABEL.get(e["correctness"]), "secondary": verdict,
        })
        print(f"  {arm:17s} {e['item_id']:5s} primary={out[-1]['primary']:18s} juror={verdict}")

    units = [[r["primary"], r["secondary"]] for r in out]
    binary = [[None if v is None else ("correct" if v == "correct" else "not_correct")
               for v in u] for u in units]
    result = {
        "batch_id": batch,
        "n_items": len(out),
        "krippendorff_alpha": krippendorff_alpha(units),
        "krippendorff_alpha_ordinal": _ordinal_alpha(units),
        "krippendorff_alpha_binarised": krippendorff_alpha(binary),
        "per_item": out,
    }
    EVALS_DIR.mkdir(parents=True, exist_ok=True)
    path = EVALS_DIR / f"agreement_{batch}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  n = {result['n_items']}")
    print(f"  alpha nominal   = {result['krippendorff_alpha']}")
    print(f"  alpha ordinal   = {result['krippendorff_alpha_ordinal']}")
    print(f"  alpha binarised = {result['krippendorff_alpha_binarised']}")
    print(f"  -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
