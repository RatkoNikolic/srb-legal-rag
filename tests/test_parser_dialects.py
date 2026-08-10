"""Parser contract for the two non-default PIS markup dialects, locked
against real fixtures (fetched 2026-06-11):

- dialect B (young-law exports, stav bodies = Basic-Paragraph):
  Zakon o digitalnoj imovini, Sl. glasnik RS 153/2020.
- dialect C (legacy classless exports, auto-style* + nevazeciakt):
  ZZPL 2008 — the whole-law-repealed predecessor, Sl. glasnik RS 97/2008…87/2018.
"""

from datetime import date
from pathlib import Path

import pytest

from lexsr import parse_consolidated_html, to_chunks

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def zdi():
    html = (FIXTURES / "zakon_o_digitalnoj_imovini_consolidated.html").read_text(encoding="utf-8")
    return parse_consolidated_html(html)


@pytest.fixture(scope="module")
def zzpl08():
    html = (FIXTURES / "zzpl_2008_consolidated.html").read_text(encoding="utf-8")
    return parse_consolidated_html(html)


# --- dialect B: Zakon o digitalnoj imovini -------------------------------


def test_zdi_title_and_chain(zdi):
    assert zdi.title == "ЗАКОН о дигиталној имовини"
    assert [g.issue for g in zdi.gazette_chain] == ["153/2020"]
    assert zdi.gazette_chain[0].published == date(2020, 12, 21)
    assert not zdi.repealed


def test_zdi_article_coverage(zdi):
    assert sorted(int(c.number) for c in zdi.main_clanovi) == list(range(1, 147))
    # a young base law: nothing transitional, nothing repealed, nothing empty
    assert len(zdi.clanovi) == len(zdi.main_clanovi)
    assert [c.number for c in zdi.main_clanovi if not c.stavovi] == []


def test_zdi_basic_paragraph_stavovi_and_italik_subsections(zdi):
    c1 = zdi.main_clanovi[0]
    assert len(c1.stavovi) == 6
    assert c1.stavovi[0].text == "Овим законом уређују се:"
    assert c1.subsection == "Предмет уређивања"  # italik per-article title
    assert c1.chapter is not None and c1.chapter.endswith("СНОВНЕ ОДРЕДБЕ")


def test_zdi_entry_into_force(zdi):
    # čl. 146: „…ступа на снагу осмог дана од објављивања…" → publication
    # (2020-12-21) + 8 days. (Its „…а примењује се по истеку шест месеци…"
    # tail is start-of-application, not entry into force — not mistaken for it.)
    assert zdi.valid_from == date(2020, 12, 29)


def test_zdi_chunks_alive(zdi):
    chunks = to_chunks(zdi, source_id="sgrs/zakon/2020/153")
    assert len(chunks) == 146
    # single-issue young law, no amendments → every article shares the law's
    # entry into force (8th day after publication).
    assert all(c.provenance.valid_from == date(2020, 12, 29) for c in chunks)
    assert all(c.provenance.valid_to is None for c in chunks)


# --- dialect C: ZZPL 2008, the whole-law-repealed predecessor -------------


def test_zzpl08_title_without_semantic_classes(zzpl08):
    assert zzpl08.title == "ЗАКОН о заштити података о личности"


def test_zzpl08_gazette_chain_from_classless_header(zzpl08):
    assert [(g.issue, g.change_type) for g in zzpl08.gazette_chain] == [
        ("97/2008", None),
        ("104/2009", "др. закон"),
        ("68/2012", "УС"),
        ("107/2012", None),
        ("87/2018", "др. закон"),
    ]
    assert zzpl08.gazette_chain[0].published == date(2008, 10, 27)


def test_zzpl08_law_level_repeal_from_publisher_note(zzpl08):
    # "НАПОМЕНА ИЗДАВАЧА: Престаје да важи ... 21. августа 2019. године"
    assert zzpl08.repealed
    assert zzpl08.valid_to == date(2019, 8, 21)
    assert zzpl08.repealed_by == "87/2018"


def test_zzpl08_articles_via_text_regex_only(zzpl08):
    nums = [c.number for c in zzpl08.main_clanovi]
    assert len(nums) == 64  # čl. 1–63 + inserted 12а
    assert "12а" in nums
    assert [c.number for c in zzpl08.main_clanovi if not c.stavovi] == []


def test_zzpl08_shape_detected_chapters_and_subsections(zzpl08):
    by_number = {c.number: c for c in zzpl08.main_clanovi}
    assert by_number["1"].chapter == "I. ОСНОВНЕ ОДРЕДБЕ"
    assert by_number["8"].chapter == "II. УСЛОВИ ЗА ОБРАДУ"
    assert by_number["3"].subsection == "Значење израза у овом закону"


def test_zzpl08_chunks_inherit_law_level_valid_to(zzpl08):
    chunks = to_chunks(zzpl08, source_id="sgrs/zakon/2008/97")
    assert len(chunks) == 64
    assert all(c.provenance.valid_to == date(2019, 8, 21) for c in chunks)
    assert all(c.metadata["repealed"] for c in chunks)
    assert all(c.metadata["law_repealed_by"] == "87/2018" for c in chunks)


# --- dialect C, legacy variants that lost article bodies (Q10 regression) ---
# Both come from PIS exports where the class carries no semantics; the bug was
# article bodies being dropped (1095/1097 in ZOO, 14/22 in ZPN). Minimal real
# slices (first 3 articles) lock the fix.


def test_zoo_msonormal_bodies_are_captured():
    # ZOO 1978: every <p> is class="MsoNormal" (legacy Word export) — bodies
    # must be captured as stavovi, not dropped for lacking a body class.
    html = (FIXTURES / "zoo_msonormal_consolidated.html").read_text(encoding="utf-8")
    law = parse_consolidated_html(html)
    assert law.title == "ЗАКОН о облигационим односима"
    assert len(law.main_clanovi) == 3
    assert all(c.stavovi for c in law.main_clanovi)  # no empty bodies
    assert law.main_clanovi[0].stavovi[0].text.startswith("Овим законом уређују се")


def test_zpn_autostyle1_bodies_not_swallowed_as_headings():
    # Zakon o prometu nepokretnosti: bodies carry `auto-style1`, the same class
    # that marks *headings* elsewhere — they must not be consumed as chapters.
    html = (FIXTURES / "zpn_autostyle_consolidated.html").read_text(encoding="utf-8")
    law = parse_consolidated_html(html)
    assert len(law.main_clanovi) == 3
    assert all(c.stavovi for c in law.main_clanovi)
    assert "Промет непокретности је слободан" in law.main_clanovi[0].stavovi[0].text
