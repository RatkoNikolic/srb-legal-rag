"""Core domain types shared by every kl-core seam.

Two invariants the whole library is built around:

1. Every retrievable unit carries `Provenance` — an answer without a source
   chain is a bug, not a degraded mode.
2. Every unit may carry a temporal validity interval, because corpora amend
   (statutes, developing stories) and "currently correct" is time-indexed.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field


class Provenance(BaseModel):
    """Where a piece of knowledge came from, precisely enough to verify it."""

    source_id: str  # canonical document id (e.g. ELI-style identifier)
    source_title: str
    fragment: str | None = None  # e.g. "član 12, stav 3"
    version: str | None = None  # which consolidated version
    valid_from: date | None = None
    valid_to: date | None = None  # None = still in force / current
    url: str | None = None
    # Delegation provenance: when this unit is a *derived* document issued under a
    # parent authority (a regulation under its enabling statute), the parent is
    # carried so the citation can be rendered as a chain, not an orphan. Domain-
    # neutral — the legal phrasing ("donet na osnovu …") stays in the skin.
    parent_source_id: str | None = None
    parent_title: str | None = None


class Chunk(BaseModel):
    """A retrievable unit of text with provenance and metadata."""

    chunk_id: str
    text: str
    provenance: Provenance
    metadata: dict[str, Any] = Field(default_factory=dict)


class Query(BaseModel):
    """A task query; `as_of` pins temporal questions to a reference date."""

    text: str
    as_of: date | None = None
    filters: dict[str, Any] = Field(default_factory=dict)


class RetrievalResult(BaseModel):
    chunks: list[Chunk]
    backend: str
    latency_ms: float | None = None
    cost_usd: float | None = None


class Answer(BaseModel):
    """A generated answer. `citations` must justify every claim; an empty
    list is only legal when `abstained` is True."""

    text: str
    citations: list[Provenance]
    abstained: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
