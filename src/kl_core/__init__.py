"""kl-core — a knowledge-layer reference implementation.

Four seams, one contract each:
- `kl_core.ingest`     — corpus → chunks with provenance (+ temporal validity)
- `kl_core.retrieval`  — the architectures under comparison (naive / advanced / KG / agentic)
- `kl_core.kg`         — typed-KG construction + the schema-evolution control point
- `kl_core.evals`      — task-grounded scoring across all of the above

A domain skin (here, Serbian statutory law) plugs into these seams; kl-core
itself stays domain-free, so a second domain reuses it unchanged.
"""

from kl_core.types import Answer, Chunk, Provenance, Query, RetrievalResult

__all__ = ["Answer", "Chunk", "Provenance", "Query", "RetrievalResult"]
__version__ = "0.0.1"
