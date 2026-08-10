"""Parser contract, locked against the real PIS consolidated text of
Zakon o radu (fetched 2026-06-10, fixture in tests/fixtures/)."""

import re
from datetime import date
from pathlib import Path

import pytest

from lexsr import parse_consolidated_html, to_chunks
from lexsr.parser import entry_into_force

FIXTURE = Path(__file__).parent / "fixtures" / "zakon_o_radu_consolidated.html"
ELI = "https://pravno-informacioni-sistem.rs/eli/rep/sgrs/skupstina/zakon/2005/24/1/reg"


@pytest.fixture(scope="module")
def law():
    return parse_consolidated_html(FIXTURE.read_text(encoding="utf-8"), eli=ELI)


def test_title(law):
    assert law.title == "ЗАКОН о раду"


def test_gazette_chain_full_with_dates_and_change_types(law):
    issues = [g.issue for g in law.gazette_chain]
    assert issues == [
        "24/2005", "61/2005", "54/2009", "32/2013",
        "75/2014", "13/2017", "113/2017", "95/2018",
    ]
    assert law.gazette_chain[0].published == date(2005, 3, 15)
    assert law.gazette_chain[5].change_type == "УС"
    assert law.gazette_chain[7].change_type == "Аутентично тумачење"


def test_article_coverage_complete(law):
    nums = {int(re.match(r"(\d+)", c.number).group(1)) for c in law.main_clanovi}
    assert max(nums) == 287
    assert sorted(set(range(1, 288)) - nums) == []  # no gaps
    assert len(law.main_clanovi) == 298  # 287 base + letter-suffixed inserts


def test_transitional_articles_segmented(law):
    assert len(law.clanovi) - len(law.main_clanovi) == 13


def test_clan_1_structure(law):
    c1 = law.main_clanovi[0]
    assert c1.number == "1"
    assert len(c1.stavovi) == 2
    assert c1.chapter == "I. ОСНОВНЕ ОДРЕДБЕ"
    assert c1.subsection == "1. Предмет"
    assert c1.stavovi[0].text.startswith("Права, обавезе и одговорности")


def test_repealed_articles_detected_with_repealing_gazette(law):
    repealed = {c.number: c.repealed_by for c in law.main_clanovi if c.repealed_by}
    assert len(repealed) == 11
    assert repealed["152"] == "75/2014"
    assert repealed["190"] == "61/2005"  # repealed by a different gazette than the rest
    # every non-repealed article has body text
    assert [c.number for c in law.main_clanovi if not c.stavovi and not c.repealed_by] == []


def test_provision_level_change_notes(law):
    changed = [s for c in law.main_clanovi for s in c.stavovi if s.changed_by]
    assert len(changed) == 115
    assert all(re.fullmatch(r"\d+/\d{4}", s.changed_by) for s in changed)


@pytest.mark.parametrize(
    "clause, expected",
    [
        # most common: 8th day after publication
        ("Овај закон ступа на снагу осмог дана од дана објављивања", date(2024, 1, 9)),
        # next day
        ("Овај закон ступа на снагу наредног дана од дана објављивања", date(2024, 1, 2)),
        # same day as publication
        ("Овај закон ступа на снагу даном објављивања", date(2024, 1, 1)),
        # a named absolute date wins over the offset words
        ("Овај закон ступа на снагу 15. марта 2024. године", date(2024, 3, 15)),
        # the trap: a trailing start-of-application date is NOT entry into force
        (
            "Овај закон ступа на снагу осмог дана од дана објављивања, а "
            "примењује се од 1. јануара 2025. године",
            date(2024, 1, 9),
        ),
        # no recognizable clause → constitutional +8-day default
        ("Прелазне и завршне одредбе нечитке.", date(2024, 1, 9)),
    ],
)
def test_entry_into_force_clause_resolution(clause, expected):
    assert entry_into_force(clause, date(2024, 1, 1)) == expected


def test_entry_into_force_base_law(law):
    # Q8: the law enters into force on the 8th day after publication
    # (čl. 287: „…ступа на снагу осмог дана од дана објављивања…"),
    # not on the publication date (2005-03-15) itself.
    assert law.gazette_chain[0].published == date(2005, 3, 15)
    assert law.valid_from == date(2005, 3, 23)


def test_to_chunks_provenance_and_temporal_validity(law):
    chunks = to_chunks(law, source_id="sgrs/zakon/2005/24", applies_from=date(2025, 12, 31))
    assert len(chunks) == 298
    by_fragment = {c.provenance.fragment: c for c in chunks}
    # live, never-amended article: valid_from = the law's entry into force
    # (8th day after the base gazette), no valid_to.
    alive = by_fragment["član 1"]
    assert alive.provenance.version is None  # unchanged
    assert alive.provenance.valid_from == date(2005, 3, 23)
    assert alive.provenance.valid_to is None
    assert alive.provenance.url == ELI
    assert alive.metadata["applies_from"] == "2025-12-31"
    # amended article: valid_from = entry into force of its last amendment
    # (75/2014 published 2014-07-21 → in force 8 days later).
    amended = by_fragment["član 118"]
    assert amended.provenance.version == "75/2014"
    assert amended.provenance.valid_from == date(2014, 7, 29)
    # repealed article: valid_to resolved to the repealing gazette's date
    dead = by_fragment["član 152"]
    assert dead.metadata["repealed"] is True
    assert dead.provenance.valid_to == date(2014, 7, 21)
