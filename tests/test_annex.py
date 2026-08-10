"""Annex extraction (lexsr.annex): deterministic tables + mocked OCR."""

from __future__ import annotations

from kl_core.llm import GenerationResult
from lexsr import annex


def test_extract_tables_to_markdown():
    html = """
    <html><body>
      <table>
        <tr><th>Тарифни број</th><th>Износ</th></tr>
        <tr><td>1</td><td>100 РСД</td></tr>
        <tr><td>2</td><td>200 РСД</td></tr>
      </table>
    </body></html>
    """
    tables = annex.extract_tables(html)
    assert len(tables) == 1
    md = tables[0]
    assert "| Тарифни број | Износ |" in md
    assert "| --- | --- |" in md
    assert "| 1 | 100 РСД |" in md
    assert "| 2 | 200 РСД |" in md


def test_extract_tables_skips_trivial():
    # a layout table with too few cells is ignored
    html = "<table><tr><td>x</td></tr></table>"
    assert annex.extract_tables(html) == []


def test_extract_image_annexes_dedup_and_filter():
    html = """
    <img src="https://reg.pravno-informacioni-sistem.rs/api/Attachment/slike/427469/OBR_1.png"/>
    <img src="https://reg.pravno-informacioni-sistem.rs/api/Attachment/slike/427469/OBR_1.png"/>
    <img src="https://example.com/logo.png"/>
    """
    urls = annex.extract_image_annexes(html)
    assert urls == [
        "https://reg.pravno-informacioni-sistem.rs/api/Attachment/slike/427469/OBR_1.png"
    ]


def test_mime_for():
    assert annex._mime_for("x/y.png", "") == "image/png"
    assert annex._mime_for("x/y.JPG", "") == "image/jpeg"
    assert annex._mime_for("x/y.image", "image/gif") == "image/gif"
    assert annex._mime_for("x/y.image", "") == "image/png"  # PIS .image default


def test_ocr_image_uses_vision_llm(monkeypatch):
    monkeypatch.setattr(annex.pis, "fetch_bytes", lambda url, **kw: (b"\x89PNG..", "image/png"))

    captured = {}

    class _FakeLLM:
        def generate(self, prompt, *, images=None, max_output_tokens=8192):
            captured["images"] = images
            captured["prompt"] = prompt
            return GenerationResult(
                text="| а | б |\n| --- | --- |\n| 1 | 2 |",
                model="fake-vision", cost_usd=0.0012,
            )

    text, cost = annex.ocr_image("https://x/Attachment/slike/1/o.png", _FakeLLM())
    assert "| а | б |" in text
    assert cost == 0.0012
    assert captured["images"][0][0] == "image/png"  # mime passed through
    assert captured["images"][0][1] == b"\x89PNG.."  # raw bytes passed through
