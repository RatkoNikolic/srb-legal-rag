"""temporal_mode distinguishes citing-as-authority from reporting-repeal."""

from __future__ import annotations

from datetime import date

from kl_core.evals import TaskItem, score_item
from kl_core.types import Answer, Provenance, Query


def _item(temporal_mode: str) -> TaskItem:
    # gold_answer=None → abstain branch (no judge needed), but the answer is NOT
    # abstained + has an as_of + a citation, so the temporal block still runs.
    return TaskItem(
        item_id="t1",
        query=Query(text="…", as_of=date(2020, 1, 1)),
        category="hard",
        capability="temporal",
        temporal_mode=temporal_mode,
        gold_answer=None,
        gold_citations=[],
    )


def _answer_citing_repealed() -> Answer:
    # an article that was repealed in 2019 — out of force on the 2020 as_of
    prov = Provenance(
        source_id="x", source_title="X", fragment="član 5",
        valid_from=date(2010, 1, 1), valid_to=date(2019, 1, 1), url=None,
    )
    return Answer(text="…", citations=[prov], abstained=False)


def test_in_force_mode_penalizes_out_of_force_citation():
    s = score_item(_item("in_force"), _answer_citing_repealed(), "graph")
    assert s.temporally_correct is False  # cited an out-of-force article as authority


def test_reports_repeal_mode_accepts_out_of_force_citation():
    s = score_item(_item("reports_repeal"), _answer_citing_repealed(), "graph")
    assert s.temporally_correct is True  # correctly surfaced the repealed article
    assert s.capability == "temporal"
