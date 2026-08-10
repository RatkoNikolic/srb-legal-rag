"""The ingestion pipeline (lexsr.ingest) — end to end with PIS fully mocked, so
no network. Covers: chunking, typed references, stub-vs-materialized, the
closure metric, CC holding chunks, and Fork-B version slicing."""

import json
from datetime import date

import pytest

from lexsr import index, ingest, kg, pis
from lexsr.pis import ActReference, LawVersion, LawView

ELI = "https://pravno-informacioni-sistem.rs/eli/rep/sgrs/skupstina/zakon/2020/1/1/reg"

_SEED_HTML = (
    '<p class="odluka-zakon">ЗАКОН</p><p class="odluka-zakon">о тесту</p>'
    '<p class="centar">"Службени гласник РС", број 1 од 1. јануара 2020.</p>'
    '<p class="clan">Члан 1.</p><p>Прва одредба о предмету овог закона и његовом домашају.</p>'
    '<p class="clan">Члан 2.</p><p>Друга одредба упућује на члан 1. овог закона и њиме се уређује нешто.</p>'
)
_BYLAW_HTML = (
    '<p class="odluka-zakon">ПРАВИЛНИК</p>'
    '<p class="clan">Члан 1.</p>'
    '<p class="Basic-Paragraph">Ближе се уређује спровођење закона о тесту у пракси, '
    'на основу члана 2. закона, ради потпуне примене његових одредаба у управном поступку.</p>'
)
_CC_HTML = (
    '<p>Преузето са https://pravno-informacioni-sistem.rs</p><p>УСТАВНИ СУД</p>'
    '<p>Уставни суд, на седници одржаној у складу са одредбама Устава и Закона о '
    'Уставном суду, размотрио је иницијативу за оцену уставности и донео следећу одлуку.</p>'
    '<p>Утврђује се да одредба члана 1. није у сагласности са Уставом Републике Србије, '
    'те престаје да важи даном објављивања ове одлуке у Службеном гласнику Републике Србије. '
    'Образложење ове одлуке заснива се на досадашњој пракси Уставног суда и начелима уставности.</p>'
)


def _patch(monkeypatch, tmp_path, *, view, refs=None, htmls=None):
    monkeypatch.setattr(ingest, "CORPUS_DIR", tmp_path)
    monkeypatch.setattr(index, "CORPUS_DIR", tmp_path)
    monkeypatch.setattr(kg, "CORPUS_DIR", tmp_path)
    monkeypatch.setattr(pis, "get_law_view", lambda guid: view)
    monkeypatch.setattr(pis, "get_eli_link", lambda guid: ELI)
    htmls = htmls or {}
    monkeypatch.setattr(pis, "fetch_consolidated_html",
                        lambda entry, **k: htmls.get(entry.regactid, _SEED_HTML))
    monkeypatch.setattr(pis, "get_references", lambda reg: refs or {})

    def _fetch_doc(url, **k):
        if "Prins" in url or "slgl" in url:  # slgl host → CC decision
            return _CC_HTML
        if "900001" in url:  # reg host → bylaw
            return _BYLAW_HTML
        return ""
    monkeypatch.setattr(pis, "fetch_doc", _fetch_doc)


def _read(tmp_path, name):
    return [json.loads(x) for x in (tmp_path / name).read_text(encoding="utf-8").splitlines() if x.strip()]


def test_pipeline_references_stub_materialized_closure(monkeypatch, tmp_path):
    view = LawView(
        law_guid="g", title="Закон о тесту: 1/2020",
        current=LawVersion(uuid="u", regactid="100", applies_from=date(2020, 1, 1)),
        previous=[],
    )
    refs = {
        "PRAVNI_OSNOV_ZA": [ActReference(
            category="aktKojimaJePropisOsnovDonosenja", edge="PRAVNI_OSNOV_ZA",
            ref_id="900001", name="Правилник о тесту",
            inline_url="https://reg.pravno-informacioni-sistem.rs/api/viewDoc?regActId=900001")],
        "OCENA_USTAVNOSTI": [ActReference(
            category="ustavnoSudskeOdluke", edge="OCENA_USTAVNOSTI",
            ref_id="900002", name="Одлука УС",
            inline_url="https://slgl.pravno-informacioni-sistem.rs/api/Prins/y")],
        "POVEZAN_SA": [ActReference(  # 429975 == zoo (in-corpus per registry)
            category="povezaniPropis", edge="POVEZAN_SA", ref_id="429975",
            name="Закон о облигационим односима",
            inline_url="https://reg.pravno-informacioni-sistem.rs/api/viewDoc?regActId=429975")],
    }
    _patch(monkeypatch, tmp_path, view=view, refs=refs)

    summary = ingest.ingest("bzr", incremental=False)

    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    by_slug = {m["slug"]: m for m in manifest}
    assert by_slug["bzr"]["role"] == "seed" and by_slug["bzr"]["materialized"] is True
    # bylaw materialized at depth 1; CC decision materialized; both referenced
    assert by_slug["ref/900001"]["vrsta"] == "podzakonski_akt"
    assert by_slug["ref/900002"]["vrsta"] == "odluka_us"
    assert "zoo" not in by_slug  # in-corpus target → edge only, not a new node here

    chunks = _read(tmp_path, "chunks.jsonl")
    bzr_chunks = [c for c in chunks if c["metadata"]["law_slug"] == "bzr"]
    assert len(bzr_chunks) == 2
    cc = [c for c in chunks if c["metadata"].get("cc_outcome")]
    assert cc and cc[0]["metadata"]["cc_outcome"] == "struck"
    assert any(c["metadata"].get("in_corpus_depth") == 1 for c in chunks)

    edges = _read(tmp_path, "references.jsonl")
    assert {e["edge"] for e in edges} == {"PRAVNI_OSNOV_ZA", "OCENA_USTAVNOSTI", "POVEZAN_SA"}
    in_corpus = [e for e in edges if e["in_corpus"]]
    assert len(in_corpus) == 1 and in_corpus[0]["to"] == "zoo"

    assert summary["closure_pct"] == pytest.approx(33.3, abs=0.1)
    assert summary["failures"] == 0


def test_kg_build_akt_verzija_clan(monkeypatch, tmp_path):
    """ingest → kg.build_kg on the SQLite store: akt/verzija/clan + typed edges
    (offline; the KG only, no embedding). The build's count gate asserts
    store-side counts match the Python-side counters internally."""
    view = LawView(
        law_guid="g", title="Закон о тесту: 1/2020",
        current=LawVersion(uuid="u", regactid="100", applies_from=date(2020, 1, 1)),
        previous=[],
    )
    refs = {
        "PRAVNI_OSNOV_ZA": [ActReference(
            category="aktKojimaJePropisOsnovDonosenja", edge="PRAVNI_OSNOV_ZA",
            ref_id="900001", name="Правилник",
            inline_url="https://reg.pravno-informacioni-sistem.rs/api/viewDoc?regActId=900001")],
        "OCENA_USTAVNOSTI": [ActReference(
            category="ustavnoSudskeOdluke", edge="OCENA_USTAVNOSTI", ref_id="900002",
            name="Одлука УС", inline_url="https://slgl.pravno-informacioni-sistem.rs/api/Prins/y")],
    }
    _patch(monkeypatch, tmp_path, view=view, refs=refs)
    monkeypatch.setattr(kg, "KG_PATH", tmp_path / "kg.sqlite")
    ingest.ingest("bzr", incremental=False)

    summary = kg.build_kg()
    assert summary["akti"] == 3  # bzr + bylaw + CC decision
    assert summary["verzije"] >= 1  # the current version of bzr
    assert summary["clanovi"] == 4  # 2 bzr + 1 bylaw + 1 CC holding
    assert summary["reference_edges"] == 2  # PRAVNI_OSNOV_ZA + OCENA_USTAVNOSTI
    assert "closure_pct" in summary

    store = kg.open_connection()
    assert store.query("SELECT count(*) AS n FROM pravni_osnov_za")[0]["n"] == 1
    # the typed schema is enforced by the store, not convention (STRICT + FKs)
    assert store.query("SELECT count(*) AS n FROM ocena_ustavnosti")[0]["n"] == 1


def test_version_slicing_changed_articles_only(monkeypatch, tmp_path):
    # two versions: član 1 unchanged, član 2 changed → exactly one slice (clan-2)
    v_cur = LawVersion(uuid="u2", regactid="200", applies_from=date(2022, 1, 1))
    v_prev = LawVersion(uuid="u1", regactid="199", applies_from=date(2019, 1, 1))
    view = LawView(law_guid="g", title="Закон о тесту", current=v_cur, previous=[v_prev])

    def html(c1, c2):
        return (
            '<p class="odluka-zakon">ЗАКОН</p><p class="odluka-zakon">о тесту</p>'
            '<p class="centar">"Службени гласник РС", број 1 од 1. јануара 2019.</p>'
            f'<p class="clan">Члан 1.</p><p>{c1}</p>'
            f'<p class="clan">Члан 2.</p><p>{c2}</p>'
        )
    htmls = {"200": html("Стабилна одредба.", "Нова верзија друге одредбе."),
             "199": html("Стабилна одредба.", "Стара верзија друге одредбе.")}
    _patch(monkeypatch, tmp_path, view=view, htmls=htmls)

    ingest.ingest("bzr", incremental=False, fetch_references=False)
    chunks = _read(tmp_path, "chunks.jsonl")
    slices = [c for c in chunks if c["metadata"].get("is_version_slice")]
    assert len(slices) == 1
    assert slices[0]["chunk_id"].endswith("/clan-2@199")
    assert slices[0]["provenance"]["valid_from"] == "2019-01-01"
    assert slices[0]["provenance"]["valid_to"] == "2022-01-01"
    # no slice for the unchanged član 1
    assert not any("clan-1@" in c["chunk_id"] for c in chunks)
