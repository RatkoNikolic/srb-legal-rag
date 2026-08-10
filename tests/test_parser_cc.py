"""Parser additions for the L0–L3 rebuild: Constitutional-Court holding/outcome
extraction (Fork A) and `tabela` (bylaw tables) body capture (§18)."""

from lexsr.parser import (
    CC_STRUCK,
    CC_UPHELD,
    CC_UNKNOWN,
    cc_outcome,
    extract_cc_holding,
    parse_consolidated_html,
)


def test_cc_outcome_struck():
    assert cc_outcome("Утврђује се да одредба члана 5. није у сагласности са Уставом.") == CC_STRUCK
    assert cc_outcome("Одредбе престају да важе даном објављивања.") == CC_STRUCK


def test_cc_outcome_upheld():
    assert cc_outcome("Одбија се иницијатива за оцену уставности.") == CC_UPHELD
    assert cc_outcome("Не прихвата се иницијатива.") == CC_UPHELD


def test_cc_outcome_unknown():
    assert cc_outcome("Уставни суд је на седници размотрио предмет.") == CC_UNKNOWN


def test_extract_cc_holding():
    html = (
        '<p>Преузето са https://pravno-informacioni-sistem.rs</p>'
        '<p>УСТАВНИ СУД</p>'
        '<p>Утврђује се да одредба члана 5. није у сагласности са Уставом.</p>'
    )
    outcome, holding = extract_cc_holding(html)
    assert outcome == CC_STRUCK
    assert "Уставни" in holding or "Утврђује" in holding
    assert "Преузето са" not in holding  # boilerplate stripped


def test_tabela_body_is_captured():
    # bylaws carry tabular content in <p class="tabela"> — it must become article
    # body text (pre-flight finding), not be dropped.
    html = (
        '<p class="odluka-zakon">ПРАВИЛНИК</p>'
        '<p class="clan">Члан 1.</p>'
        '<p class="tabela">Ред 1 — 100 динара</p>'
        '<p class="Basic-Paragraph">Текст одредбе.</p>'
    )
    law = parse_consolidated_html(html)
    assert len(law.main_clanovi) == 1
    body = "\n".join(s.text for s in law.main_clanovi[0].stavovi)
    assert "100 динара" in body and "Текст одредбе" in body
