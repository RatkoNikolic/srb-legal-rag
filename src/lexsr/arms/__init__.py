"""Retrieval arms — the benchmark treatments. Same corpus, same generator,
same instrumentation; only the retrieval strategy differs."""

from lexsr.arms.advanced import AdvancedArm
from lexsr.arms.agentic import AgenticArm
from lexsr.arms.base import ArmResult, RetrievalArm
from lexsr.arms.graph import GraphArm
from lexsr.arms.naive import NaiveArm
from lexsr.arms.stuffing import StuffingArm

ARMS: dict[str, type[RetrievalArm]] = {
    "context-stuffing": StuffingArm,
    "naive": NaiveArm,
    "advanced": AdvancedArm,
    "graph": GraphArm,
    "agentic": AgenticArm,
}

__all__ = [
    "ARMS", "ArmResult", "RetrievalArm",
    "StuffingArm", "NaiveArm", "AdvancedArm", "GraphArm", "AgenticArm",
]
