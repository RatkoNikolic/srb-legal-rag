"""Vector-reachability diagnostic for the golden set.

For each gold item, run **flat top-k** vector retrieval on the question and
record, **per gold citation**, whether that unit lands in the top-k. An item is
`reachable_by_flat_topk` only when flat surfaces *every* required citation
(**all-of**, not any-of). The any-citation rule lied for multi-cite structural
items: a `cc_validity` item (`[izreka, article]`) looked "reachable" once top-k
found the *article*, even when it missed the discriminating **`izreka`** — which
is exactly where naive cites a struck article as in force and **graph has a
genuine chance**. All-of is the honest signal: an item only "gives graph no
chance" when flat already has every gold unit in hand. This is standard
leakage/difficulty hygiene (QA benchmarks routinely check lexical-overlap
findability) — surfaced as the `reachable_by_flat_topk` flag plus the
reviewer-legible `flat_topk = {hits, misses}` evidence, reported in the methods
section. It is a **label, never a filter**: the construction charter
(the benchmark design doc §5) commits to *reporting* every stratum, never *selecting*
items by it.

Read both ways (the symmetry that defuses both the rigged-for-graph and the
discriminates-against-graph objection):
- a fully flat-reachable item warns of a possible naive-RAG gimme (leakage);
- a whole set that is flat-reachable warns that *graph never got a chance* —
  if every gold answer is already in flat top-k, structure can't earn its keep.

Usage (writes labels back into the golden file + prints a set-level summary):
  .venv/bin/python -m lexsr.diagnose --golden goldens/golden-situational-60.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lexsr import bench
from lexsr.config import gemini_api_key

FLAT_TOP_K = 8  # the naive arm's k — the diagnostic must mirror the deployable floor


def flat_reachable(
    question: str, gold_citations: list[str], embedder, table, *, k: int = FLAT_TOP_K
) -> dict | None:
    """Per-citation flat top-k reachability for one item.

    Returns ``{"hits": [...], "misses": [...], "all_reachable": bool}`` where
    hits/misses are the original gold-citation strings, and ``all_reachable`` is
    True only when flat top-k surfaces *every* required citation. Returns None
    when the item has no gold citation to find (abstain target). A citation that
    can't be resolved to a chunk_id counts as a **miss** — flat cannot surface
    what doesn't map to a unit."""
    if not gold_citations:
        return None
    vector = embedder.embed([question], task_type="RETRIEVAL_QUERY").vectors[0]
    # mirror the naive arm exactly (incl. its slice-flood guard, 2026-07-29) —
    # the diagnostic is defined as the deployable floor, not raw vector top-k
    rows = (
        table.search(vector)
        .where("is_version_slice = false")
        .limit(k)
        .to_list()
    )
    retrieved = {row.get("chunk_id") for row in rows}
    hits: list[str] = []
    misses: list[str] = []
    for c in gold_citations:
        cid = bench.citation_to_chunk_id(c)
        (hits if cid is not None and cid in retrieved else misses).append(c)
    return {"hits": hits, "misses": misses, "all_reachable": not misses}


def annotate_golden(golden_path: Path, *, k: int = FLAT_TOP_K) -> dict:
    """Label every item with `reachable_by_flat_topk`, write the file back, and
    return a set-level summary (the symmetric read)."""
    from kl_core.providers.gemini import GeminiEmbedder
    from lexsr.index import open_table

    embedder = GeminiEmbedder(gemini_api_key())
    table = open_table()
    items = [json.loads(line) for line in golden_path.open(encoding="utf-8") if line.strip()]

    answerable = 0
    reachable = 0
    for it in items:
        result = flat_reachable(
            it.get("query", {}).get("text", ""), it.get("gold_citations") or [],
            embedder, table, k=k,
        )
        if result is None:
            it["reachable_by_flat_topk"] = None
            it["flat_topk"] = None
            print(f"[diagnose] {it.get('item_id', '?'):6s} reachable=None", flush=True)
            continue
        it["reachable_by_flat_topk"] = result["all_reachable"]
        it["flat_topk"] = {"hits": result["hits"], "misses": result["misses"]}
        answerable += 1
        reachable += int(result["all_reachable"])
        print(
            f"[diagnose] {it.get('item_id', '?'):6s} all_reachable={result['all_reachable']} "
            f"(hits={len(result['hits'])} misses={len(result['misses'])})",
            flush=True,
        )

    with golden_path.open("w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")

    pct = round(reachable / answerable, 3) if answerable else None
    summary = {
        "golden": golden_path.name,
        "items": len(items),
        "answerable": answerable,
        "flat_all_reachable": reachable,  # every gold citation in flat top-k (all-of)
        "flat_all_reachable_pct": pct,
    }
    print(f"\n[diagnose] {summary}")
    if pct is not None and pct >= 0.9:
        print(
            "[diagnose] WARNING: ≥90% of answerable items are fully flat-reachable — "
            "the structure-aware arms may have little room to differentiate."
        )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, required=True)
    parser.add_argument("--k", type=int, default=FLAT_TOP_K)
    args = parser.parse_args()
    if not args.golden.exists():
        print(f"golden not found: {args.golden}", file=sys.stderr)
        return 2
    annotate_golden(args.golden, k=args.k)
    return 0


if __name__ == "__main__":
    sys.exit(main())
