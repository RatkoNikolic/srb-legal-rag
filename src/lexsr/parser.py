"""Parse PIS consolidated-text HTML into structured Zakon → Član → Stav.

Markup contract (probed 2026-06-10 on Zakon o radu, see tests/fixtures):
- `<p class="odluka-zakon">` — the law's title lines ("ЗАКОН", "о раду").
- `<p class="centar">` — the gazette header: the full amendment chain with
  dates and change types (УС, Аутентично тумачење, др. закон...).
- `<p class="clan">Члан N.</p>` — article headings. The same class is also
  used for chapter headings ("I. ОСНОВНЕ ОДРЕДБЕ") — disambiguated by regex.
- `<p class="bold">` — subsection headings ("1. Предмет").
- plain `<p>` — one stav (paragraph) of the current article.
- **Amended provisions carry a `v2-*` class vocabulary instead**: `v2-clan-1`
  (article heading), `v2-clan-verzal-1` (chapter), `v2-bold-1` (subsection),
  `v2-clan-left-1` / `v2-clan-left-2` (stavovi/tačke of amended text). The
  `v2` styling itself signals "this text is a changed version".
- `<p class="hide-change">*Службени гласник РС, број 75/2014</p>` — editorial
  note that the *preceding* provision was changed by that gazette issue.
- Consolidated texts append the transitional articles of amendment laws
  after the main text ("Самостални чланови..."), restarting numbering —
  detected and flagged `transitional=True`.

Markup dialects (probed 2026-06-11 on the full spike registry):
- **A — amendment-era exports** (Zakon o radu): the contract above.
- **B — young-law exports** (Zakon o digitalnoj imovini, ZZPL 2018): same
  heading classes, but stav bodies are `Basic-Paragraph` and per-article
  titles are `italik` (treated like `bold`).
- **C — legacy/repealed-law exports** (ZZPL 2008; ZOO 1978; Zakon o prometu
  nepokretnosti): semantically classless — `auto-style*` everywhere, **or**
  Word-export `MsoNormal` on *every* `<p>` (ZOO), **or** `auto-style1` reused
  for bodies where another law uses it for headings (ZPN). The class carries
  no meaning, so articles are detected purely by the `Члан N.` regex, chapters
  by the ALL-CAPS roman-numeral pattern, subsections by a short-title
  heuristic — never by class. Carries `<p class="nevazeciakt">` and a publisher's
  note ("НАПОМЕНА ИЗДАВАЧА: Престаје да важи ... <датум>") → **law-level**
  `valid_to`/`repealed_by`. Amendments are marked with trailing `*`
  asterisks + gazette footnotes (not mapped to stavovi in v0 — see
  see the knowledge-base design doc).
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from kl_core.types import Chunk, Provenance
from lexsr.metadata import build_chunk_metadata

_MONTHS = {
    "јануара": 1, "фебруара": 2, "марта": 3, "априла": 4, "маја": 5, "јуна": 6,
    "јула": 7, "августа": 8, "септембра": 9, "октобра": 10, "новембра": 11, "децембра": 12,
}

# Trailing asterisks: dialect-C amendment markers ("Члан 12а *").
_CLAN_RE = re.compile(r"^Члан\s+(\d+[а-џ]?)\s*\.?\s*\*{0,3}\s*$")
# Dialect-C chapter headings: "II. УСЛОВИ ЗА ОБРАДУ" (all-caps checked separately).
_ROMAN_CHAPTER_RE = re.compile(r"^[IVX]+[аб]?\.\s")
_GAZETTE_ENTRY_RE = re.compile(
    r"(\d+)\s+од\s+(\d+)\.\s+([а-џ]+)\s+(\d{4})(?:\s*[-–]\s*([^,]+))?"
)
_CHANGE_NOTE_RE = re.compile(r"број\s+(\d+/\d{4})")
_TRANSITIONAL_RE = re.compile(r"Самостални\s+чл", re.IGNORECASE)
# "Брисан је (види члан 63. Закона - 75/2014-3)" / "Престао је да важи (види...)"
# — editorial repeal notes; "назив" variants refer to deleted *headings*, not articles.
_REPEAL_NOTE_RE = re.compile(r"^(Брисан[аиое]?\s|Преста[ол][аои]?\s)")
# Law-level repeal in the publisher's note of a dialect-C export:
# "НАПОМЕНА ИЗДАВАЧА: Престаје да важи ... односно 21. августа 2019. године (...)"
_LAW_REPEAL_RE = re.compile(r"[Пп]рестаје?\s+да\s+важи|[Пп]рестао\s+да\s+важи")
_DAY_MONTH_YEAR_RE = re.compile(r"(\d{1,2})\.\s*([а-џ]+)\s*(\d{4})\.?\s*године")
_ISSUE_RE = re.compile(r"(\d+/\d{4})")

# --- Entry into force (stupanje na snagu) -----------------------------------
# „in force on date X" is keyed to *entry into force*,
# not publication. The final provision of a law states the rule relative to
# its publication date — „ступа на снагу осмог дана од дана објављивања"
# (8th day), „наредног дана" (next day), „даном објављивања" (same day), or a
# named absolute date. The constitutional default (Ustav RS čl. 196 —
# „најраније осмог дана") is the +8-day fallback when no clause is parseable.
_EIF_CLAUSE_RE = re.compile(r"закон\s+ступа\s+на\s+снагу(.{0,70})")
# Serbian ordinal day-words that appear in entry-into-force clauses → day count.
_EIF_DAY_WORDS = {
    "наредног": 1, "осмог": 8, "петнаестог": 15, "тридесетог": 30,
}
_CONSTITUTIONAL_VACATIO = timedelta(days=8)  # Ustav RS čl. 196 default


def _default_eif(published: date | None) -> date | None:
    """Constitutional default: in force on the 8th day after publication."""
    return published + _CONSTITUTIONAL_VACATIO if published else None


def entry_into_force(clause: str, published: date | None) -> date | None:
    """Resolve a „…ступа на снагу…" clause to an absolute entry-into-force date.

    Only the text immediately following „ступа на снагу" is inspected, so a
    trailing „…, а примењује се од 1. јануара…" (start of *application*, a
    distinct concept — `applies_from`) is not mistaken for entry into force.
    Falls back to the constitutional +8-day default when the clause states no
    recognizable rule.
    """
    m = _EIF_CLAUSE_RE.search(clause)
    if not m:
        return _default_eif(published)
    window = m.group(1)
    for word, days in _EIF_DAY_WORDS.items():
        if word in window and published is not None:
            return published + timedelta(days=days)
    abs_date = _DAY_MONTH_YEAR_RE.search(window)
    if abs_date and abs_date.group(2) in _MONTHS:
        return date(int(abs_date.group(3)), _MONTHS[abs_date.group(2)], int(abs_date.group(1)))
    if "даном објављивања" in window:  # in force on the day of publication
        return published
    return _default_eif(published)


class GazetteRef(BaseModel):
    issue: str  # e.g. "75/2014"
    published: date | None = None
    change_type: str | None = None  # None = textual amendment; else УС / Аутентично тумачење / др. закон


class Stav(BaseModel):
    number: int
    text: str
    changed_by: str | None = None  # gazette issue, e.g. "75/2014"


class Clan(BaseModel):
    number: str  # "1", "32а", ...
    chapter: str | None = None
    subsection: str | None = None
    stavovi: list[Stav] = Field(default_factory=list)
    transitional: bool = False  # belongs to an appended amendment-law block
    repealed_by: str | None = None  # gazette issue that deleted it ("brisan"), e.g. "75/2014"


class ConsolidatedLaw(BaseModel):
    title: str
    gazette_chain: list[GazetteRef] = Field(default_factory=list)
    clanovi: list[Clan] = Field(default_factory=list)
    eli: str | None = None
    # Law-level entry into force (stupanje na snagu) — resolved from the base
    # law's „ступа на снагу" final provision; the constitutional +8-day default
    # otherwise (Q8). This is when the original law took legal effect.
    valid_from: date | None = None
    # Law-level repeal (whole-law "prestao da važi", e.g. ZZPL 2008) — distinct
    # from per-article `repealed_by`; sourced from the publisher's note.
    valid_to: date | None = None
    repealed_by: str | None = None  # gazette issue of the repealing law

    @property
    def repealed(self) -> bool:
        return self.valid_to is not None or self.repealed_by is not None

    @property
    def main_clanovi(self) -> list[Clan]:
        return [c for c in self.clanovi if not c.transitional]


def _parse_gazette_chain(text: str) -> list[GazetteRef]:
    refs: list[GazetteRef] = []
    for issue, day, month, year, change_type in _GAZETTE_ENTRY_RE.findall(text):
        published = None
        if month in _MONTHS:
            published = date(int(year), _MONTHS[month], int(day))
        refs.append(
            GazetteRef(
                issue=f"{issue}/{year}",
                published=published,
                change_type=change_type.strip() or None if change_type else None,
            )
        )
    return refs


def parse_consolidated_html(html: str, *, eli: str | None = None) -> ConsolidatedLaw:
    soup = BeautifulSoup(html, "lxml")

    title_parts = [p.get_text(" ", strip=True) for p in soup.find_all("p", class_="odluka-zakon")]
    law = ConsolidatedLaw(title=" ".join(title_parts) or "?", eli=eli)
    has_semantic_title = bool(title_parts)

    current: Clan | None = None
    chapter: str | None = None
    subsection: str | None = None
    transitional = False
    fallback_title_parts: list[str] = []

    for p in soup.find_all("p"):
        classes = p.get("class") or []
        text = re.sub(r"\s+", " ", p.get_text(" ", strip=True).replace("\xa0", " ")).strip()
        if not text or text.startswith("Преузето са"):
            continue

        # NB: `auto-style1` is deliberately *not* here. In legacy/auto-style
        # exports classes carry no semantics — the same class marks headings in
        # one law (ZZPL 2008) and stav bodies in another (Zakon o prometu
        # nepokretnosti). Real headings are caught by `clan_match` (regex) and
        # the dialect-C shape heuristics below; forcing a class to mean
        # "heading" swallowed article bodies (Q10).
        heading_like = bool({"clan", "v2-clan-1", "v2-clan-verzal-1"} & set(classes))
        bold_like = bool({"bold", "v2-bold-1", "italik"} & set(classes))
        # Stav bodies: classless (dialect A), v2-clan-left* (A, amended),
        # Basic-Paragraph (B), auto-style* (C), or MsoNormal (C, legacy Word
        # exports — ZOO/ZPN, where *every* <p> is MsoNormal and the class
        # carries no semantics; headings/titles are told apart by shape).
        # `tabela` (tables) appears in bylaws (Pravilnik/Uredba) — pre-flight
        # finding (ingestion_pipeline.md §18); treat table cells as body text so
        # članak-level chunks keep their tabular content.
        stav_like = not classes or any(
            c.startswith(("v2-clan-left", "auto-style"))
            or c in ("Basic-Paragraph", "MsoNormal", "tabela")
            for c in classes
        )
        clan_match = _CLAN_RE.match(text)

        if "odluka-zakon" in classes:
            continue
        if "nevazeciakt" in classes:  # dialect C "Неважећи акт" banner
            continue

        # Law-level repeal from the publisher's note (dialect C), e.g.
        # "НАПОМЕНА ИЗДАВАЧА: Престаје да важи ... 21. августа 2019. године
        #  (види члан 101. Закона – 87/2018-54)." Appears before член 1.
        if current is None and _LAW_REPEAL_RE.search(text) and "гласник" in text.lower():
            dmy = _DAY_MONTH_YEAR_RE.search(text)
            if dmy and dmy.group(2) in _MONTHS:
                law.valid_to = date(int(dmy.group(3)), _MONTHS[dmy.group(2)], int(dmy.group(1)))
            issue = _ISSUE_RE.search(text)
            law.repealed_by = issue.group(1) if issue else law.repealed_by
            continue

        # Repeal notes ("Брисан је…", "Престао је да важи…") appear under at
        # least four class vocabularies (hide-change, centar, auto-style1,
        # clan+<em>) — detect by text, not by class. The "назив" variants
        # announce a deleted *heading*, not a deleted article.
        if _REPEAL_NOTE_RE.match(text):
            # "назив"/"речи" variants announce a deleted *heading/words*, not a
            # deleted article; either way the note itself is editorial — consume it.
            if (
                "назив" not in text
                and "речи" not in text
                and current is not None
                and not current.stavovi
            ):
                issue = re.search(r"(\d+/\d{4})", text)
                current.repealed_by = issue.group(1) if issue else "?"
            continue

        # Gazette headers name РС's „Службени гласник" — or, in pre-1992 laws
        # (ZOO 1978), the federal „Службени лист СФРЈ/СРЈ".
        is_gazette_text = "гласник" in text or "Службени лист" in text
        if "centar" in classes:
            if not law.gazette_chain and is_gazette_text:
                law.gazette_chain = _parse_gazette_chain(text)
            continue
        # Dialects C/legacy: the gazette header is a *classless* <p> before член 1.
        if current is None and not law.gazette_chain and is_gazette_text and "од" in text:
            parsed = _parse_gazette_chain(text)
            if parsed:
                law.gazette_chain = parsed
                continue
        if "hide-change" in classes and not heading_like:
            # Deleted articles may carry their *heading* as a hide-change <p>
            # ("Члан 152."); otherwise hide-change marks "changed by gazette X"
            # for the preceding provision.
            if clan_match:
                heading_like = True  # fall through to the article-heading branch
            else:
                issue = _CHANGE_NOTE_RE.search(text)
                if issue and current is not None and current.stavovi:
                    current.stavovi[-1].changed_by = issue.group(1)
                continue

        # Dialect C has no title class — the title is whatever precedes the
        # gazette header ("ЗАКОН", "о заштити података о личности", ...).
        if (
            current is None
            and not has_semantic_title
            and not law.gazette_chain
            and not clan_match
            and not heading_like
        ):
            fallback_title_parts.append(text)
            continue

        # Article headings: class-marked in dialects A/B; in dialect C only
        # the exact "Члан N." text identifies them — the regex is the contract.
        if clan_match:
            number = clan_match.group(1)
            # Numbering restarting at an already-seen article ⇒ we've crossed
            # into an appended amendment-law block (e.g. a second "Члан 1.").
            if any(c.number == number and c.transitional == transitional for c in law.clanovi):
                transitional = True
            current = Clan(
                number=number, chapter=chapter, subsection=subsection, transitional=transitional
            )
            law.clanovi.append(current)
            continue
        if heading_like or bold_like:
            if _TRANSITIONAL_RE.search(text):
                transitional = True
            if heading_like:
                chapter, subsection = text, None
            else:
                subsection = text
            continue

        # Dialect C / legacy (auto-style* or fully classless exports): headings
        # and annotations share the body class, so they are told apart by
        # shape, not markup. The "no semantic title" signal identifies these
        # documents — dialects A/B always carry `odluka-zakon` title lines.
        if not has_semantic_title or any(c.startswith("auto-style") for c in classes):
            if _ROMAN_CHAPTER_RE.match(text) and text == text.upper():
                chapter, subsection = text, None
                continue
            if text.startswith("*") or (_REPEAL_NOTE_RE.match(text) and "(види" in text):
                continue  # asterisk-footnote / stav-level repeal annotation
            if len(text) < 60 and not text.rstrip("* ").endswith((".", ";", ":", ",", ")")):
                subsection = text
                continue

        if current is not None and stav_like:
            if text.startswith("*"):
                continue  # legacy asterisk gazette-footnote markers (not stav text)
            current.stavovi.append(Stav(number=len(current.stavovi) + 1, text=text))

    if not has_semantic_title and fallback_title_parts:
        law.title = " ".join(fallback_title_parts)

    # Entry into force of the *base* law: its final provision („Овај закон
    # ступа на снагу…") lives among the main články (amendment laws' own
    # „ступа на снагу" clauses sit in the appended transitional blocks, which
    # we skip here). Fall back to the constitutional +8-day default.
    base_published = law.gazette_chain[0].published if law.gazette_chain else None
    eif_clause = next(
        (s.text for c in law.main_clanovi for s in c.stavovi if _EIF_CLAUSE_RE.search(s.text)),
        None,
    )
    law.valid_from = (
        entry_into_force(eif_clause, base_published) if eif_clause else _default_eif(base_published)
    )
    return law


# --- Constitutional-Court decisions (holding-level) -------------------------
# CC decisions are consumed through the OCENA_USTAVNOSTI edge, not deep-retrieved
# (ingestion_pipeline.md §5.4), so we extract only an outcome + a holding
# snippet, not a full članak tree. A strike-down sets the target provision's
# validity; upholds set nothing.
CC_STRUCK = "struck"  # provision found unconstitutional
CC_UPHELD = "upheld"  # challenge dismissed
CC_UNKNOWN = "unknown"

_CC_STRUCK_RE = re.compile(
    r"ни(?:је|су)\s+у\s+сагласност|неуставн|престај[уе]\s+да\s+важ|поништав", re.IGNORECASE
)
_CC_UPHELD_RE = re.compile(r"одбија\s+се|не\s+прихвата|одбацује\s+се", re.IGNORECASE)


def cc_outcome(text: str) -> str:
    """Classify a Constitutional-Court decision: struck / upheld / unknown."""
    if _CC_STRUCK_RE.search(text):
        return CC_STRUCK
    if _CC_UPHELD_RE.search(text):
        return CC_UPHELD
    return CC_UNKNOWN


# A CC izreka names the struck provision as „…члан N. … Закона о X … није у
# сагласности…". The leading „на основу члана 167. … Устава" is the court's own
# jurisdictional cite — excluded by requiring „Закон" (not „Устав") as the target.
_CC_NONCONFORM_RE = re.compile(
    r"ни(?:је|су)\s+у\s+сагласност|неуставн|престај[еу]\s+да\s+важ|поништава", re.IGNORECASE
)
_CC_ART_LAW_RE = re.compile(
    r"члан[ауеом]{0,2}\s+((?:\d+[а-џ]?)(?:\.|\s)*"
    r"(?:(?:став|тачк[аеи])\s*\d+[а-џ]?\.?\s*|,\s*|и\s+|до\s+)*)"
    r"[^.]{0,90}?[Зз]акон[ауеом]{0,2}\s+о\s+([а-џ][а-џ\s]{3,60}?)"
    r"(?=\s*[,.\(„”\"]|\s+из\b|\s+број|\s+ни(?:је|су)|\s+преста|$)"
)


# A struck-provision span is „179. став 3. тачка 5)" or „1, 2 и 3" — a leading
# article number, optionally qualified by став/тачка (a *partial* strike). The
# qualifier digits must NOT be mistaken for article numbers (the old bug marked
# članci 3 and 5 repealed when only član 179 st.3 t.5 was struck).
_ART_OR_SUBDIV = re.compile(r"(став|тачк[аеи])\s*(\d+[а-џ]?)|(\d+[а-џ]?)")


def _parse_article_strikes(span: str) -> list[tuple[str, str | None]]:
    """Parse a struck-provision span into [(article_number, struck_part | None)].
    став/тачка attach to the most recent article (a partial strike, part != None);
    a bare number is a whole-article strike (part is None)."""
    arts: list[list] = []
    for t in _ART_OR_SUBDIV.finditer(span):
        if t.group(1):  # став/тачка → qualifies the last article (partial strike)
            if arts:
                piece = f"{t.group(1)} {t.group(2)}"
                arts[-1][1] = f"{arts[-1][1]}, {piece}" if arts[-1][1] else piece
        else:  # bare number → a new article target
            arts.append([t.group(3), None])
    return [(a, p) for a, p in arts]


def cc_struck_targets(holding: str) -> list[tuple[str, list[tuple[str, str | None]]]]:
    """Best-effort (law_phrase, [(article_number, struck_part|None)]) the CC izreka
    struck — only when the holding actually states a non-conformity. The
    *resolution* gate (must match an in-corpus article) lives downstream, so a noisy
    phrase that resolves to nothing sets nothing — no false invalidation (invariant
    2). `struck_part` is None for a whole-article strike, else the став/тачка struck
    (a partial strike — the article stays in force, carries a note)."""
    if not _CC_NONCONFORM_RE.search(holding):
        return []
    out: list[tuple[str, list[tuple[str, str | None]]]] = []
    for m in _CC_ART_LAW_RE.finditer(holding):
        arts = _parse_article_strikes(m.group(1))
        law = re.sub(r"\s+", " ", m.group(2)).strip()
        if arts and "устав" not in law.lower():
            out.append((law, arts))
    return out


def cc_decision_date(holding: str) -> date | None:
    """The decision date („на седници одржаној D. M. YYYY") — the approximate
    constitutional-invalidation date (the precise effect date is the decision's
    Sl. glasnik publication, not always in the holding)."""
    m = re.search(r"седниц[аи]\s+одржаној\s+" + _DAY_MONTH_YEAR_RE.pattern, holding)
    if not m:
        m2 = _DAY_MONTH_YEAR_RE.search(holding)
        if not m2 or m2.group(2) not in _MONTHS:
            return None
        return date(int(m2.group(3)), _MONTHS[m2.group(2)], int(m2.group(1)))
    if m.group(2) not in _MONTHS:
        return None
    return date(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)))


def extract_cc_holding(html: str, *, max_chars: int = 2000) -> tuple[str, str]:
    """(outcome, holding_text) for a CC-decision HTML. Holding = the leading
    substantive text (the izreka), boilerplate stripped."""
    soup = BeautifulSoup(html, "lxml")
    lines: list[str] = []
    for s in soup.stripped_strings:
        t = re.sub(r"\s+", " ", s).strip()
        if not t or t.startswith(("Преузето са", "https://", "Овај акт", "/eli/")):
            continue
        lines.append(t)
        if sum(len(x) for x in lines) >= max_chars:
            break
    holding = "\n".join(lines)[:max_chars]
    return cc_outcome(holding), holding


def to_chunks(
    law: ConsolidatedLaw, *, source_id: str, applies_from: date | None = None
) -> list[Chunk]:
    """One retrieval chunk per član; per-stav structure kept in metadata.

    `valid_from` is the article's **entry into force** (Q8), not its
    publication date: an unchanged article inherits the law's entry into force;
    an amended one takes the entry into force of the gazette issue that last
    changed it (the constitutional +8-day default applied to that issue's
    publication, since amendment laws' own clauses are not per-issue parsed in
    v0). `applies_from` (PIS `versionApplicationDate` — *početak primene*, the
    distinct start-of-application date) is carried in metadata when known.

    Repealed (brisan) articles are emitted too, with `valid_to` resolved from
    the gazette chain — they are the ground-truth targets for the benchmark's
    abstain/repealed question category, not noise to be filtered out. The same
    holds one level up: in a whole-law-repealed statute (ZZPL 2008) every
    article inherits the law-level `valid_to` unless it was repealed earlier.
    """
    law_eif = law.valid_from
    base_published = law.gazette_chain[0].published if law.gazette_chain else None
    published_by_issue = {g.issue: g.published for g in law.gazette_chain}
    mains = law.main_clanovi
    chunks: list[Chunk] = []
    for ordinal, clan in enumerate(mains):
        last_change = max(
            (s.changed_by for s in clan.stavovi if s.changed_by),
            # (year, issue-number) — year alone picks arbitrarily between two
            # same-year amendments and can date valid_from off the wrong issue
            key=lambda issue: (int(issue.split("/")[1]), int(issue.split("/")[0])),
            default=None,
        )
        # An amended article's current text entered force when its amendment
        # did; the constitutional +8-day default applies to that issue's date.
        amend_published = published_by_issue.get(last_change) if last_change else None
        valid_from = _default_eif(amend_published) if amend_published else law_eif
        repealed = clan.repealed_by is not None
        meta = build_chunk_metadata(
            chapter=clan.chapter,
            subsection=clan.subsection,
            heading=clan.subsection,
            text="\n".join(s.text for s in clan.stavovi),
            law_title=law.title,
            fragment=f"član {clan.number}",
            ordinal=ordinal,
            prev_clan=mains[ordinal - 1].number if ordinal > 0 else None,
            next_clan=mains[ordinal + 1].number if ordinal + 1 < len(mains) else None,
            stav_count=len(clan.stavovi),
            last_changed_by=last_change,
            published=base_published,
            applies_from=applies_from,
            valid_from=valid_from,
            repealed=repealed,
            law_repealed=law.repealed,
            transitional=clan.transitional,
        )
        meta.update(
            stavovi=[s.model_dump(mode="json") for s in clan.stavovi],
            repealed=repealed or law.repealed,
            repealed_by=clan.repealed_by,
            law_repealed=law.repealed,
            law_repealed_by=law.repealed_by,
        )
        chunks.append(
            Chunk(
                chunk_id=f"{source_id}/clan-{clan.number}",
                text="\n".join(s.text for s in clan.stavovi)
                if not repealed
                else f"(брисан — види „Службени гласник РС“ {clan.repealed_by})",
                provenance=Provenance(
                    source_id=source_id,
                    source_title=law.title,
                    fragment=f"član {clan.number}",
                    version=last_change,
                    valid_from=valid_from,
                    valid_to=published_by_issue.get(clan.repealed_by)
                    if repealed
                    else law.valid_to,
                    url=law.eli,
                ),
                metadata=meta,
            )
        )
    return chunks
