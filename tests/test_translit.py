"""Transliteration contract: query-side Gajica → Ćirilica."""

from lexsr.translit import to_cyrillic


def test_to_cyrillic_mapping_contract():
    """One mapping, every case it has to get right.

    Digraphs must beat their letter-by-letter reading (lj→љ, not л+ј) in both
    cases, including all-caps. Non-Serbian letters, digits, punctuation and
    already-Cyrillic text pass through untouched, so the function is safe to
    apply to any query. `dj` is deliberately NOT mapped to ђ: the strict
    mapping keeps "odjava" as одјава rather than turning it into *ођава.
    """
    cases = [
        # (input, expected, what it pins)
        ("Koliki su porezi za paušalca?", "Колики су порези за паушалца?", "basic sentence"),
        ("ljubav", "љубав", "digraph lj"),
        ("njegov", "његов", "digraph nj"),
        ("džak", "џак", "digraph dž"),
        ("Ljiljana Njegoš Džordan", "Љиљана Његош Џордан", "title-case digraphs"),
        ("NJUJORK", "ЊУЈОРК", "all-caps digraph"),
        ("čćžšđ ČĆŽŠĐ", "чћжшђ ЧЋЖШЂ", "diacritics, both cases"),
        (
            "Колико износи отпремнина при одласку у пензију?",
            "Колико износи отпремнина при одласку у пензију?",
            "cyrillic passthrough",
        ),
        (
            "član 152 (75/2014) — q&A x y w",
            "члан 152 (75/2014) — q&А x y w",
            "mixed / non-Serbian chars",
        ),
        ("odjava", "одјава", "informal dj is NOT ђ"),
    ]
    for text, expected, pins in cases:
        assert to_cyrillic(text) == expected, pins
