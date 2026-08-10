"""Typed-KG construction + the schema-evolution control point.

This package is the research hook of the whole library: when the
construction agent meets an observation that doesn't fit the current
typed schema, the `SchemaPolicy` decides — coerce it into an existing
type, or propose a schema extension under cost. Every decision is logged
so benchmark episodes can score the policy against labelled ground truth.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class SchemaAction(str, Enum):
    COERCE = "coerce"  # map the observation onto an existing type
    EXTEND = "extend"  # propose a new entity/relation type
    DEFER = "defer"  # park for review; do not write


class SchemaDecision(BaseModel):
    """One logged coerce-vs-extend decision — the unit the benchmark scores."""

    observation: dict[str, Any]
    action: SchemaAction
    target_type: str  # existing type coerced into, or proposed new type
    rationale: str
    estimated_cost: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class SchemaPolicy(Protocol):
    """The schema-evolution control policy under study; baselines implement
    trivial variants (always-coerce, always-extend, threshold-based)."""

    name: str

    def decide(self, observation: dict[str, Any], schema: dict[str, Any]) -> SchemaDecision:
        ...
