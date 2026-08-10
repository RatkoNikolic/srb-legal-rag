"""Vector + full-text index over the corpus (LanceDB, embedded).

One row per chunk: the embedding (gemini-embedding-2, RETRIEVAL_DOCUMENT),
the text, and the provenance/temporal fields the retrieval arms filter on.
Lance-native FTS over the text gives the BM25 side of hybrid retrieval
(lancedb ≥0.19 default; NOT tantivy — the default analyzer is English +
ascii-folding + lowercase, i.e. effectively unstemmed for Serbian Cyrillic).

Usage: .venv/bin/python -m lexsr.index
"""

from __future__ import annotations

import json
import sys

import lancedb

from kl_core.providers.gemini import GeminiEmbedder
from kl_core.types import Chunk
from lexsr.config import CORPUS_DIR, INDEX_DIR, gemini_api_key

TABLE = "chunks"
_EMBED_CHUNK_TEXT_LIMIT = 28_000  # ~8k tokens; viewdoc članovi rarely exceed this


def load_chunks() -> list[Chunk]:
    path = CORPUS_DIR / "chunks.jsonl"
    return [Chunk(**json.loads(line)) for line in path.open(encoding="utf-8")]


def embed_text(chunk: Chunk) -> str:
    """The text that gets embedded (D-14, ingestion_pipeline.md §8 bucket 2): the
    article body + a *light* contextual prefix (law title › path › status), which
    lifts retrieval of short/anaphoric articles. The FTS leg indexes the bare
    `text` separately — relational metadata is never embedded."""
    prefix = chunk.metadata.get("contextual_prefix") or (
        f"{chunk.provenance.source_title} — {chunk.provenance.fragment}"
    )
    return f"{prefix}\n{chunk.text}"[:_EMBED_CHUNK_TEXT_LIMIT]


def _row(chunk: Chunk, vector: list[float]) -> dict:
    p = chunk.provenance
    return {
        "chunk_id": chunk.chunk_id,
        "vector": vector,
        "text": chunk.text,
        "law_slug": chunk.metadata.get("law_slug", ""),
        "source_id": p.source_id,
        "source_title": p.source_title,
        "fragment": p.fragment or "",
        "version": p.version or "",
        "valid_from": str(p.valid_from or ""),  # entry into force (Q8)
        "valid_to": str(p.valid_to or ""),
        "applies_from": chunk.metadata.get("applies_from", ""),  # početak primene
        "published": chunk.metadata.get("published", ""),  # objavljivanje
        "url": p.url or "",
        "repealed": bool(chunk.metadata.get("repealed", False)),
        "status": chunk.metadata.get("status", ""),
        "chapter": chunk.metadata.get("chapter") or "",
        # structural neighbourhood (Tier-1 metadata) — filterable + surfaced
        "structural_path": chunk.metadata.get("structural_path", ""),
        "article_heading": chunk.metadata.get("article_heading", ""),
        "prev_clan": chunk.metadata.get("prev_clan", ""),
        "next_clan": chunk.metadata.get("next_clan", ""),
        "is_definitions": bool(chunk.metadata.get("is_definitions", False)),
        "is_penalty": bool(chunk.metadata.get("is_penalty", False)),
        "delegates_to_bylaw": bool(chunk.metadata.get("delegates_to_bylaw", False)),
        # L0–L3 facets: act type, corpus role, depth, version slice
        "vrsta": chunk.metadata.get("vrsta", "zakon"),
        "role": chunk.metadata.get("role", "seed"),
        "in_corpus_depth": int(chunk.metadata.get("in_corpus_depth", 0)),
        "is_version_slice": bool(chunk.metadata.get("is_version_slice", False)),
        "cc_outcome": chunk.metadata.get("cc_outcome", ""),
        "cluster": chunk.metadata.get("cluster", ""),
        # annex provenance (DS-3/IP-5): text | table_html | ocr — lets arms
        # include/exclude OCR-derived (non-verbatim) content.
        "source_modality": chunk.metadata.get("source_modality", "text"),
        "is_annex": bool(chunk.metadata.get("is_annex", False)),
        "metadata_json": json.dumps(chunk.metadata, ensure_ascii=False),
    }


def _compact(table) -> None:
    """Purge retained versions after every write. Lance keeps time-travel
    history by default — without this, deleted rows stay readable via
    `checkout()` and the LFS artifact ships its own edit history (the shipped
    index once carried 11 versions incl. deleted ghost rows)."""
    from datetime import timedelta

    table.optimize(cleanup_older_than=timedelta(0), delete_unverified=True)


def build_index() -> dict:
    chunks = load_chunks()
    embedder = GeminiEmbedder(gemini_api_key())
    texts = [embed_text(c) for c in chunks]
    print(f"[index] embedding {len(texts)} chunks …", flush=True)
    batch = embedder.embed(texts, task_type="RETRIEVAL_DOCUMENT")
    print(f"[index] embedded: ~{batch.total_tokens} tokens, ${batch.cost_usd:.4f}", flush=True)

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    db = lancedb.connect(INDEX_DIR)
    rows = [_row(c, v) for c, v in zip(chunks, batch.vectors)]
    db.drop_table(TABLE, ignore_missing=True)
    table = db.create_table(TABLE, rows)
    table.create_fts_index("text", replace=True)
    _compact(table)
    summary = {"rows": len(rows), "embed_cost_usd": round(batch.cost_usd, 4)}
    print(f"[index] done: {summary}", flush=True)
    return summary


def update_laws(slugs: list[str]) -> dict:
    """Incrementally re-embed only the given laws: delete their existing rows
    and insert freshly-embedded ones. The refresh primitive (ingestion_pipeline
    §7) so an amendment to one law does not re-embed the whole corpus.

    Pass EVERY law_slug whose rows changed — including `ref/…` slugs of newly
    materialized bylaws (a re-ingest of law X can add referenced-act chunks
    that ride their own slugs, which `update_laws(["X"])` alone won't embed).
    A law with no remaining chunks (removed from the corpus) has its stale
    index rows deleted.
    """
    if not slugs:
        return {"laws": 0, "rows": 0, "embed_cost_usd": 0.0}
    targets = set(slugs)
    chunks = [c for c in load_chunks() if c.metadata.get("law_slug") in targets]
    table = open_table()
    quoted = ", ".join("'" + s.replace("'", "''") + "'" for s in targets)
    if not chunks:  # law removed from the corpus → its stale index rows go too
        table.delete(f"law_slug IN ({quoted})")
        table.create_fts_index("text", replace=True)
        summary = {"laws": len(targets), "rows": 0, "embed_cost_usd": 0.0}
        print(f"[index] removed laws with no chunks: {summary}", flush=True)
        return summary
    # embed FIRST, delete after — an embed failure must not leave the law
    # deleted with no replacement rows
    embedder = GeminiEmbedder(gemini_api_key())
    texts = [embed_text(c) for c in chunks]
    batch = embedder.embed(texts, task_type="RETRIEVAL_DOCUMENT")
    table.delete(f"law_slug IN ({quoted})")
    table.add([_row(c, v) for c, v in zip(chunks, batch.vectors)])
    table.create_fts_index("text", replace=True)  # FTS must cover the new rows
    _compact(table)
    summary = {"laws": len(targets), "rows": len(chunks), "embed_cost_usd": round(batch.cost_usd, 4)}
    print(f"[index] updated laws: {summary}", flush=True)
    return summary


def open_table():
    return lancedb.connect(INDEX_DIR).open_table(TABLE)


if __name__ == "__main__":
    sys.exit(0 if build_index()["rows"] > 0 else 1)
