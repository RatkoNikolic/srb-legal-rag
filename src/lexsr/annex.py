"""Annex extraction — make the non-prose content of statutes/bylaws retrievable.

Two kinds of annex content that the prose parser (`lexsr.parser`) does not
capture as clean article text (docs/ingestion_pipeline.md §6, IP-1/IP-5; DS-3):

- **HTML tables** (`<table>`) — tarife/rate schedules in bylaws. Deterministic:
  flattened to a markdown table. No model call.
- **Image annexes** (`<img src=".../Attachment/slike/…">`) — obrasci/forms and
  scanned tables. Transcribed by **gemini-3.6-flash** (multimodal OCR), prompted
  to emit markdown so table structure survives.

Both are emitted as **additive chunks** attached to their act (it never touches
the contract-locked prose parser). Every annex chunk is flagged
`source_modality` ∈ {`table_html`, `ocr`} and carries the source URL as
provenance — an OCR transcription is **not verbatim-authoritative** (invariant 1),
so it is traceable and a human can verify against the image.

CLI:
    python -m lexsr.annex            # tables only (deterministic, free)
    python -m lexsr.annex --ocr      # + OCR image annexes ($, gemini-3.6-flash)
    python -m lexsr.annex 1 --ocr    # restrict to one cluster's acts
"""

from __future__ import annotations

import json
import re
import sys

from bs4 import BeautifulSoup

from kl_core.llm import generate_text
from kl_core.types import Chunk, Provenance
from lexsr import pis
from lexsr.config import CORPUS_DIR, gemini_api_key

_IMG_ANNEX_RE = re.compile(r"Attachment/slike/", re.IGNORECASE)
_MIN_TABLE_CELLS = 4  # skip layout/1-cell tables
# Cap OCR per act: a few acts embed hundreds of page-images (e.g. a 307-image
# systematization org-chart annex) — high cost, near-zero QA value. Cap to the
# first N so one pathological act can't dominate cost/time; the skip is logged.
_MAX_IMAGES_PER_ACT = 20
_OCR_PROMPT = (
    "Ово је слика прилога (образац или табела) из српског прописа. "
    "Препиши САВ видљиви текст верно на ћирилици. Ако је табела, врати је као "
    "markdown табелу са свим редовима и колонама. Не додај објашњења."
)


def extract_tables(html: str) -> list[str]:
    """Each `<table>` → a markdown string (header row + body). Skips trivial
    (layout) tables. Deterministic, no model call."""
    soup = BeautifulSoup(html, "lxml")
    out: list[str] = []
    for tbl in soup.find_all("table"):
        rows: list[list[str]] = []
        for tr in tbl.find_all("tr"):
            cells = [
                re.sub(r"\s+", " ", c.get_text(" ", strip=True)).strip()
                for c in tr.find_all(["td", "th"])
            ]
            if any(cells):
                rows.append(cells)
        if not rows or sum(len(r) for r in rows) < _MIN_TABLE_CELLS:
            continue
        width = max(len(r) for r in rows)
        norm = [r + [""] * (width - len(r)) for r in rows]
        md = ["| " + " | ".join(norm[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
        md += ["| " + " | ".join(r) + " |" for r in norm[1:]]
        out.append("\n".join(md))
    return out


def extract_image_annexes(html: str) -> list[str]:
    """Distinct annex image URLs (`Attachment/slike/…`), in document order."""
    soup = BeautifulSoup(html, "lxml")
    urls: list[str] = []
    seen: set[str] = set()
    for img in soup.find_all("img"):
        src = (img.get("src") or "").strip()
        if src and _IMG_ANNEX_RE.search(src) and src not in seen:
            seen.add(src)
            urls.append(src)
    return urls


def _mime_for(url: str, content_type: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct.startswith("image/"):
        return ct
    low = url.lower()
    for ext, mime in ((".png", "image/png"), (".jpg", "image/jpeg"),
                      (".jpeg", "image/jpeg"), (".gif", "image/gif")):
        if ext in low:
            return mime
    return "image/png"  # PIS `.image` assets are PNG


def ocr_image(url: str, llm) -> tuple[str, float]:
    """OCR one annex image with gemini-3.6-flash. Returns (markdown_text, cost).
    Short fetch timeout so one bad image cannot hang the pass; shape failures
    are retried (≤3); raises on failure (caller isolates per image)."""
    raw, content_type = pis.fetch_bytes(url, timeout_s=20)
    res, acct = generate_text(
        llm, _OCR_PROMPT, images=[(_mime_for(url, content_type), raw)],
        max_output_tokens=16384,
    )
    return res.text.strip(), acct.cost_usd


def _annex_chunk(act: dict, idx: int, kind: str, text: str, url: str | None) -> Chunk:
    sid = act["source_id"]
    label = "табела" if kind == "table_html" else "образац"
    return Chunk(
        chunk_id=f"{sid}/prilog-{kind}-{idx}",
        text=text,
        provenance=Provenance(
            source_id=sid, source_title=act.get("title", ""),
            fragment=f"прилог ({label} {idx + 1})", url=url or act.get("eli"),
        ),
        metadata={
            "law_slug": act.get("slug", sid), "cluster": act.get("cluster", "referenced"),
            "role": act.get("role", "referenced"), "vrsta": act.get("vrsta", "akt"),
            "in_corpus_depth": int(act.get("in_corpus_depth", 1)), "materialized": True,
            "fragment": f"прилог ({label} {idx + 1})", "is_annex": True,
            "source_modality": kind, "image_url": url or "",
            "contextual_prefix": f"{act.get('title','')} — прилог ({label} {idx + 1})",
        },
    )


def _write_chunks(chunks: list[dict]) -> None:
    with open(CORPUS_DIR / "chunks.jsonl", "w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")


def build_annex(selector: str = "all", *, ocr: bool = False) -> dict:
    """Append annex chunks (tables always; image OCR if `ocr`) for the
    materialized acts in the manifest to chunks.jsonl, deduped by chunk_id.

    **Resumable + checkpointed:** existing annex chunks are kept, acts that
    already have OCR chunks are skipped, and chunks.jsonl is flushed after every
    act that did OCR — so a slow/killed run never loses progress and can be
    re-run to continue (the hang that buffered-and-wrote-once could not)."""
    manifest = json.loads((CORPUS_DIR / "manifest.json").read_text(encoding="utf-8"))
    chunks = [json.loads(line) for line in (CORPUS_DIR / "chunks.jsonl").open(encoding="utf-8")]
    seen_ids = {c["chunk_id"] for c in chunks}
    # acts already OCR'd (resume support): source_id before "/prilog-ocr-"
    ocr_done = {c["chunk_id"].rsplit("/prilog-ocr-", 1)[0]
                for c in chunks if "/prilog-ocr-" in c["chunk_id"]}

    from lexsr import registry
    want = None if selector == "all" else {s.slug for s in registry.resolve_specs(selector)}
    # With a selector, a referenced act is in scope only when its enabling law
    # is one of the selected laws (PRAVNI_OSNOV_ZA parent) — the old blanket
    # `cluster == "referenced"` override OCR'd every referenced act corpus-wide
    # regardless of the selector, which is exactly the cost the flag guards.
    parent_slug: dict[str, str] = {}
    if want is not None:
        try:
            with open(CORPUS_DIR / "references.jsonl", encoding="utf-8") as f:
                for line in f:
                    e = json.loads(line)
                    if e.get("edge") == "PRAVNI_OSNOV_ZA" and e.get("to") and e.get("from_slug"):
                        parent_slug.setdefault(e["to"], e["from_slug"])
        except FileNotFoundError:
            pass
    acts = [m for m in manifest if m.get("materialized")
            and m.get("vrsta") != "odluka_us"  # CC holdings have no annexes
            and (want is None or m.get("slug") in want
                 or parent_slug.get(m.get("source_id", "")) in want)]

    llm = None
    if ocr:
        from kl_core.providers.gemini import GeminiLLM
        llm = GeminiLLM(gemini_api_key(), timeout_s=240)  # vision: 16k-token OCR needs headroom

    n_tables = n_ocr = n_failed = n_resumed = 0
    cost = 0.0
    for n, act in enumerate(acts):
        sid = act["source_id"]
        html_path = CORPUS_DIR / f"{act.get('regactid', '')}.html"
        if not html_path.exists():
            continue
        html = html_path.read_text(encoding="utf-8")
        for i, md in enumerate(extract_tables(html)):
            c = _annex_chunk(act, i, "table_html", md, None)
            if c.chunk_id not in seen_ids:
                chunks.append(c.model_dump(mode="json"))
                seen_ids.add(c.chunk_id)
                n_tables += 1
        if not ocr:
            continue
        if sid in ocr_done:
            n_resumed += 1
            continue
        all_images = extract_image_annexes(html)
        images = all_images[:_MAX_IMAGES_PER_ACT]
        if images:
            skip = len(all_images) - len(images)
            note = f" (capped, {skip} skipped)" if skip else ""
            print(f"[annex] {sid}: {len(images)} images → OCR …{note}", flush=True)
        added = 0
        for i, url in enumerate(images):
            cid = f"{sid}/prilog-ocr-{i}"
            if cid in seen_ids:
                continue
            try:
                text, c = ocr_image(url, llm)
                cost += c
                if text:
                    chunks.append(_annex_chunk(act, i, "ocr", text, url).model_dump(mode="json"))
                    seen_ids.add(cid)
                    n_ocr += 1
                    added += 1
            except Exception as exc:  # one bad image must not sink the pass
                n_failed += 1
                print(f"[annex]   ! ocr {url[:70]}: {type(exc).__name__}", flush=True)
        if added:  # checkpoint after each act that produced OCR chunks
            _write_chunks(chunks)

    _write_chunks(chunks)
    summary = {"acts": len(acts), "table_chunks": n_tables, "ocr_chunks": n_ocr,
               "ocr_failed": n_failed, "acts_resumed": n_resumed,
               "ocr_cost_usd": round(cost, 4), "total_chunks": len(chunks)}
    print(f"[annex] done: {summary}", flush=True)
    return summary


def _main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    flags = {a for a in argv if a.startswith("--")}
    build_annex(args[0] if args else "all", ocr="--ocr" in flags)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
