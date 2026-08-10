"""Tier-1 chunk-metadata builder contract (knowledge_base.md §5)."""

from datetime import date

from lexsr.metadata import (
    build_chunk_metadata,
    chunk_status,
    classify_article,
    contextual_prefix,
    structural_path,
)


def test_classify_definitions():
    assert classify_article("Значење израза", "У овом закону, израз...")["is_definitions"]
    assert classify_article("Појмови", "")["is_definitions"]
    assert not classify_article("Предмет", "Овим законом се уређује...")["is_definitions"]
    # the reverse word order — the dominant lead of a definitions article, and
    # the form the forward-only regex used to miss (izvor: planiranje-izgradnja,
    # bezbednost-saobracaja, doprinosi, …):
    assert classify_article(
        "", "Поједини изрази употребљени у овом закону имају следеће значење: 1) ..."
    )["is_definitions"]
    assert classify_article(
        "", "Поједини појмови употребљени у овом правилнику имају следеће значење:"
    )["is_definitions"]
    assert classify_article(
        "", "Термини који се користе у овом закону имају следеће значење:"
    )["is_definitions"]
    # a body that merely uses the word „израз" without a definitions lead must not trip:
    assert not classify_article(
        "Отказ", "Послодавац изразом отказа престаје радни однос запосленог."
    )["is_definitions"]


def test_classify_penalty():
    assert classify_article("Казнене одредбе", "Новчаном казном...")["is_penalty"]
    assert classify_article("", "Новчаном казном од 100.000 динара казниће се...")["is_penalty"]
    assert not classify_article("Предмет", "Уређује се...")["is_penalty"]


def test_classify_bylaw_delegation():
    assert classify_article("", "Ближе услове прописује министар.")["delegates_to_bylaw"]
    assert classify_article("", "Влада доноси подзаконски акт.")["delegates_to_bylaw"]
    assert not classify_article("", "Запослени има право на одмор.")["delegates_to_bylaw"]


def test_chunk_status():
    assert chunk_status(repealed=False, law_repealed=False) == "in_force"
    assert chunk_status(repealed=True, law_repealed=False) == "repealed"
    assert chunk_status(repealed=False, law_repealed=True) == "repealed"


def test_structural_path_breadcrumb():
    assert structural_path("II. ЗАРАДА", "1. Минимална зарада") == "II. ЗАРАДА › 1. Минимална зарада"
    assert structural_path("II. ЗАРАДА", None) == "II. ЗАРАДА"
    assert structural_path(None, None) == ""


def test_contextual_prefix_is_discriminative():
    # leads with the article + heading breadcrumb (unique), not the law title
    p = contextual_prefix(
        fragment="član 118",
        law_title="ЗАКОН о раду",
        path="II. ЗАРАДА › Минимална зарада",
        valid_from="2014-07-29",
    )
    assert p.startswith("član 118 · II. ЗАРАДА")
    assert "Закон о раду" in p
    assert "на снази од 2014-07-29" in p


def test_build_chunk_metadata_assembles_all_groups():
    meta = build_chunk_metadata(
        chapter="II. ЗАРАДА",
        subsection="Минимална зарада",
        heading="Минимална зарада",
        text="Новчаном казном казниће се послодавац...",  # penalty wording
        law_title="ЗАКОН о раду",
        fragment="član 118",
        ordinal=117,
        prev_clan="117",
        next_clan="119",
        stav_count=3,
        last_changed_by="75/2014",
        published=date(2005, 3, 15),
        applies_from=date(2026, 1, 1),
        valid_from=date(2014, 7, 29),
        repealed=False,
        law_repealed=False,
        transitional=False,
    )
    assert meta["structural_path"] == "II. ЗАРАДА › Минимална зарада"
    assert meta["prev_clan"] == "117" and meta["next_clan"] == "119"
    assert meta["ordinal"] == 117 and meta["stav_count"] == 3
    assert meta["published"] == "2005-03-15"
    assert meta["applies_from"] == "2026-01-01"
    assert meta["status"] == "in_force"
    assert meta["is_penalty"] is True
    assert "law_slug" not in meta  # stamped by the corpus builder, not here
