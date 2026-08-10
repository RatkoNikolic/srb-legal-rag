"""KG construction helpers (pure): cross-reference extraction recall, typed
structural edges, and graph-density stats (knowledge_base.md §9)."""

from types import SimpleNamespace

from lexsr.kg import _defined_terms, _extract_refs, graph_stats, structural_edges

# slug -> normalized title (as build_kg derives it: lowercased, "о " dropped)
LAW_TITLES = {
    "zakon-o-pio": "пензијском и инвалидском осигурању",
    "zakon-o-radu": "раду",
}


# Reference-resolution recall is the single behaviour every cross-law edge in
# the KG depends on: a missed reference is a missing edge, and a wrongly
# resolved one is a false edge pointing at the wrong statute.
POREZ_TITLES = {
    "zakon-o-pdv": "Закон о порезу на додату вредност",
    "zakon-o-porezu-na-dohodak": "Закон о порезу на доходак грађана",
    "zakon-o-porezu-na-dobit": "Закон о порезу на добит правних лица",
}


def test_extract_refs_resolution_contract():
    """`_extract_refs` recall and precision, case by case.

    Recall fixes pinned here: a cross-law citation with no trailing gazette
    paren used to be missed entirely; a short title („раду", 4 chars) was
    skipped by a <10-char guard, so Zakon o radu could never be a cross-law
    target; a degenerate manifest title („ЗАКОН") is recovered from the
    slug-derived key; and „Законом о X … члан N" (law *before* the article)
    was missed by a forward-only window.

    Precision: an unknown law yields no edge rather than a guess, and a
    truncated capture that matches several laws equally („порезу на") resolves
    to nothing rather than to an arbitrary one of them. Ranges expand, lists
    do not.
    """
    cases = [
        # (text, titles, assertion-kind, expected, what it pins)
        ("у складу са чланом 18. овог закона", LAW_TITLES,
         "eq", [(None, "18", "чланом 18.")], "internal reference"),
        ("сходно члану 5. Закона о пензијском и инвалидском осигурању („Сл. гласник РС“)",
         LAW_TITLES, "in", ("zakon-o-pio", "5", "члану 5."), "cross-law, gazette paren"),
        ("примењује се члан 5. Закона о пензијском и инвалидском осигурању, став 2",
         LAW_TITLES, "in", ("zakon-o-pio", "5", "члан 5."), "cross-law, no paren"),
        ("види члан 9. Закона о нечем непостојећем (…)", LAW_TITLES,
         "eq", [], "unknown law → no edge"),
        ("сходно члану 5. Закона о раду, став 1", LAW_TITLES,
         "in", ("zakon-o-radu", "5", "члану 5."), "short title resolves"),
        ("према члану 84. Закона о планирању и изградњи.",
         {"zakon-o-planiranju-i-izgradnji": " ЗАКОН"},
         "in", ("zakon-o-planiranju-i-izgradnji", "84", "члану 84."), "bare-ЗАКОН via slug"),
        ("сагласно члану 3. Закона о порезу на, нешто", POREZ_TITLES,
         "eq", [], "ambiguous truncation → no edge"),
        ("члан 3. Закона о порезу на доходак грађана", POREZ_TITLES,
         "in", ("zakon-o-porezu-na-dohodak", "3", "члан 3."), "disambiguated porez law"),
        ("у складу са Законом о раду, а нарочито члан 179.", LAW_TITLES,
         "in", ("zakon-o-radu", "179", "члан 179."), "law name before član"),
    ]
    for text, titles, kind, expected, pins in cases:
        refs = _extract_refs(text, titles)
        if kind == "eq":
            assert refs == expected, pins
        else:
            assert expected in refs, pins

    # ranges expand to every article in them; comma/„и" lists do not
    assert [n for _, n, _ in _extract_refs("сходно чл. 5. до 9. овог закона", LAW_TITLES)] == \
        ["5", "6", "7", "8", "9"], "range expands"
    assert [n for _, n, _ in _extract_refs("чл. 192. и 195. овог закона", LAW_TITLES)] == \
        ["192", "195"], "list does not expand"


def _chunk(slug, num, text="", **meta):
    return SimpleNamespace(
        chunk_id=f"{slug}/clan-{num}", text=text, metadata={"law_slug": slug, **meta}
    )


def test_defined_terms_extraction():
    text = '„адаптација" је промена намене.\nинвеститор је лице које финансира градњу.'
    terms = _defined_terms(text)
    assert "адаптација" in terms  # quoted
    assert "инвеститор" in terms  # leading noun-phrase before „је"


def test_structural_edges_defined_in_is_term_aware():
    # P3: the definitions article defines „адаптација"; only the article that
    # actually uses the term links to it — not the whole law.
    chunks = [
        _chunk("z", "2", text='„адаптација" значи промену намене зграде', is_definitions=True),
        _chunk("z", "5", text="Инвеститор може да изврши адаптацију објекта."),
        _chunk("z", "9", text="Рок за жалбу је осам дана."),
    ]
    defined_in = structural_edges(chunks)["DEFINED_IN"]
    assert ("z/clan-5", "z/clan-2") in defined_in  # uses the term → linked
    assert ("z/clan-9", "z/clan-2") not in defined_in  # doesn't → not linked


def test_structural_edges_defined_in_falls_back_when_no_terms():
    # extraction miss (defs article with no parseable term) → law-global link,
    # so we never regress below the prior recall.
    chunks = [
        _chunk("z", "1", text="неки текст без дефиниција", is_definitions=True),
        _chunk("z", "4", text="било шта"),
    ]
    defined_in = structural_edges(chunks)["DEFINED_IN"]
    assert ("z/clan-4", "z/clan-1") in defined_in


def test_structural_edges_next_and_defined_in():
    chunks = [
        _chunk("zakon-o-radu", "1"),
        _chunk("zakon-o-radu", "5", is_definitions=True),
        _chunk("zakon-o-radu", "6"),
    ]
    edges = structural_edges(chunks)
    # NEXT links consecutive articles within the law
    assert ("zakon-o-radu/clan-1", "zakon-o-radu/clan-5") in edges["NEXT"]
    assert ("zakon-o-radu/clan-5", "zakon-o-radu/clan-6") in edges["NEXT"]
    # every non-definitions article points at the definitions article
    assert ("zakon-o-radu/clan-1", "zakon-o-radu/clan-5") in edges["DEFINED_IN"]
    assert ("zakon-o-radu/clan-6", "zakon-o-radu/clan-5") in edges["DEFINED_IN"]
    # the definitions article doesn't point at itself
    assert ("zakon-o-radu/clan-5", "zakon-o-radu/clan-5") not in edges["DEFINED_IN"]


def test_graph_stats_density():
    # 4 nodes, out-degrees [2,0,0,1] → 3 edges, 2 isolated, 1 cross-law
    s = graph_stats(4, [2, 0, 0, 1], cross_law_edges=1)
    assert s["nodes"] == 4
    assert s["semantic_edges"] == 3
    assert s["mean_out_degree"] == 0.75
    assert s["isolated_nodes"] == 2 and s["isolated_pct"] == 50.0
    assert s["cross_law_edges"] == 1 and s["max_out_degree"] == 2
