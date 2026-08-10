"""Gold-answer grounding: a gold answer must be lexically supported by
the verbatim text it cites, not merely point at a non-empty chunk."""

import pytest

from lexsr import bench

ARTICLE = (
    "Послодавац је дужан да најмање осам дана пре почетка рада обавести надлежну "
    "инспекцију рада о почетку свог рада."
)
CHUNK_ID = "src/x/clan-20"


@pytest.fixture(autouse=True)
def _corpus(monkeypatch):
    monkeypatch.setattr(bench, "_nonempty_cache", {CHUNK_ID})
    monkeypatch.setattr(bench, "_chunk_text_cache", {CHUNK_ID: ARTICLE})


def _item(gold):
    return {
        "item_id": "e01", "category": "easy",
        "query": {"text": "Колико дана раније?", "filters": {}},
        "gold_answer": gold,
        "gold_citations": ["src/x::član 20"],
    }


def test_overlap_high_for_verbatim_answer():
    grounded = "Најмање осам дана пре почетка рада обавестити инспекцију рада."
    assert bench.grounding_overlap(grounded, [ARTICLE]) >= bench.GROUNDING_MIN_OVERLAP


def test_overlap_low_for_fabricated_answer():
    fabricated = "Минимални износ казне износи педесет хиљада динара годишње."
    assert bench.grounding_overlap(fabricated, [ARTICLE]) < bench.GROUNDING_MIN_OVERLAP


def test_validate_accepts_grounded_and_rejects_ungrounded():
    grounded = "Послодавац обавести инспекцију рада осам дана пре почетка рада."
    assert bench.validate_item(_item(grounded)) == []

    fabricated = "Износ пореза одређује општински орган према тарифи из правилника."
    problems = bench.validate_item(_item(fabricated))
    assert any("not grounded" in p for p in problems)


def test_grounding_skipped_when_text_unavailable(monkeypatch):
    # id resolves to a non-empty chunk but its text isn't loaded → can't ground → no problem
    monkeypatch.setattr(bench, "_chunk_text_cache", {})
    assert bench.validate_item(_item("ungrounded but untestable")) == []
