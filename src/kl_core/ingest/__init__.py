"""Ingestion — pluggable per corpus (Serbian statutes, GDELT news, ...).

An ingestor turns raw source documents into `Chunk`s with full provenance
and, where the domain has them, temporal validity intervals. Domain skins
Domain skins provide concrete ingestors; kl-core owns only the contract.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from kl_core.types import Chunk


@runtime_checkable
class Ingestor(Protocol):
    name: str

    def ingest(self) -> Iterable[Chunk]:
        """Yield chunks with provenance; never yield a chunk without one."""
        ...
