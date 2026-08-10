"""Export the document embeddings engine-independently (vector-store
reassessment §6.5): a parquet with per-row provenance so a future store
migration never re-pays the embedding bill — and can *verify* the vectors
still match the corpus before reusing them.

Columns: chunk_id · vector (3072-d float32) · embedded_text_sha256 (hash of
the exact `embed_text()` string the vector was computed from — recomputed from
chunks.jsonl, which is only valid while index ≡ corpus; the smoke coherence
check guards that) · model · task_type · dims.

Usage: .venv/bin/python scripts/export_embeddings.py [out.parquet]
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kl_core.providers.gemini import EMBED_MODEL  # noqa: E402
from lexsr.index import embed_text, load_chunks, open_table  # noqa: E402

TASK_TYPE = "RETRIEVAL_DOCUMENT"


def main() -> int:
    # default under data/raw/ (gitignored): the export is insurance, not a
    # shipped artifact — committing a second copy of the vectors would double
    # the LFS payload (vector_store_reassessment.md §5)
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/raw/embeddings-export.parquet")
    out.parent.mkdir(parents=True, exist_ok=True)
    chunks = {c.chunk_id: c for c in load_chunks()}
    model = EMBED_MODEL  # the embedding snapshot the index was built with

    table = open_table()
    rows = table.search().select(["chunk_id", "vector"]).limit(len(chunks) + 1000).to_list()
    missing = [r["chunk_id"] for r in rows if r["chunk_id"] not in chunks]
    if missing or len(rows) != len(chunks):
        print(f"[export] REFUSING: index ≠ corpus (rows={len(rows)}, chunks={len(chunks)}, "
              f"index-only={missing[:3]}…) — rebuild/repair first", flush=True)
        return 1

    ids, vectors, hashes = [], [], []
    for r in rows:
        c = chunks[r["chunk_id"]]
        ids.append(c.chunk_id)
        vectors.append(r["vector"])
        hashes.append(hashlib.sha256(embed_text(c).encode("utf-8")).hexdigest())
    dims = len(vectors[0])
    pq.write_table(
        pa.table({
            "chunk_id": ids,
            "vector": pa.array(vectors, type=pa.list_(pa.float32(), dims)),
            "embedded_text_sha256": hashes,
            "model": pa.array([model] * len(ids)),
            "task_type": pa.array([TASK_TYPE] * len(ids)),
            "dims": pa.array([dims] * len(ids), type=pa.int32()),
        }),
        out,
        compression="zstd",
    )
    print(f"[export] {len(ids)} vectors ({dims}-d, {model}) → {out} "
          f"({out.stat().st_size / 1e6:.0f} MB)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
