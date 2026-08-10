"""Typed legal KG (SQLite, stdlib, embedded) — the L0–L3 Akt/Verzija/Clan model.

The graph is the *data model*; storage is typed relational node/edge tables in
stdlib SQLite (chosen because the serve-time workload is exact-key lookups; the
SQL/PGQ-style "property graph over relational tables" pattern). One table per
node type, one per edge type — `STRICT` + endpoint FOREIGN KEYs, so the typed
schema is enforced by the store, not by convention.

Schema (ingestion_pipeline.md §5):
  akt(source_id PK, slug, title, vrsta, role, cluster, materialized,
      in_corpus_depth, eli, valid_from, valid_to, applies_from, repealed_by,
      down_weight)
  verzija(regactid PK, akt_source→akt, applies_from)
  clan(chunk_id PK, seq, source_id→akt, law_slug, number, fragment, text,
       chapter, structural_path, article_heading, status, valid_from, valid_to,
       applies_from, published, repealed, is_definitions, is_penalty, version,
       is_version_slice, cc_outcome)

  sadrzi(akt→clan)                 act → its articles
  ima_verziju(akt→verzija)         act → its dated consolidations
  u_verziji(clan→verzija)          article slice → the version it lived in
  upucuje_na(clan→clan, mention)   intra-corpus cross-reference (regex);
                                   traversed UNDIRECTED by the arms
  next(clan→clan)                  sequential (document order)
  defined_in(clan→clan)            article → its law's definitions article
  pravni_osnov_za(akt→akt)         law → bylaw under it        (authoritative L2)
  ocena_ustavnosti(akt→akt, outcome)  law → CC decision against it (D-8)
  ustavni_osnov(akt→akt, down_weight) law → its enactment basis (hub; D-7)
  povezan_sa(akt→akt, down_weight)    related/connected; traversed UNDIRECTED
  uticao_na(akt→akt, down_weight)     affected-by

`clan.seq` is the document-order insertion index — the deterministic ORDER BY
key for every truncated (LIMIT) expansion query, so which neighbours survive a
cap is document order, never engine-internal row order. Reference edges
(akt→akt) come from the authoritative PIS reference graph (`references.jsonl`);
intra-law edges (upucuje_na/next/defined_in) come from regex extraction over
stav text. Hub-origin reference edges carry `down_weight` so the graph arm can
exclude them from 2-hop expansion (D-7). Stub `akt` rows (`materialized=0`)
keep the graph complete without being retrievable (D-11).

After the bulk load, a **count gate** asserts store-side `SELECT count(*)` per
table equals the Python-side counters that produce `graph_stats.json` — the
store provably holds the graph the stats describe (one-command reviewer check).

Usage: .venv/bin/python -m lexsr.kg
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import threading
from pathlib import Path

from lexsr.config import CORPUS_DIR, KG_PATH
from lexsr.index import load_chunks
from lexsr.parser import CC_STRUCK, cc_decision_date, cc_struck_targets
from lexsr.translit import to_cyrillic

# ---- regex cross-reference extraction (unchanged contract) ------------------
_CLAN_REF = re.compile(r"чл(?:ан[ауеом]{0,2}|\.)\s*((?:\d+[а-џ]?[.,]?\s*(?:и|до|–|—|-)?\s*)+)")
_NUM = re.compile(r"\d+[а-џ]?")
_PURE_NUM = re.compile(r"^\d+$")
_INTERNAL = re.compile(r"ов(?:ог|им|их|е)\s+закон")
_RANGE = re.compile(r"\bдо\b|[–—-]")
_CROSS_LAW = re.compile(
    r"[Зз]акон[ауеом]{0,2}\s+о\s+([а-џ][а-џ\s]{3,80}?)"
    r"(?=\s*[(,.„“”\"]|\s+из\b|\s+број|\s+члан|\s+и\s+друг|$)"
)
_NAME_PREFIX = re.compile(r"^\s*(?:закон[ауеом]{0,2}\s+)?(?:о\s+)?")
_PUNCT = re.compile(r"[^а-џa-z0-9\s-]")
_CLAN_NUM_RE = re.compile(r"/clan-([^@/]+)")


def _name_tokens(name: str) -> tuple[str, ...]:
    s = _PUNCT.sub(" ", name.lower())
    s = _NAME_PREFIX.sub("", s)
    return tuple(t for t in s.replace("-", " ").split() if t)


def _slug_tokens(slug: str) -> tuple[str, ...]:
    body = slug
    for pref in ("zakon-o-", "zakon-", "zzpl-", "zup"):
        if body.startswith(pref):
            body = body[len(pref):]
            break
    return tuple(to_cyrillic(body.replace("-", " ")).split())


def build_law_keys(law_titles: dict[str, str]) -> dict[str, list[tuple[str, ...]]]:
    keys: dict[str, list[tuple[str, ...]]] = {}
    for slug, title in law_titles.items():
        cand: set[tuple[str, ...]] = set()
        tt = _name_tokens(title)
        if tt and tt != ("закон",):
            cand.add(tt)
        st = _slug_tokens(slug)
        if st and st != ("закон",):
            cand.add(st)
        keys[slug] = [k for k in cand if k]
    return keys


def _prefix_len(a: tuple[str, ...], b: tuple[str, ...]) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _resolve_law(mention: str, law_keys: dict[str, list[tuple[str, ...]]]) -> str | None:
    mtok = _name_tokens(mention)
    if not mtok:
        return None
    best_slug: str | None = None
    best_score = 0
    tie = False
    for slug, keys in law_keys.items():
        for key in keys:
            n = _prefix_len(mtok, key)
            if n == 0 or n != min(len(mtok), len(key)):
                continue
            if n > best_score:
                best_slug, best_score, tie = slug, n, False
            elif n == best_score and slug != best_slug:
                tie = True
    return None if tie else best_slug


def _expand_numbers(group: str) -> list[str]:
    nums = _NUM.findall(group)
    if (_RANGE.search(group) and len(nums) >= 2
            and _PURE_NUM.match(nums[0]) and _PURE_NUM.match(nums[-1])):
        lo, hi = int(nums[0]), int(nums[-1])
        if 0 < hi - lo <= 50:
            return [str(n) for n in range(lo, hi + 1)]
    return nums


def _extract_refs(
    text: str, law_titles: dict[str, str] | None = None, *,
    law_keys: dict[str, list[tuple[str, ...]]] | None = None,
) -> list[tuple[str | None, str, str]]:
    """Yield (target_law_slug | None for same-law, clan_number, mention)."""
    if law_keys is None:
        law_keys = build_law_keys(law_titles or {})
    refs: list[tuple[str | None, str, str]] = []
    for m in _CLAN_REF.finditer(text):
        forward = text[m.end(): m.end() + 160]
        target_slug: str | None = None
        if _INTERNAL.search(forward[:50]):
            target_slug = None
        else:
            cross = _CROSS_LAW.search(forward)
            if cross is None:
                back = text[max(0, m.start() - 140): m.start()]
                back_matches = list(_CROSS_LAW.finditer(back))
                cross = back_matches[-1] if back_matches else None
            if cross is not None:
                target_slug = _resolve_law(cross.group(1), law_keys)
                if target_slug is None:
                    continue
        for num in _expand_numbers(m.group(1)):
            refs.append((target_slug, num, m.group(0).strip()))
    return refs


def _law_name(m: dict) -> str:
    rt = (m.get("registry_title") or "").split(":", 1)[0].strip()
    return rt or (m.get("title") or "").strip()


CC_STRUCK_STATUS = "престао по одлуци УС"


def apply_cc_invalidation(chunk_dicts: list[dict], manifest: list[dict]) -> dict:
    """Resolve struck CC holdings to their target articles and mark them on the
    *chunk* metadata (so both the vector index and the KG inherit it — invariant
    2). A **whole-article** strike sets `repealed`/`status`/`valid_to`; a
    **partial** strike (only a став/тачка struck) leaves the article in force and
    records `cc_note`/`cc_struck_part` — never repealing the whole article, and
    never the sibling articles whose numbers were the став/тачка digits (the old
    bug). Operates on chunks.jsonl-shaped dicts; idempotent. Returns counts."""
    law_keys = build_law_keys({m["slug"]: _law_name(m) for m in manifest})
    by_law_number: dict[tuple[str, str], dict] = {}
    for c in chunk_dicts:
        md = c.get("metadata", {})
        if md.get("is_version_slice") or md.get("is_annex"):
            continue
        by_law_number[(md.get("law_slug", ""), _clan_number(c["chunk_id"]))] = c
    full = partial = 0
    for c in chunk_dicts:
        if not c["chunk_id"].endswith("/holding") or c.get("metadata", {}).get("cc_outcome") != CC_STRUCK:
            continue
        text = c.get("text", "") or ""
        dt = cc_decision_date(text)
        for law_phrase, arts in cc_struck_targets(text):
            slug = _resolve_law(law_phrase, law_keys)
            if not slug:
                continue
            for art_num, part in arts:
                tgt = by_law_number.get((slug, art_num))
                if not tgt:
                    continue
                md = tgt.setdefault("metadata", {})
                if part is None:  # whole-article strike → repeal
                    md["repealed"] = True
                    md["status"] = CC_STRUCK_STATUS
                    if dt:  # never write "" — Provenance.valid_to is date|None
                        tgt.setdefault("provenance", {})["valid_to"] = str(dt)
                    full += 1
                else:  # partial strike → article stays in force, carries a note
                    note = f"Уставни суд ({dt or '?'}): неуставан {part}"
                    # idempotent: incremental re-ingests re-run this pass over
                    # chunks that already carry the note — don't append twice
                    if note not in (md.get("cc_note") or ""):
                        md["cc_note"] = (
                            f"{md['cc_note']}; {note}" if md.get("cc_note") else note
                        )
                        partial += 1
                    md["cc_struck_part"] = part
    return {"cc_full": full, "cc_partial": partial}


# Defined-term extraction for the *term-aware* DEFINED_IN edge (P3). The two
# dominant Serbian definitions forms: a quoted term („појам") and a leading
# noun-phrase before је/значи/подразумева at a list-item start („1) адаптација
# јесте…"). Terms < 4 chars are dropped (too generic to be a reliable signal).
# lowercase Cyrillic class + IGNORECASE (uppercase Serbian letters are not a
# contiguous range with а-џ, so [А-Џ] is invalid — case-fold instead).
_DEF_TERM_QUOTED = re.compile(r"[„\"”“]([а-џ][а-џ \-]{2,60}?)[“”\"]", re.IGNORECASE)
_DEF_TERM_LEAD = re.compile(
    r"(?m)^\s*(?:\d+[.)]\s*|[-–—]\s*)?([а-џ][а-џ \-]{2,50}?)\s+"
    r"(?:је|јесте|значи|представља|подразумева(?:ју)?|обухвата|сматра\s+се)\b",
    re.IGNORECASE,
)


def _defined_terms(text: str) -> set[str]:
    """The terms a definitions article actually defines (lower-cased)."""
    terms: set[str] = set()
    for rx in (_DEF_TERM_QUOTED, _DEF_TERM_LEAD):
        for m in rx.finditer(text or ""):
            t = m.group(1).strip().lower()
            if 4 <= len(t) <= 60:
                terms.add(t)
    return terms


def _term_stem(term: str) -> str:
    """Crude stem so an inflected use matches the nominative defined form
    (Serbian is heavily inflected — „адаптација" appears as „адаптацију",
    „адаптације" …). Trim the last 2 chars of a word ≥6 long; the last token of
    a multi-word term carries the inflection."""
    head, _, last = term.rpartition(" ")
    last = last[:-2] if len(last) >= 6 else last
    return f"{head} {last}".strip()


def structural_edges(chunks: list) -> dict[str, list[tuple[str, str]]]:
    """NEXT (consecutive in-force članci) + DEFINED_IN (→ definitions article).
    Version slices are skipped — structure is over the current text.

    DEFINED_IN is **term-aware** (P3): an article links to its law's definitions
    article only when its text actually uses a term defined there — not the old
    law-global "every article → the glossary", which inflated graph reach with a
    signal-free edge. **Fallback:** if no terms could be extracted from a
    definitions article (parser miss), it reverts to the law-global link for that
    article, so extraction failure never drops below the prior recall."""
    next_edges: list[tuple[str, str]] = []
    defined_in: list[tuple[str, str]] = []
    by_law: dict[str, list] = {}
    for c in chunks:
        if c.metadata.get("is_version_slice") or c.metadata.get("is_annex"):
            continue
        by_law.setdefault(c.metadata.get("law_slug", ""), []).append(c)
    for law_chunks in by_law.values():
        defs = [c for c in law_chunks if c.metadata.get("is_definitions")]
        defs_stems = {
            d.chunk_id: {_term_stem(t) for t in _defined_terms(getattr(d, "text", "") or "")}
            for d in defs
        }
        for i, c in enumerate(law_chunks):
            if i + 1 < len(law_chunks):
                next_edges.append((c.chunk_id, law_chunks[i + 1].chunk_id))
            ctext = (getattr(c, "text", "") or "").lower()
            for d in defs:
                if d.chunk_id == c.chunk_id:
                    continue
                stems = defs_stems[d.chunk_id]
                # term-aware (stem) link; law-global fallback when no terms parsed
                if not stems or any(s in ctext for s in stems):
                    defined_in.append((c.chunk_id, d.chunk_id))
    return {"NEXT": next_edges, "DEFINED_IN": defined_in}


def graph_stats(node_count: int, out_degrees: list[int], cross_law_edges: int,
                closure: dict | None = None, expand_degrees: list[int] | None = None) -> dict:
    """`out_degrees` is the UPUĆUJE_NA (numbered cross-reference) layer — the
    intrinsic, regex-extractable citation web. `expand_degrees`, when given, is
    the layer the graph *arm* actually traverses (UPUĆUJE_NA ∪ DEFINED_IN); the
    `expansion_*` fields report it so "isolated" isn't read as "the arm can't
    expand here" — it expands via DEFINED_IN on every article in a law that has
    a definitions article. NEXT is built but the arm doesn't traverse it, so it
    is deliberately excluded here."""
    total = sum(out_degrees)
    isolated = sum(1 for d in out_degrees if d == 0)
    stats = {
        "nodes": node_count,
        "semantic_edges": total,
        "mean_out_degree": round(total / node_count, 3) if node_count else 0.0,
        "isolated_nodes": isolated,
        "isolated_pct": round(100 * isolated / node_count, 1) if node_count else 0.0,
        "cross_law_edges": cross_law_edges,
        "cross_law_pct": round(100 * cross_law_edges / total, 1) if total else 0.0,
        "max_out_degree": max(out_degrees) if out_degrees else 0,
    }
    if expand_degrees is not None:
        etotal = sum(expand_degrees)
        eiso = sum(1 for d in expand_degrees if d == 0)
        stats.update({
            "expansion_edges": etotal,
            "expansion_mean_out_degree": round(etotal / node_count, 3) if node_count else 0.0,
            "expansion_isolated_pct": round(100 * eiso / node_count, 1) if node_count else 0.0,
            "arm_reachable_pct": round(100 * (node_count - eiso) / node_count, 1) if node_count else 0.0,
        })
    if closure:
        stats.update(closure)
    return stats


# ---- schema --------------------------------------------------------------
# One table per node/edge type; STRICT + endpoint FKs enforce the typed schema
# at the store level. Booleans are INTEGER 0/1 (consumers use truthiness).
_SCHEMA = """
CREATE TABLE akt(
  source_id TEXT PRIMARY KEY, slug TEXT NOT NULL, title TEXT NOT NULL,
  vrsta TEXT NOT NULL, role TEXT NOT NULL, cluster TEXT NOT NULL,
  materialized INTEGER NOT NULL, in_corpus_depth INTEGER NOT NULL,
  eli TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT NOT NULL,
  applies_from TEXT NOT NULL, repealed_by TEXT NOT NULL,
  down_weight INTEGER NOT NULL
) STRICT;
CREATE TABLE verzija(
  regactid TEXT PRIMARY KEY,
  akt_source TEXT NOT NULL REFERENCES akt(source_id),
  applies_from TEXT NOT NULL
) STRICT;
CREATE TABLE clan(
  chunk_id TEXT PRIMARY KEY,
  seq INTEGER NOT NULL,
  source_id TEXT NOT NULL REFERENCES akt(source_id),
  law_slug TEXT NOT NULL, number TEXT NOT NULL, fragment TEXT NOT NULL,
  text TEXT NOT NULL, chapter TEXT NOT NULL, structural_path TEXT NOT NULL,
  article_heading TEXT NOT NULL, status TEXT NOT NULL,
  valid_from TEXT NOT NULL, valid_to TEXT NOT NULL, applies_from TEXT NOT NULL,
  published TEXT NOT NULL, repealed INTEGER NOT NULL,
  is_definitions INTEGER NOT NULL, is_penalty INTEGER NOT NULL,
  version TEXT NOT NULL, is_version_slice INTEGER NOT NULL,
  cc_outcome TEXT NOT NULL, delegates_to_bylaw INTEGER NOT NULL
) STRICT;
CREATE TABLE sadrzi(
  src TEXT NOT NULL REFERENCES akt(source_id),
  dst TEXT NOT NULL REFERENCES clan(chunk_id)
) STRICT;
CREATE TABLE ima_verziju(
  src TEXT NOT NULL REFERENCES akt(source_id),
  dst TEXT NOT NULL REFERENCES verzija(regactid)
) STRICT;
CREATE TABLE u_verziji(
  src TEXT NOT NULL REFERENCES clan(chunk_id),
  dst TEXT NOT NULL REFERENCES verzija(regactid)
) STRICT;
CREATE TABLE upucuje_na(
  src TEXT NOT NULL REFERENCES clan(chunk_id),
  dst TEXT NOT NULL REFERENCES clan(chunk_id),
  mention TEXT NOT NULL
) STRICT;
CREATE TABLE next(
  src TEXT NOT NULL REFERENCES clan(chunk_id),
  dst TEXT NOT NULL REFERENCES clan(chunk_id)
) STRICT;
CREATE TABLE defined_in(
  src TEXT NOT NULL REFERENCES clan(chunk_id),
  dst TEXT NOT NULL REFERENCES clan(chunk_id)
) STRICT;
CREATE TABLE pravni_osnov_za(
  src TEXT NOT NULL REFERENCES akt(source_id),
  dst TEXT NOT NULL REFERENCES akt(source_id)
) STRICT;
CREATE TABLE ocena_ustavnosti(
  src TEXT NOT NULL REFERENCES akt(source_id),
  dst TEXT NOT NULL REFERENCES akt(source_id),
  outcome TEXT NOT NULL
) STRICT;
CREATE TABLE ustavni_osnov(
  src TEXT NOT NULL REFERENCES akt(source_id),
  dst TEXT NOT NULL REFERENCES akt(source_id),
  down_weight INTEGER NOT NULL
) STRICT;
CREATE TABLE povezan_sa(
  src TEXT NOT NULL REFERENCES akt(source_id),
  dst TEXT NOT NULL REFERENCES akt(source_id),
  down_weight INTEGER NOT NULL
) STRICT;
CREATE TABLE uticao_na(
  src TEXT NOT NULL REFERENCES akt(source_id),
  dst TEXT NOT NULL REFERENCES akt(source_id),
  down_weight INTEGER NOT NULL
) STRICT;
-- serve-time lookup paths (both endpoint columns on undirected relations)
CREATE INDEX idx_akt_slug ON akt(slug);
CREATE INDEX idx_clan_law_slug ON clan(law_slug);
CREATE INDEX idx_sadrzi_src ON sadrzi(src);
CREATE INDEX idx_upucuje_src ON upucuje_na(src);
CREATE INDEX idx_upucuje_dst ON upucuje_na(dst);
CREATE INDEX idx_defined_in_src ON defined_in(src);
CREATE INDEX idx_pravni_src ON pravni_osnov_za(src);
CREATE INDEX idx_ocena_src ON ocena_ustavnosti(src);
CREATE INDEX idx_povezan_src ON povezan_sa(src);
CREATE INDEX idx_povezan_dst ON povezan_sa(dst);
"""

# reference edge type → whether it carries an outcome / down_weight property
_PLAIN_REF = {"PRAVNI_OSNOV_ZA"}
_OUTCOME_REF = {"OCENA_USTAVNOSTI"}
_WEIGHTED_REF = {"USTAVNI_OSNOV", "POVEZAN_SA", "UTICAO_NA"}


def _clan_number(chunk_id: str) -> str:
    m = _CLAN_NUM_RE.search(chunk_id)
    return m.group(1) if m else ""


class KGStore:
    """Read-only handle over the KG SQLite file. Stdlib `sqlite3` connections
    are not shareable across threads, and the FastAPI app serves arms from a
    threadpool while bench batches run on a background thread — so the store
    opens one read-only connection per thread (thread-local) and returns plain
    dict rows (arm consumers call `.get()`; booleans arrive as 0/1)."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = str(path or KG_PATH)
        self._local = threading.local()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            try:
                conn = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
                # probe now: an un-smudged Git-LFS pointer file *connects* fine
                # and only fails on first query ("file is not a database") —
                # surface an actionable message instead of a bare mid-request 500
                conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
            except sqlite3.Error as exc:
                raise RuntimeError(
                    f"KG store unavailable at {self._path} ({exc}). Fresh checkout? "
                    "Run `git lfs install && git lfs pull` to fetch it, or rebuild "
                    "offline with `python -m lexsr.kg`."
                ) from exc
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def query(self, sql: str, params: dict | tuple = ()) -> list[dict]:
        return [dict(r) for r in self._conn().execute(sql, params)]


def open_connection() -> KGStore:
    return KGStore()


def _fresh_db() -> sqlite3.Connection:
    KG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # -journal is the sidecar a crashed DELETE-mode build actually leaves; a
    # stale hot journal beside a freshly-created db is the classic corruption
    # hazard. -wal/-shm covered for completeness.
    for suffix in ("", "-wal", "-shm", "-journal"):
        KG_PATH.with_name(KG_PATH.name + suffix).unlink(missing_ok=True)
    conn = sqlite3.connect(str(KG_PATH))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    return conn


def build_kg() -> dict:
    manifest = json.loads((CORPUS_DIR / "manifest.json").read_text(encoding="utf-8"))
    chunks = load_chunks()
    refs_path = CORPUS_DIR / "references.jsonl"
    ref_edges = [json.loads(line) for line in refs_path.open(encoding="utf-8")] \
        if refs_path.exists() else []

    conn = _fresh_db()
    slug_to_source = {m["slug"]: m["source_id"] for m in manifest}

    # --- akt nodes (every act: seed / backbone / referenced / stub) ----------
    conn.executemany(
        "INSERT INTO akt VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (m["source_id"], m.get("slug", ""), m.get("title", ""),
             m.get("vrsta", "zakon"), m.get("role", "seed"), m.get("cluster", ""),
             int(bool(m.get("materialized", True))), int(m.get("in_corpus_depth", 0)),
             m.get("eli") or "", m.get("law_valid_from", ""), m.get("law_valid_to", ""),
             m.get("applies_from", ""), m.get("law_repealed_by") or "",
             int(bool(m.get("down_weight_in_graph", False))))
            for m in manifest
        ],
    )

    # --- verzija nodes + akt-[ima_verziju]->verzija ---------------------------
    # Per-version start dates are DERIVED, never fabricated (invariant 2): a
    # version slice's valid_from is when that version began applying; the act's
    # current version (identified via the non-slice chunks) gets the act-level
    # applies_from; anything else is honestly unknown → "". (Previously every
    # version got the *current* consolidation's date — wrong for all but one.)
    version_start: dict[str, str] = {}
    current_reg_by_act: dict[str, str] = {}
    for c in chunks:
        ver = c.provenance.version or ""
        if not ver:
            continue
        if c.metadata.get("is_version_slice"):
            vf = str(c.provenance.valid_from or "")
            if vf:
                prev = version_start.get(ver, "")
                version_start[ver] = min(prev, vf) if prev else vf
        else:
            current_reg_by_act.setdefault(c.provenance.source_id, ver)
    verzija_ids: set[str] = set()
    verzija_rows: list[tuple] = []
    ima_verziju_rows: list[tuple] = []
    for m in manifest:
        for reg in m.get("versions", []) or []:
            if reg in verzija_ids:
                continue
            verzija_ids.add(reg)
            if reg == current_reg_by_act.get(m["source_id"]):
                applies = m.get("applies_from", "") or version_start.get(reg, "")
            else:
                applies = version_start.get(reg, "")
            verzija_rows.append((reg, m["source_id"], applies))
            ima_verziju_rows.append((m["source_id"], reg))
    conn.executemany("INSERT INTO verzija VALUES (?,?,?)", verzija_rows)
    conn.executemany("INSERT INTO ima_verziju VALUES (?,?)", ima_verziju_rows)

    # --- clan nodes + sadrzi + u_verziji --------------------------------------
    # `seq` is the load_chunks() position = document order → the deterministic
    # ORDER BY key of every truncated expansion query.
    by_law_number: dict[tuple[str, str], str] = {}
    clan_rows: list[tuple] = []
    sadrzi_rows: list[tuple] = []
    u_verziji_rows: list[tuple] = []
    for seq, c in enumerate(chunks):
        meta = c.metadata
        sid = c.provenance.source_id
        number = _clan_number(c.chunk_id)
        if not meta.get("is_version_slice") and not meta.get("is_annex"):
            by_law_number[(meta.get("law_slug", ""), number)] = c.chunk_id
        clan_rows.append((
            c.chunk_id, seq, sid, meta.get("law_slug", ""), number,
            c.provenance.fragment or "", c.text, meta.get("chapter") or "",
            meta.get("structural_path", ""), meta.get("article_heading", ""),
            meta.get("status", ""), str(c.provenance.valid_from or ""),
            str(c.provenance.valid_to or ""), meta.get("applies_from", ""),
            meta.get("published", ""), int(bool(meta.get("repealed", False))),
            int(bool(meta.get("is_definitions", False))),
            int(bool(meta.get("is_penalty", False))), c.provenance.version or "",
            int(bool(meta.get("is_version_slice", False))), meta.get("cc_outcome", ""),
            int(bool(meta.get("delegates_to_bylaw", False))),
        ))
        sadrzi_rows.append((sid, c.chunk_id))
        ver = c.provenance.version or ""
        if ver in verzija_ids:
            u_verziji_rows.append((c.chunk_id, ver))
    conn.executemany(
        "INSERT INTO clan VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", clan_rows)
    conn.executemany("INSERT INTO sadrzi VALUES (?,?)", sadrzi_rows)
    conn.executemany("INSERT INTO u_verziji VALUES (?,?)", u_verziji_rows)

    # --- intra-law cross-references (regex) + structural edges ---------------
    law_keys = build_law_keys({m["slug"]: _law_name(m) for m in manifest})

    edge_count = 0
    cross_law = 0
    out_degree: dict[str, int] = {}
    upucuje_rows: list[tuple] = []
    for c in chunks:
        if c.metadata.get("is_version_slice") or c.metadata.get("is_annex"):
            continue
        src_slug = c.metadata.get("law_slug", "")
        for target_slug, number, mention in _extract_refs(c.text, law_keys=law_keys):
            target_id = by_law_number.get((target_slug or src_slug, number))
            if target_id is None or target_id == c.chunk_id:
                continue
            upucuje_rows.append((c.chunk_id, target_id, mention))
            edge_count += 1
            out_degree[c.chunk_id] = out_degree.get(c.chunk_id, 0) + 1
            if target_slug and target_slug != src_slug:
                cross_law += 1
    conn.executemany("INSERT INTO upucuje_na VALUES (?,?,?)", upucuje_rows)

    struct = structural_edges(chunks)
    conn.executemany("INSERT INTO next VALUES (?,?)", struct["NEXT"])
    conn.executemany("INSERT INTO defined_in VALUES (?,?)", struct["DEFINED_IN"])

    # --- constitutional invalidation: a struck CC decision repeals its
    # target article — but only a WHOLE-article strike sets status/valid_to; a
    # partial strike (став/тачка) leaves the article in force (the fixed parser
    # no longer mistakes став/тачка digits for sibling article numbers). Gated on
    # in-corpus resolution (invariant 2). Chunks loaded here already carry these
    # flags when ingest ran `apply_cc_invalidation`; this pass keeps a standalone
    # `python -m lexsr.kg` correct and is idempotent.
    cc_invalidated = 0
    for c in chunks:
        if not c.chunk_id.endswith("/holding") or c.metadata.get("cc_outcome") != CC_STRUCK:
            continue
        dt = cc_decision_date(c.text)
        for law_phrase, arts in cc_struck_targets(c.text):
            slug = _resolve_law(law_phrase, law_keys)
            if not slug:
                continue
            for art_num, part in arts:
                if part is not None:  # partial strike → article stays in force
                    continue
                tgt = by_law_number.get((slug, art_num))
                if not tgt:
                    continue
                conn.execute(
                    "UPDATE clan SET status = ?, valid_to = ?, repealed = 1 "
                    "WHERE chunk_id = ?",
                    (CC_STRUCK_STATUS, str(dt or ""), tgt),
                )
                cc_invalidated += 1

    # --- authoritative typed reference edges (akt→akt) ------------------------
    akt_ids = {m["source_id"] for m in manifest}
    ref_edge_count = 0
    typed_rows: dict[str, list[tuple]] = {
        "pravni_osnov_za": [], "ocena_ustavnosti": [],
        "ustavni_osnov": [], "povezan_sa": [], "uticao_na": [],
    }
    for e in ref_edges:
        edge = e["edge"]
        frm = slug_to_source.get(e.get("from_slug", ""), e.get("from"))
        to = slug_to_source.get(e["to"], e["to"]) if e.get("in_corpus") else e["to"]
        if frm not in akt_ids or to not in akt_ids:
            continue
        if edge in _OUTCOME_REF:
            # CC outcome lives on the materialized holding chunk
            typed_rows["ocena_ustavnosti"].append((frm, to, ""))
        elif edge in _PLAIN_REF:
            typed_rows["pravni_osnov_za"].append((frm, to))
        elif edge in _WEIGHTED_REF:
            typed_rows[edge.lower()].append((frm, to, int(bool(e.get("down_weight", False)))))
        else:
            continue
        ref_edge_count += 1
    conn.executemany("INSERT INTO pravni_osnov_za VALUES (?,?)", typed_rows["pravni_osnov_za"])
    for table in ("ocena_ustavnosti", "ustavni_osnov", "povezan_sa", "uticao_na"):
        conn.executemany(f"INSERT INTO {table} VALUES (?,?,?)", typed_rows[table])
    conn.commit()

    # --- count gate: the store provably holds the graph the stats describe ---
    expected = {
        "akt": len(manifest), "verzija": len(verzija_rows), "clan": len(clan_rows),
        "sadrzi": len(sadrzi_rows), "ima_verziju": len(ima_verziju_rows),
        "u_verziji": len(u_verziji_rows), "upucuje_na": edge_count,
        "next": len(struct["NEXT"]), "defined_in": len(struct["DEFINED_IN"]),
        **{t: len(rows) for t, rows in typed_rows.items()},
    }
    mismatches = {
        t: (n, conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0])
        for t, n in expected.items()
        if conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] != n
    }
    if mismatches:
        raise RuntimeError(f"[kg] count gate failed (expected, stored): {mismatches}")
    conn.close()

    # closure split: completeness (target is any node) vs materialized
    # (target is retrievable) vs seed-to-seed (target is a pinned law).
    materialized_sids = {m["source_id"] for m in manifest if m.get("materialized")}

    def _tgt(e: dict) -> str:
        return slug_to_source.get(e["to"], e["to"]) if e.get("in_corpus") else e["to"]

    n = len(ref_edges)
    seed = sum(1 for e in ref_edges if e.get("in_corpus"))
    complete = sum(1 for e in ref_edges if _tgt(e) in akt_ids)
    materialized = sum(1 for e in ref_edges if _tgt(e) in materialized_sids)
    pct = lambda a: round(100 * a / n, 1) if n else 0.0  # noqa: E731
    closure = {
        "reference_edges": n,
        "in_corpus_reference_edges": seed,
        "closure_pct": pct(seed),  # seed-to-seed (kept key)
        "graph_completeness_pct": pct(complete),  # target is a node (stub or materialized)
        "materialized_target_pct": pct(materialized),  # target is retrievable content
        "stub_nodes": sum(1 for m in manifest if m.get("role") == "stub"),
    }
    clan_chunks = [c for c in chunks
                   if not c.metadata.get("is_version_slice") and not c.metadata.get("is_annex")]
    # expansion layer = what the graph arm actually traverses (UPUĆUJE_NA ∪ DEFINED_IN)
    expand_degree = dict(out_degree)
    for a, _b in struct["DEFINED_IN"]:
        expand_degree[a] = expand_degree.get(a, 0) + 1
    stats = graph_stats(
        len(clan_chunks), [out_degree.get(c.chunk_id, 0) for c in clan_chunks], cross_law, closure,
        expand_degrees=[expand_degree.get(c.chunk_id, 0) for c in clan_chunks],
    )
    (CORPUS_DIR / "graph_stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "akti": len(manifest), "verzije": len(verzija_ids), "clanovi": len(chunks),
        "upucuje_na": edge_count, "next": len(struct["NEXT"]),
        "defined_in": len(struct["DEFINED_IN"]), "reference_edges": ref_edge_count,
        "cc_invalidated": cc_invalidated, **stats,
    }
    print(f"[kg] done: {summary}", flush=True)
    return summary


if __name__ == "__main__":
    sys.exit(0 if build_kg()["clanovi"] > 0 else 1)
