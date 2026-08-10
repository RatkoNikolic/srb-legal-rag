"""Benchmark authoring + run orchestration behind the Benchmark UI tab.

Two responsibilities, both thin:
- **Golden CRUD** over the live benchmark file (`CURRENT_GOLDEN`, default `goldens/golden-situational-60.jsonl`):
  list / upsert / delete questions, with mechanical validation that every gold
  citation resolves to a *non-empty* corpus chunk (an empty stub is ungrounded).
- **Batch runs**: kick off `lexsr.evals.run_eval` on a background thread and
  expose live progress, so a user can configure + launch a benchmark from the UI
  without blocking the request. Jobs are tracked in memory (single-worker uvicorn).
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from lexsr.config import CORPUS_DIR, CURRENT_GOLDEN, GOLDENS_DIR

TIERS = ("easy", "medium", "hard")

_CITE_RE = re.compile(r"^(?P<src>.+)::\s*(?:član|члан)?\s*(?P<num>\S+)\s*$", re.IGNORECASE)
_nonempty_cache: set[str] | None = None
_chunk_text_cache: dict[str, str] | None = None

# Gold-answer grounding: a gold answer must be *supported by* the verbatim
# text it cites, not merely point at a non-empty chunk. We measure lexical recall
# of the gold answer's content words against the union of its cited article texts
# — the mechanical substitute for expert review (benchmark design doc §5). It
# catches fabrication and citation-drift; it does NOT catch legal-interpretation
# error (the stated Phase-2 residual). The threshold is deliberately lenient: a
# correct client-voice answer adds framing words ("Не.", "Послодавац…") absent
# from the statute, so we require a majority — not all — of its content words to
# trace to the cited text.
GROUNDING_MIN_OVERLAP = 0.5
# Content words: ≥3 chars drops the Serbian function-word tail (и, у, на, се, да,
# за …), which is mostly 1–2 chars, without a hand-maintained stoplist.
_WORD_RE = re.compile(r"[\w]+", re.UNICODE)


# ── corpus-grounded citation validation ───────────────────────────────────
def _load_chunks() -> None:
    """Populate the id→nonempty and id→text caches in one pass over the corpus."""
    global _nonempty_cache, _chunk_text_cache
    ids: set[str] = set()
    texts: dict[str, str] = {}
    path = CORPUS_DIR / "chunks.jsonl"
    if path.exists():
        for line in path.open(encoding="utf-8"):
            c = json.loads(line)
            text = c.get("text", "")
            if text.strip():
                ids.add(c["chunk_id"])
                texts[c["chunk_id"]] = text
    _nonempty_cache = ids
    _chunk_text_cache = texts


def _nonempty_chunks() -> set[str]:
    if _nonempty_cache is None:
        _load_chunks()
    return _nonempty_cache  # type: ignore[return-value]


def _chunk_texts() -> dict[str, str]:
    if _chunk_text_cache is None:
        _load_chunks()
    return _chunk_text_cache  # type: ignore[return-value]


def _content_words(text: str) -> set[str]:
    return {w for w in (m.group().lower() for m in _WORD_RE.finditer(text)) if len(w) >= 3}


def grounding_overlap(gold_answer: str, cited_texts: list[str]) -> float:
    """Fraction of the gold answer's content words that appear in the union of its
    cited article texts. 1.0 when the gold answer adds no out-of-statute words;
    near 0 when the answer is fabricated or cites the wrong article."""
    gold = _content_words(gold_answer)
    if not gold:
        return 1.0
    source: set[str] = set()
    for t in cited_texts:
        source |= _content_words(t)
    return len(gold & source) / len(gold)


def citation_to_chunk_id(citation: str) -> str | None:
    m = _CITE_RE.match(citation.strip())
    if not m:
        return None
    num = m["num"].strip().rstrip(".")
    # CC decisions are cited by their holding fragment (izreka), not a član number
    if num.lower() in ("izreka", "изрека", "holding"):
        return f"{m['src']}/holding"
    return f"{m['src']}/clan-{num}"


def validate_item(item: dict) -> list[str]:
    """Return a list of problems; empty means the item is valid + grounded."""
    problems: list[str] = []
    if not item.get("query", {}).get("text", "").strip():
        problems.append("empty question text")
    if item.get("category") not in TIERS:
        problems.append(f"category must be one of {TIERS}")
    # Abstain targets (gold_answer is None) legitimately carry no citations —
    # the correct behavior is refusal, so an empty gold citation list is valid.
    is_abstain = item.get("gold_answer") is None
    cites = item.get("gold_citations") or []
    if not cites and not is_abstain:
        problems.append("at least one gold citation is required (non-abstain item)")
    nonempty = _nonempty_chunks()
    texts = _chunk_texts()
    # A missing `chunks.jsonl` makes *every* citation look unresolvable, which
    # reads as "the goldens are broken" when the real cause is that the derived
    # artifact was never built (it is regenerated locally, not shipped). Say so
    # once, instead of emitting one misleading problem per citation.
    if not nonempty:
        return [
            "corpus chunks not built — run `python -m lexsr.ingest all` first "
            "(citation grounding cannot be checked without data/corpus/chunks.jsonl)"
        ]
    cited_texts: list[str] = []
    for c in cites:
        cid = citation_to_chunk_id(c)
        if cid is None:
            problems.append(f"unparseable citation: {c!r}")
        elif cid not in nonempty:
            problems.append(f"citation has no non-empty corpus chunk: {c!r}")
        elif cid in texts:
            cited_texts.append(texts[cid])
    # grounding: only when we could actually read the cited text (skipped in
    # id-only unit fixtures, and never applied to abstain items, which cite nothing).
    gold = item.get("gold_answer")
    if gold and cited_texts and not problems:
        overlap = grounding_overlap(gold, cited_texts)
        if overlap < GROUNDING_MIN_OVERLAP:
            problems.append(
                f"gold answer not grounded in cited text (content-word overlap "
                f"{overlap:.0%} < {GROUNDING_MIN_OVERLAP:.0%})"
            )
    return problems


# ── golden sets ────────────────────────────────────────────────────────────
# A *golden set* is one `*.jsonl` file under goldens/; its name is the file
# stem. The benchmark scopes everything — the question editor, the run target,
# and the metrics filter — to a chosen set, so several can coexist (e.g. the
# full v3 set plus a cheap `smoke` subset for harness checks).
def set_path(name: str | Path | None = None) -> Path:
    """Resolve a golden-set NAME (file stem) to its path under goldens/.

    `None` → the live default set (`CURRENT_GOLDEN`). A `Path` passes through
    (callers that already hold a file). Guards against traversal: any directory
    component is stripped and the result must land directly in GOLDENS_DIR.
    """
    if name is None:
        return CURRENT_GOLDEN
    if isinstance(name, Path):
        return name
    stem = Path(name).name.removesuffix(".jsonl")  # strip dirs + extension
    if not stem or stem.startswith("."):
        raise ValueError(f"invalid golden set name: {name!r}")
    path = (GOLDENS_DIR / f"{stem}.jsonl").resolve()
    if path.parent != GOLDENS_DIR.resolve():
        raise ValueError(f"invalid golden set name: {name!r}")
    return path


def golden_sets() -> list[dict]:
    """Every `*.jsonl` golden file under goldens/, newest-relevant first, each
    with its item count; the live default set is flagged so the UI preselects
    it. The default is always present even if it lives outside goldens/ (tests)."""
    default = CURRENT_GOLDEN.resolve()
    out: list[dict] = []
    seen: set[Path] = set()
    for path in sorted(GOLDENS_DIR.glob("*.jsonl")):
        rp = path.resolve()
        seen.add(rp)
        out.append({
            "name": path.stem,
            "items": len(load_items(path)),
            "is_default": rp == default,
        })
    if default not in seen and CURRENT_GOLDEN.exists():
        out.insert(0, {"name": CURRENT_GOLDEN.stem,
                       "items": len(load_items(CURRENT_GOLDEN)), "is_default": True})
    return out


# ── golden CRUD ────────────────────────────────────────────────────────────
def load_items(golden: str | Path | None = None) -> list[dict]:
    path = set_path(golden)  # resolved at call time so tests can repoint CURRENT_GOLDEN
    if not path.exists():
        return []
    return [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]


def _save_items(items: list[dict], path: Path | None = None) -> None:
    path = path or CURRENT_GOLDEN
    order = {t: i for i, t in enumerate(TIERS)}

    def key(it: dict):
        m = re.match(r"[a-z]+(\d+)", it.get("item_id", ""))
        return (order.get(it.get("category"), 9), int(m.group(1)) if m else 0)

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for it in sorted(items, key=key):
            f.write(json.dumps(it, ensure_ascii=False) + "\n")


def _next_id(items: list[dict], category: str) -> str:
    prefix = {"easy": "e", "medium": "m", "hard": "h"}[category]
    nums = [
        int(re.match(rf"{prefix}(\d+)", it["item_id"]).group(1))
        for it in items
        if it.get("item_id", "").startswith(prefix)
        and re.match(rf"{prefix}(\d+)$", it["item_id"])
    ]
    return f"{prefix}{(max(nums) + 1) if nums else 1:02d}"


_GOLDEN_LOCK = threading.Lock()  # CRUD is read-modify-write on a shared file


def upsert_item(item: dict, golden: str | Path | None = None) -> dict:
    """Add or update one golden item in the chosen set. An update MERGES into
    the stored item (shallow, with `query` merged one level deeper) — a partial
    payload from the editor must not strip benchmark annotations the UI doesn't
    surface (`capability`, `temporal_mode`, `query.as_of`, reachability labels);
    all 60 live items carry them and scoring silently degrades without them.
    Raises ValueError on validation failure (or an unknown set name)."""
    path = set_path(golden)
    with _GOLDEN_LOCK:
        items = load_items(path)
        if not item.get("item_id"):
            item["item_id"] = _next_id(items, item["category"])
        by_id = {it["item_id"]: it for it in items}
        prior = by_id.get(item["item_id"], {})
        merged = {**prior, **item}
        if "query" in prior and "query" in item:
            merged["query"] = {**prior["query"], **item["query"]}
        problems = validate_item(merged)
        if problems:
            raise ValueError("; ".join(problems))
        by_id[merged["item_id"]] = merged
        _save_items(list(by_id.values()), path)
    return merged


def delete_item(item_id: str, golden: str | Path | None = None) -> bool:
    path = set_path(golden)
    with _GOLDEN_LOCK:
        items = load_items(path)
        kept = [it for it in items if it.get("item_id") != item_id]
        if len(kept) == len(items):
            return False
        _save_items(kept, path)
    return True


# ── batch-run jobs ─────────────────────────────────────────────────────────
_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


def start_batch(
    arms: list[str],
    *,
    categories: list[str] | None = None,
    limit: int | None = None,
    batch_id: str | None = None,
    golden: str | Path | None = None,
    ablation=None,
) -> dict:
    """Launch a benchmark run on a background thread; returns the job record.

    `golden` selects which golden set to measure against (None = the live
    default). The set name is recorded on every run so the metrics view can be
    filtered back to it. `ablation` (an AblationConfig) applies the same
    capability toggles to every arm in the run — its label is recorded on each
    run so a batch is grouped by the config it ran under."""
    from lexsr.arms.base import AblationConfig
    from lexsr.evals import run_eval  # local import: avoids a heavy import at startup

    if ablation is not None and not isinstance(ablation, AblationConfig):
        ablation = AblationConfig(**ablation)
    golden_path = set_path(golden)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    batch_id = batch_id or f"bench-{stamp}-{uuid.uuid4().hex[:4]}"
    items = load_items(golden_path)
    if categories:
        items = [it for it in items if it.get("category") in categories]
    n_items = len(items[:limit]) if limit else len(items)
    job_id = uuid.uuid4().hex[:12]
    job = {
        "job_id": job_id,
        "batch_id": batch_id,
        "golden": golden_path.stem,
        "arms": arms,
        "categories": categories or list(TIERS),
        "ablation": ablation.label() if ablation is not None else "default",
        "status": "running",
        "done": 0,
        "total": n_items * len(arms),
        "last": None,
        "started": stamp,
        "error": None,
    }
    with _LOCK:
        _JOBS[job_id] = job

    def _progress(done, total, arm, item_id):
        with _LOCK:
            job.update(done=done, total=total, last=f"{arm}/{item_id}")

    def _run():
        try:
            run_eval(
                arms, golden_path=golden_path, limit=limit, batch_id=batch_id,
                categories=categories, config=ablation, progress=_progress,
            )
            with _LOCK:
                job["status"] = "done"
        except Exception as exc:  # surface failure to the UI, don't crash the server
            with _LOCK:
                job["status"] = "error"
                job["error"] = str(exc)

    threading.Thread(target=_run, daemon=True).start()
    return job


def get_job(job_id: str) -> dict | None:
    with _LOCK:
        job = _JOBS.get(job_id)
        return dict(job) if job else None


def list_jobs() -> list[dict]:
    with _LOCK:
        return sorted((dict(j) for j in _JOBS.values()), key=lambda j: j["started"], reverse=True)
