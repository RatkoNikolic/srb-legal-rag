"""CC strike-down target extraction + the closure-metric split."""

from __future__ import annotations

from datetime import date

from lexsr.bench import citation_to_chunk_id
from lexsr.ingest import closure_metric
from lexsr.parser import cc_decision_date, cc_struck_targets


def test_citation_to_chunk_id_handles_cc_holding_and_article():
    # a CC decision is cited by its holding fragment (izreka) → /holding chunk
    assert citation_to_chunk_id("ref/893049::izreka") == "ref/893049/holding"
    # ordinary article citation still maps to the člának chunk
    assert citation_to_chunk_id("sgrs/skupstina/zakon/2005/24/1::član 179") == \
        "sgrs/skupstina/zakon/2005/24/1/clan-179"
    # a version-PINNED citation resolves to that slice's chunk, which is what
    # makes a temporal gold addressable at all: unpinned, every slice of an
    # article collapses to one key.
    assert citation_to_chunk_id("sgsrs/skupstina/zakon/1982/25/1::član 40@421995") == \
        "sgsrs/skupstina/zakon/1982/25/1/clan-40@421995"


def test_cc_struck_targets_extracts_article_and_law():
    izreka = (
        "на основу члана 167. став 1. тачка 1. Устава Републике Србије, "
        "на седници одржаној 17. новембра 2016. године, донео је ОДЛУКУ "
        "Утврђује се да одредба члана 179. став 3. тачка 5) Закона о раду "
        "није у сагласности са Уставом."
    )
    targets = cc_struck_targets(izreka)
    assert targets, "should extract the struck target"
    law, arts = targets[0]
    assert "раду" in law  # the struck law (not Ustav, the jurisdictional cite)
    nums = [a for a, _ in arts]
    assert nums == ["179"]  # only the ARTICLE — став/тачка digits are NOT articles (regression)
    art, part = arts[0]  # partial strike: član 179 st.3 t.5 only → article stays in force, noted
    assert art == "179" and part and "став 3" in part and "тачка 5" in part


def test_cc_struck_targets_whole_article_strike():
    # no став/тачка qualifier ⇒ the whole article is struck (part is None)
    izreka = "Утврђује се да члан 28а Закона о раду није у сагласности са Уставом."
    _law, arts = cc_struck_targets(izreka)[0]
    assert arts == [("28а", None)]


def test_cc_struck_targets_excludes_ustav_jurisdiction_cite():
    # only the court's jurisdiction is cited, no non-conformity ⇒ no target
    only_juris = "на основу члана 167. став 1. тачка 1. Устава Републике Србије донео је"
    assert cc_struck_targets(only_juris) == []


def test_cc_struck_targets_requires_nonconformity():
    # mentions an article+law but no strike clause ⇒ nothing
    txt = "у вези са чланом 5. Закона о раду суд констатује"
    assert cc_struck_targets(txt) == []


def test_cc_decision_date():
    assert cc_decision_date("на седници одржаној 25. априла 2019. године") == date(2019, 4, 25)
    assert cc_decision_date("нема датума овде") is None


def _edge(edge, in_corpus):
    return {"edge": edge, "in_corpus": in_corpus, "from": "a", "to": "b"}


def test_closure_metric_split():
    edges = [
        _edge("PRAVNI_OSNOV_ZA", False),   # bylaw — never seed-to-seed
        _edge("USTAVNI_OSNOV", True),       # → Ustav (in-corpus hub)
        _edge("POVEZAN_SA", False),         # tangential
        _edge("POVEZAN_SA", True),          # tangential but in-corpus
        _edge("OCENA_USTAVNOSTI", False),
    ]
    m = closure_metric(edges)
    assert m["reference_edges"] == 5
    assert m["in_corpus_edges"] == 2  # the two in_corpus=True
    assert m["closure_pct"] == m["seed_to_seed_pct"] == 40.0
    # answer-relevant drops the two POVEZAN_SA → 3 edges, 1 in-corpus
    assert m["answer_relevant_edges"] == 3
    assert m["answer_relevant_in_corpus_pct"] == round(100 / 3, 1)
    assert m["by_edge_type"]["PRAVNI_OSNOV_ZA"] == {"total": 1, "in_corpus": 0, "pct": 0.0}
    assert m["by_edge_type"]["POVEZAN_SA"]["total"] == 2
