"""The L2/L3 typed reference endpoint (pis.get_references) — JSON-shape parsing,
category→edge mapping, and host routing (ingestion_pipeline.md §18)."""

from lexsr import pis


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


# Real shape (probed 2026-06-20): {category: {actReferenceTypeName, lawActs:[...]}}.
_PAYLOAD = {
    "osnovniTekstIzmeneDopuneIspravke": {  # amendment chain — must be skipped
        "actReferenceTypeName": "Основни текст…",
        "lawActs": [{"id": "1", "name": "измена", "inlineUrl": "https://slgl…/Prins/x", "url": "None"}],
    },
    "osnovDonosenjaPropisa": {
        "actReferenceTypeName": "Основ доношења",
        "lawActs": [{"id": "433539", "name": "Устав РС",
                     "inlineUrl": "https://reg.pravno-informacioni-sistem.rs/api/viewDoc?regActId=433539",
                     "url": "None"}],
    },
    "aktKojimaJePropisOsnovDonosenja": {
        "actReferenceTypeName": "Бylaws",
        "lawActs": [
            {"id": "900001", "name": "Правилник",
             "inlineUrl": "https://reg.pravno-informacioni-sistem.rs/api/viewDoc?regActId=900001",
             "url": "None"},
            {"id": "", "name": "no-id row", "inlineUrl": "", "url": "None"},  # dropped
        ],
    },
    "ustavnoSudskeOdluke": {
        "actReferenceTypeName": "Уставносудске одлуке",
        "lawActs": [{"id": "900002", "name": "Одлука УС",
                     "inlineUrl": "https://slgl.pravno-informacioni-sistem.rs/api/Prins/y",
                     "url": "None"}],
    },
}


def test_get_references_maps_and_routes(monkeypatch):
    monkeypatch.setattr(pis, "_request", lambda *a, **k: _FakeResp(_PAYLOAD))
    refs = pis.get_references("439321")

    # amendment category is skipped (reconstructed from the version timeline)
    assert "AMENDMENT" not in refs and "osnovniTekstIzmeneDopuneIspravke" not in refs
    # categories map to edge types
    assert {"USTAVNI_OSNOV", "PRAVNI_OSNOV_ZA", "OCENA_USTAVNOSTI"} <= set(refs)

    ustav = refs["USTAVNI_OSNOV"][0]
    assert ustav.ref_id == "433539" and ustav.host == "reg"

    # the id-less / url-less row was dropped
    bylaws = refs["PRAVNI_OSNOV_ZA"]
    assert [b.ref_id for b in bylaws] == ["900001"]

    cc = refs["OCENA_USTAVNOSTI"][0]
    assert cc.ref_id == "900002" and cc.host == "slgl"  # served only on slgl/Prins


def test_reference_edge_mapping_complete():
    # every mapped category has a non-empty edge name
    assert all(v for v in pis.REFERENCE_EDGES.values())
    assert pis.REFERENCE_EDGES["ustavnoSudskeOdluke"] == "OCENA_USTAVNOSTI"
