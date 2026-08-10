"""Per-chunk metadata builder — law context + neighbourhood (Tier 1).

Implements the corpus-intrinsic ("Tier 1") metadata model (knowledge_base.md §5):
deterministic facts about a članо that the raw article text never states —
its temporal status, its place in the law's structure, and what *kind* of
article it is. No LLM, no network; pure functions over the parsed law.

Tier 2 (the denormalized reference graph, embedding-side contextualization)
is a *retrieval treatment* and lives elsewhere (the KG + arm config), so the
benchmark arms stay distinguishable — see the doc's §3.
"""

from __future__ import annotations

import re
from datetime import date

# --- deterministic article-type tags (group D of the schema) ----------------
# Detected on the article heading + body; Cyrillic, case-insensitive where it
# helps. These power routing (definitions lookups), and honest answers about
# delegated detail ("the law leaves this to a bylaw").
# Two phrasings, both common in Serbian statutes: the forward "значење израза"
# and — far more common as the definitions article's lead — the *reverse*
# "(Поједини) изрази/појмови/термини … имају следеће значење". The forward-only
# regex missed the reverse form, leaving ~1/3 of laws without a flagged
# definitions article (and starving the KG's DEFINED_IN edge).
_DEFINITIONS_RE = re.compile(
    r"значењ[еа]\s+(израза|појединих|појмова)|^појмови|дефинициј"
    r"|(?:изрази|појмови|термини)\b[^.\n]{0,70}?(?:имају|има)\s+(?:следеће\s+)?значењ",
    re.IGNORECASE,
)
_PENALTY_RE = re.compile(
    r"казнен[ае]\s+одредб|новчаном\s+казном|прекршај|привредн[иом]\s+преступ", re.IGNORECASE
)
_BYLAW_RE = re.compile(
    r"ближе[а-џ\s]{0,30}?(уређује|прописује|одређује|утврђује)|"
    r"(министар|влад[аеу])[а-џ\s]{0,30}?пропис|"
    r"пропис[а-џ\s]{0,20}?(министар|влад)|"
    r"подзаконск|актом\s+(министра|владе)",
    re.IGNORECASE,
)

STATUS_IN_FORCE = "in_force"
STATUS_REPEALED = "repealed"


def classify_article(heading: str, text: str) -> dict:
    """Deterministic type tags for an article (heading = naziv/chapter, text =
    the article body). Cheap regex/keyword rules — see the schema group D."""
    probe = f"{heading}\n{text}"
    return {
        "is_definitions": bool(_DEFINITIONS_RE.search(heading) or _DEFINITIONS_RE.search(text[:200])),
        "is_penalty": bool(_PENALTY_RE.search(probe)),
        "delegates_to_bylaw": bool(_BYLAW_RE.search(text)),
    }


def chunk_status(*, repealed: bool, law_repealed: bool) -> str:
    """Absolute (date-independent) status. The date-relative distinction
    `in_force_not_yet_applied` is derived at query time from `valid_from` vs
    `applies_from` (the data is carried); only repeal is absolute here."""
    return STATUS_REPEALED if (repealed or law_repealed) else STATUS_IN_FORCE


def structural_path(chapter: str | None, subsection: str | None) -> str:
    """Heading breadcrumb situating the article: „Глава › пододељак".

    We expose the structure the parser reliably tracks (chapter + subsection);
    full Deo › Glava › Odeljak depth is partial in classless dialect-C exports
    (knowledge_base.md §5), so we do not fabricate levels we did not parse.
    """
    return " › ".join(p for p in (chapter, subsection) if p)


def contextual_prefix(
    *, fragment: str, law_title: str, path: str, valid_from: str
) -> str:
    """A short, *discriminative* situating line (knowledge_base.md §5, contextual_prefix): leads
    with the article + its heading breadcrumb (unique to this chunk), not the
    constant law title — boilerplate prefixes dilute embeddings.

    Stored for the agentic nav block, the UI, and as the building block for the
    (separately-ablated) embedding/BM25-side contextualization.
    """
    bits = [fragment]
    if path:
        bits.append(path)
    short = re.sub(r"^ЗАКОН\s*", "Закон ", law_title).strip()
    bits.append(short)
    if valid_from:
        bits.append(f"на снази од {valid_from}")
    return " · ".join(bits)


def build_chunk_metadata(
    *,
    chapter: str | None,
    subsection: str | None,
    heading: str | None,
    text: str,
    law_title: str,
    fragment: str,
    ordinal: int,
    prev_clan: str | None,
    next_clan: str | None,
    stav_count: int,
    last_changed_by: str | None,
    published: date | None,
    applies_from: date | None,
    valid_from: date | None,
    repealed: bool,
    law_repealed: bool,
    transitional: bool,
) -> dict:
    """Assemble the Tier-1 metadata dict for one článо chunk."""
    path = structural_path(chapter, subsection)
    tags = classify_article(heading or subsection or "", text)
    return {
        # law_slug / cluster are stamped by the corpus builder (not known here)
        # structural neighbourhood
        "chapter": chapter,
        "subsection": subsection,
        "article_heading": heading or subsection or "",
        "structural_path": path,
        "ordinal": ordinal,
        "prev_clan": prev_clan or "",
        "next_clan": next_clan or "",
        "stav_count": stav_count,
        # temporal (the dated triple lives in provenance; these are the extras)
        "published": str(published) if published else "",
        "applies_from": str(applies_from) if applies_from else "",
        "last_changed_by": last_changed_by,
        "status": chunk_status(repealed=repealed, law_repealed=law_repealed),
        # type tags
        "is_transitional": transitional,
        **tags,
        # retrieval aid (Tier-2 building block; stored, not yet embedded)
        "contextual_prefix": contextual_prefix(
            fragment=fragment,
            law_title=law_title,
            path=path,
            valid_from=str(valid_from) if valid_from else "",
        ),
    }
