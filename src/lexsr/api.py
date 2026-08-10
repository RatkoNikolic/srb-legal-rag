"""FastAPI server: the arms + run telemetry behind a small JSON API.

Run: .venv/bin/uvicorn lexsr.api:app --port 8000 --reload
The Vite dev server (web/) proxies /api to :8000.
"""

from __future__ import annotations

import json
import re
from datetime import date
from functools import lru_cache

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from kl_core.providers.gemini import GeminiEmbedder, GeminiLLM
from lexsr import bench, runstore
from lexsr.arms import ARMS
from lexsr.arms.base import ABLATION_SPEC, AblationConfig, RetrievalArm
from lexsr.config import CORPUS_DIR, REPO_ROOT, gemini_api_key
from lexsr.index import open_table
from lexsr.kg import open_connection

app = FastAPI(title="srb-legal-rag", version="0.1")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@lru_cache(maxsize=1)
def _shared() -> dict:
    key = gemini_api_key()
    return {
        "llm": GeminiLLM(key),
        "embedder": GeminiEmbedder(key),
        "table": open_table(),
        "kg": open_connection(),
    }


@lru_cache(maxsize=8)
def _arm(name: str) -> RetrievalArm:
    if name not in ARMS:
        raise HTTPException(404, f"unknown arm {name!r}; have {sorted(ARMS)}")
    s = _shared()
    return ARMS[name](s["llm"], s["embedder"], s["table"], s["kg"])


def _arm_with(name: str, config: AblationConfig | None) -> RetrievalArm:
    """The arm for this request: the cached full-pipeline instance when no
    ablation is set, else a fresh instance carrying the per-request config (the
    LRU cache only fits the default config — ablated arms aren't cacheable)."""
    if name not in ARMS:
        raise HTTPException(404, f"unknown arm {name!r}; have {sorted(ARMS)}")
    if config is None or not config.nondefault():
        return _arm(name)
    s = _shared()
    return ARMS[name](s["llm"], s["embedder"], s["table"], s["kg"], config=config)


class Turn(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class AskRequest(BaseModel):
    question: str
    arm: str = "advanced"
    as_of: date | None = None
    law_slug: str | None = None  # stuffing arm only: the oracle target law
    history: list[Turn] = []  # prior conversation turns → multi-turn continuity
    conversation_id: str | None = None  # groups runs of one conversation
    ablation: AblationConfig | None = None  # per-request capability toggles


@app.post("/api/ask")
def ask(req: AskRequest) -> dict:
    if not req.question.strip():
        raise HTTPException(422, "empty question")
    if req.arm not in ARMS:
        raise HTTPException(404, f"unknown arm {req.arm!r}; have {sorted(ARMS)}")
    hints = {"law_slug": req.law_slug} if req.arm == "context-stuffing" and req.law_slug else {}
    history = [t.model_dump() for t in req.history] or None
    arm = _arm_with(req.arm, req.ablation)
    result = arm.answer(req.question.strip(), as_of=req.as_of, history=history, **hints)
    run_id = runstore.save_run(result, conversation_id=req.conversation_id)
    run = runstore.get_run(run_id)
    assert run is not None
    return run


@app.get("/api/arms")
def arms() -> list[str]:
    return sorted(ARMS)


@app.get("/api/ablations")
def ablations() -> dict:
    """The ablation surface for the UI: each toggle's key/label/group/arms/
    description + the production default, straight from AblationConfig — so the
    controls render generically and stay in sync with the backend."""
    return {"spec": ABLATION_SPEC, "defaults": AblationConfig().model_dump()}


@app.get("/api/runs")
def runs(limit: int = 100, batch_id: str | None = None) -> list[dict]:
    return runstore.list_runs(limit=min(limit, 1000), batch_id=batch_id)


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str) -> dict:
    run = runstore.get_run(run_id)
    if run is None:
        raise HTTPException(404, "no such run")
    return run


@app.get("/api/stats")
def stats(batch_id: str | None = None) -> dict:
    return runstore.stats(batch_id=batch_id)


@app.get("/api/batches")
def batches() -> list[dict]:
    return runstore.list_batches()


@app.get("/api/batches/{batch_id}/stats")
def batch_stats(batch_id: str) -> dict:
    return runstore.batch_stats(batch_id)


@app.get("/api/batches/{batch_id}/items")
def batch_items(batch_id: str) -> list[dict]:
    return runstore.batch_items(batch_id)


# ── benchmark authoring + runs (Benchmark tab) ──────────────────────────────
@app.get("/api/golden/sets")
def golden_sets() -> list[dict]:
    """The available golden sets (one `*.jsonl` per set) with item counts; the
    live default is flagged so the UI preselects it."""
    return bench.golden_sets()


@app.get("/api/golden")
def golden(golden: str | None = None) -> list[dict]:
    """Items of one golden set (`?golden=<name>`); default = the live set."""
    try:
        return bench.load_items(golden)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.put("/api/golden")
def golden_upsert(item: dict, golden: str | None = None) -> dict:
    try:
        return bench.upsert_item(item, golden)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.delete("/api/golden/{item_id}")
def golden_delete(item_id: str, golden: str | None = None) -> dict:
    try:
        deleted = bench.delete_item(item_id, golden)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not deleted:
        raise HTTPException(404, f"no golden item {item_id!r}")
    return {"deleted": item_id}


class RunConfig(BaseModel):
    arms: list[str] = ["naive", "advanced", "graph", "context-stuffing", "agentic"]
    categories: list[str] | None = None  # None = all tiers
    limit: int | None = None
    batch_id: str | None = None
    golden: str | None = None  # which golden set to measure against (None = live default)
    ablation: AblationConfig | None = None  # capability toggles applied to every arm in the run


@app.post("/api/benchmark/run")
def benchmark_run(cfg: RunConfig) -> dict:
    unknown = [a for a in cfg.arms if a not in ARMS]
    if unknown:
        raise HTTPException(422, f"unknown arms {unknown}; have {sorted(ARMS)}")
    if not cfg.arms:
        raise HTTPException(422, "select at least one arm")
    try:
        return bench.start_batch(
            cfg.arms, categories=cfg.categories, limit=cfg.limit,
            batch_id=cfg.batch_id, golden=cfg.golden, ablation=cfg.ablation,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/benchmark/jobs")
def benchmark_jobs() -> list[dict]:
    return bench.list_jobs()


@app.get("/api/benchmark/jobs/{job_id}")
def benchmark_job(job_id: str) -> dict:
    job = bench.get_job(job_id)
    if job is None:
        raise HTTPException(404, "no such job")
    return job


# Corpus tab shows "the laws" (seeds + backbone), not the ~2k materialized
# bylaws / CC holdings / stubs that the uncap (ADR-9) added — those are the
# periphery, surfaced as counts via /api/corpus/summary.
_LAW_ROLES = {"seed", "hub", "foundational"}


def _manifest() -> list[dict]:
    path = CORPUS_DIR / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


@lru_cache(maxsize=1)
def _repealed_by_law() -> dict:
    """Per-law count of repealed članci, read from the chunk metadata. The manifest
    never recorded `repealed_clanovi`, so without this the corpus 'repealed članova'
    figure is missing (NaN) — there genuinely are repealed articles (e.g. ZZPL-2008
    64, PIO 30), so the honest value is computed here, not defaulted to 0."""
    counts: dict[str, int] = {}
    path = CORPUS_DIR / "chunks.jsonl"
    if not path.exists():
        return counts
    with path.open(encoding="utf-8") as f:
        for line in f:
            md = json.loads(line).get("metadata", {})
            if md.get("repealed") or md.get("status") == "repealed":
                slug = md.get("law_slug")
                if slug:
                    counts[slug] = counts.get(slug, 0) + 1
    return counts


@app.get("/api/corpus")
def corpus(include: str = "laws") -> list[dict]:
    """`include=laws` (default) → the seed+backbone laws; `include=all` → every
    act (laws + materialized bylaws/CC + stubs). Each act is annotated with its
    real `repealed_clanovi` count (from chunk metadata — the manifest omits it)."""
    rep = _repealed_by_law()
    # Prefer the manifest's stored count (backfilled / written at ingest); compute
    # from chunk metadata only as a fallback for entries that lack it.
    manifest = [
        {**m, "repealed_clanovi": m.get("repealed_clanovi", rep.get(m.get("slug", ""), 0))}
        for m in _manifest()
    ]
    if include == "all":
        return manifest
    return [m for m in manifest if m.get("role") in _LAW_ROLES]


@app.get("/api/corpus/summary")
def corpus_summary() -> dict:
    """Counts for the whole KB so the Corpus tab can show the document layer the
    uncap added (laws vs materialized bylaws/CC vs stubs)."""
    manifest = _manifest()
    mat = [m for m in manifest if m.get("materialized")]
    return {
        "laws": sum(1 for m in manifest if m.get("role") in _LAW_ROLES),
        "materialized_bylaws": sum(1 for m in mat if m.get("vrsta") == "podzakonski_akt"),
        "cc_decisions": sum(1 for m in mat if m.get("vrsta") == "odluka_us"),
        "referenced_materialized": sum(1 for m in manifest if m.get("role") == "referenced"),
        "stubs": sum(1 for m in manifest if m.get("role") == "stub"),
        "acts_total": len(manifest),
    }


@app.get("/api/corpus/stats")
def corpus_stats() -> dict:
    """KG density metrics (written by `lexsr.kg`) — the diagnostic behind the
    graph-sparsity finding (benchmark.md §6). Empty if not built yet."""
    path = CORPUS_DIR / "graph_stats.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


@app.get("/api/corpus/graph")
def corpus_graph() -> dict:
    """Law-level cross-reference graph for the Corpus tab: nodes are laws
    (sized by članova, coloured by cluster), edges are UPUĆUJE_NA references
    aggregated across laws (intra-law references excluded)."""
    path = CORPUS_DIR / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    law_slugs = [law["slug"] for law in manifest if law.get("role") in _LAW_ROLES]
    if not law_slugs:
        return {"nodes": [], "edges": []}
    kg = _shared()["kg"]
    marks = ",".join("?" for _ in law_slugs)

    # Both queries are scoped to the law nodes below — otherwise the edge list
    # carries ref/* (bylaw/CC) endpoints that never appear in `nodes` (dangling
    # references for any renderer that joins them) and `internal_refs` counts
    # periphery acts the graph doesn't show.
    internal = {
        r["slug"]: int(r["w"])
        for r in kg.query(
            "SELECT a.law_slug AS slug, count(*) AS w FROM upucuje_na e "
            "JOIN clan a ON a.chunk_id = e.src JOIN clan b ON b.chunk_id = e.dst "
            f"WHERE a.law_slug = b.law_slug AND a.law_slug IN ({marks}) "
            "GROUP BY a.law_slug",
            tuple(law_slugs),
        )
    }
    nodes = [
        {
            "slug": law["slug"],
            "title": law["title"],
            "cluster": law.get("cluster", ""),
            "clanova": law.get("chunks", law.get("main_clanovi", 0)),
            "repealed": law.get("repealed_clanovi", 0),
            "amendments": len(law.get("gazette_chain", [])),
            "internal_refs": internal.get(law["slug"], 0),
        }
        for law in manifest
        if law.get("role") in _LAW_ROLES  # the law-level graph is the laws, not the periphery
    ]
    edges = [
        {"source": r["src"], "target": r["dst"], "weight": int(r["w"])}
        for r in kg.query(
            "SELECT a.law_slug AS src, b.law_slug AS dst, count(*) AS w FROM upucuje_na e "
            "JOIN clan a ON a.chunk_id = e.src JOIN clan b ON b.chunk_id = e.dst "
            f"WHERE a.law_slug <> b.law_slug AND a.law_slug IN ({marks}) "
            f"AND b.law_slug IN ({marks}) GROUP BY a.law_slug, b.law_slug",
            tuple(law_slugs) + tuple(law_slugs),
        )
    ]
    return {"nodes": nodes, "edges": edges}


def _clan_sort_key(number: str) -> tuple[int, str]:
    m = re.match(r"(\d+)(.*)", number or "")
    return (int(m.group(1)), m.group(2)) if m else (10**6, number or "")


@app.get("/api/corpus/article-graph")
def corpus_article_graph(slugs: str | None = None) -> dict:
    """Article-level graph over a selection of laws (default: the whole corpus).
    Nodes are članci (chunk_id, in document order, coloured by cluster); edges
    are UPUĆUJE_NA references — both intra-law and cross-law within the
    selection. `slugs` is a comma-separated law-slug filter."""
    path = CORPUS_DIR / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    cluster_by = {law["slug"]: law.get("cluster", "") for law in manifest}
    requested = [s for s in (slugs.split(",") if slugs else list(cluster_by)) if s in cluster_by]
    sel = requested or list(cluster_by)
    if not sel:  # empty manifest (partial checkout) — an IN () would be a SQL error
        return {"slugs": [], "nodes": [], "edges": []}
    # slugs are validated against the manifest → parameterized IN list
    marks = ",".join("?" for _ in sel)
    kg = _shared()["kg"]

    nodes = [
        {
            "chunk_id": r["cid"],
            "law_slug": r["slug"],
            "cluster": cluster_by.get(r["slug"], ""),
            "number": r["number"],
            "fragment": r["fragment"],
            "repealed": bool(r["repealed"]) or bool(r["vt"]),
        }
        # slices excluded: they share the parent's law_slug and would render an
        # amended article once per version, every copy mis-flagged repealed via
        # its bounded valid_to (zakon-o-radu alone: 1,190 phantom dots)
        for r in kg.query(
            "SELECT chunk_id AS cid, law_slug AS slug, number, fragment, "
            f"repealed, valid_to AS vt FROM clan WHERE law_slug IN ({marks}) "
            "AND is_version_slice = 0",
            tuple(sel),
        )
    ]
    nodes.sort(key=lambda n: (n["law_slug"], _clan_sort_key(n["number"])))
    edges = [
        {"source": r["src"], "target": r["dst"]}
        for r in kg.query(
            "SELECT a.chunk_id AS src, b.chunk_id AS dst FROM upucuje_na e "
            "JOIN clan a ON a.chunk_id = e.src JOIN clan b ON b.chunk_id = e.dst "
            f"WHERE a.law_slug IN ({marks}) AND b.law_slug IN ({marks})",
            tuple(sel) + tuple(sel),
        )
    ]
    return {"slugs": sel, "nodes": nodes, "edges": edges}


# — production serving: when the frontend has been built (`cd web && npm run
# build`), serve it from this process — a one-command deploy (uvicorn only),
# same-origin `/api` so the built app needs no proxy. Registered last, so API
# routes always win; dev keeps using Vite :5173 (web/dist absent → no mount).
_DIST = REPO_ROOT / "web" / "dist"
if _DIST.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=_DIST, html=True), name="app")
