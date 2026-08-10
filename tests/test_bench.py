"""Benchmark authoring contract: citation→chunk resolution, grounded
validation against the corpus, golden CRUD round-trips, and multi-set
discovery/scoping on temp files."""

import json

import pytest

from lexsr import bench


@pytest.fixture(autouse=True)
def _tmp_golden(tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "CURRENT_GOLDEN", tmp_path / "golden.jsonl")
    # a small fake "non-empty chunk" universe so validation is corpus-independent
    monkeypatch.setattr(bench, "_nonempty_cache", {
        "sgrs/skupstina/zakon/2005/24/1/clan-69",
        "sgrs/skupstina/zakon/2005/24/1/clan-72",
    })
    # id-only fixtures carry no chunk *text*, so the grounding check has
    # nothing to read and is skipped (it's exercised separately in test_grounding).
    monkeypatch.setattr(bench, "_chunk_text_cache", {})


def _item(item_id="", category="easy", text="Питање?", cites=None):
    return {
        "item_id": item_id,
        "category": category,
        "query": {"text": text, "filters": {"law_slug": "zakon-o-radu"}},
        "gold_answer": "Одговор.",
        "gold_citations": cites if cites is not None else ["sgrs/skupstina/zakon/2005/24/1::član 69"],
    }


def test_citation_parsing_handles_cyrillic_and_latin_fragment():
    assert (bench.citation_to_chunk_id("src/x::član 69") == "src/x/clan-69")
    assert (bench.citation_to_chunk_id("src/x::члан 12а") == "src/x/clan-12а")
    assert (bench.citation_to_chunk_id("src/x::69") == "src/x/clan-69")
    assert bench.citation_to_chunk_id("garbage") is None


def test_validate_rejects_ungrounded_citation():
    assert bench.validate_item(_item()) == []
    bad = _item(cites=["sgrs/skupstina/zakon/2005/24/1::član 9999"])
    assert any("non-empty corpus chunk" in p for p in bench.validate_item(bad))
    assert "empty question text" in bench.validate_item(_item(text="  "))
    assert any("category" in p for p in bench.validate_item(_item(category="trivia")))


def test_upsert_assigns_next_id_per_tier():
    a = bench.upsert_item(_item(category="easy"))
    b = bench.upsert_item(_item(category="easy"))
    assert a["item_id"] == "e01" and b["item_id"] == "e02"
    h = bench.upsert_item(_item(category="hard"))
    assert h["item_id"] == "h01"


def test_upsert_updates_in_place_and_delete():
    created = bench.upsert_item(_item(text="прво"))
    iid = created["item_id"]
    bench.upsert_item({**_item(item_id=iid), "query": {"text": "измењено",
                       "filters": {"law_slug": "zakon-o-radu"}}})
    items = bench.load_items()
    assert len(items) == 1 and items[0]["query"]["text"] == "измењено"
    assert bench.delete_item(iid) is True
    assert bench.load_items() == []
    assert bench.delete_item("nope") is False


def test_upsert_rejects_invalid():
    with pytest.raises(ValueError):
        bench.upsert_item(_item(cites=["sgrs/skupstina/zakon/2005/24/1::član 9999"]))


# ── multi-set discovery + scoping ──────────────────────────────────────────
def test_set_path_contains_names_and_rejects_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "GOLDENS_DIR", tmp_path)
    assert bench.set_path("smoke") == (tmp_path / "smoke.jsonl").resolve()
    assert bench.set_path("smoke.jsonl") == (tmp_path / "smoke.jsonl").resolve()
    assert bench.set_path(None) == bench.CURRENT_GOLDEN  # None → live default
    # traversal attempts collapse to a bare filename inside goldens/ (contained)
    assert bench.set_path("../../etc/passwd").parent == tmp_path.resolve()
    for bad in ("", ".", "..", ".hidden"):
        with pytest.raises(ValueError):
            bench.set_path(bad)


def test_golden_sets_lists_files_and_flags_default(tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "GOLDENS_DIR", tmp_path)
    monkeypatch.setattr(bench, "CURRENT_GOLDEN", tmp_path / "main.jsonl")
    (tmp_path / "main.jsonl").write_text(json.dumps({"item_id": "e01"}) + "\n", encoding="utf-8")
    (tmp_path / "smoke.jsonl").write_text(
        json.dumps({"item_id": "e01"}) + "\n" + json.dumps({"item_id": "e02"}) + "\n",
        encoding="utf-8",
    )
    sets = {s["name"]: s for s in bench.golden_sets()}
    assert sets["main"]["is_default"] and sets["main"]["items"] == 1
    assert not sets["smoke"]["is_default"] and sets["smoke"]["items"] == 2


def test_crud_is_scoped_per_set(tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "GOLDENS_DIR", tmp_path)
    created = bench.upsert_item(_item(category="easy"), golden="setA")
    assert created["item_id"] == "e01"
    assert len(bench.load_items("setA")) == 1
    assert bench.load_items("setB") == []  # a different set is untouched
    assert (tmp_path / "setA.jsonl").exists()
    assert bench.delete_item("e01", golden="setA") is True
    assert bench.load_items("setA") == []
