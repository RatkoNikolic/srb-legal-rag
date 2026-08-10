"""The srb-legal-rag ingestion pipeline — triggerable, incremental.

Builds the L0–L3 knowledge base described in
`docs/engineering/ingestion_pipeline.md`: typed `Akt` / `Verzija` / `Clan`,
multi-version article slices (Fork B), the authoritative typed reference graph
with stub-vs-materialized nodes under the bounded traversal policy, and the
closure metric.

Stages (§14): resolve → fetch versions → parse → version-diff → chunk (+ new
metadata) → typed references (stub/materialized) → write storage.

Storage (data/corpus/, §11):
- manifest.json     — every act (seed + backbone + referenced): vrsta, role,
                      materialized?, in_corpus_depth, versions, ELI, counts
- <regActId>.html   — cached fetched text per version (free offline reparse)
- chunks.jsonl      — chunks for MATERIALIZED acts only (what gets embedded)
- references.jsonl  — ALL typed edges, incl. edges to stub targets
- graph_stats.json  — density + corpus-closure %

Triggerable — process any subset, merged into existing storage:
    python -m lexsr.ingest all                 # whole corpus
    python -m lexsr.ingest 1                    # one cluster
    python -m lexsr.ingest zakon-o-radu,pio     # specific laws (incremental)
    python -m lexsr.ingest 1 --embed --kg       # + re-embed ($) + rebuild KG
    python -m lexsr.ingest all --no-references   # seeds/backbone only, no L2/L3
"""

from __future__ import annotations

import json
import re
import sys
import traceback
from datetime import date

from kl_core.types import Chunk, Provenance
from lexsr import pis, registry
from lexsr.config import CORPUS_DIR
from lexsr.parser import ConsolidatedLaw, extract_cc_holding, parse_consolidated_html, to_chunks
from lexsr.registry import LawSpec, Role, VersionPolicy

# Bounded traversal (docs/ingestion_pipeline.md §5): fetch one typed hop, then
# close the induced subgraph and stop (no depth-2). Record edges to full closure.
# ADR-9 (2026-06-21): the bylaw cap is abolished — all reachable PRAVNI_OSNOV_ZA
# bylaws are materialized (they are all HTML; a missing bylaw is unrecoverable at
# inference). CC decisions: all (tiny, validity). Tangential related/affected:
# edges only (stub unless already in-corpus). `bylaw_cap` stays as an optional
# knob (None = uncapped, the default).
DEFAULT_BYLAW_CAP: int | None = None  # ADR-9: uncapped
_MATERIALIZE = {"OCENA_USTAVNOSTI", "PRAVNI_OSNOV_ZA"}  # fetch + chunk these
_EDGE_ONLY = {"POVEZAN_SA", "UTICAO_NA", "USTAVNI_OSNOV"}  # stub unless already in-corpus

# regactid → slug for every seed/backbone law (in-corpus membership test).
_PINNED: dict[str, str] = {s.regactid: s.slug for s in registry.REGISTRY if s.regactid}

_WS_RE = re.compile(r"\s+")


def _norm(text: str) -> str:
    """Collapse whitespace for version-diff comparison (IP-3): a whitespace-only
    re-export of an unchanged article must not register as a changed slice."""
    return _WS_RE.sub(" ", text).strip()


def _source_id(eli: str | None, slug: str) -> str:
    if eli:
        m = re.search(r"/eli/rep/(.+?)/?$", eli)
        if m:
            return m.group(1).removesuffix("/reg").rstrip("/")
    return slug


def _resolve(spec: LawSpec) -> tuple[pis.LawView, str | None]:
    """LawView + canonical ELI for a registry spec. Repealed laws (eli_path)
    resolve via ELI; everything else via the stable law_guid (re-resolving the
    current regactid, since pins drift)."""
    if spec.eli_path:
        view = pis.resolve_eli(spec.eli_path)
        eli = pis.get_eli_link(view.law_guid) or (
            "https://pravno-informacioni-sistem.rs" + spec.eli_path
        )
        return view, eli
    if spec.law_guid:
        return pis.get_law_view(spec.law_guid), pis.get_eli_link(spec.law_guid)
    assert spec.query is not None
    hits = [h for h in pis.search_registry(spec.query) if h.doc_type == "Закон"]
    if not hits:
        raise LookupError(f"no registry hit for {spec.query!r}")
    return pis.get_law_view(hits[0].law_guid), pis.get_eli_link(hits[0].law_guid)


def _versions_for(view: pis.LawView, policy: VersionPolicy) -> list[pis.LawVersion]:
    """Versions to fetch, newest first. CURRENT_ONLY → just the current text;
    CHANGED_SLICES / FULL_HISTORY → current + all prior consolidations."""
    if policy is VersionPolicy.CURRENT_ONLY:
        return [view.current]
    ordered = [view.current, *view.previous]
    # newest first; versions lacking an applies_from sort last (stable)
    return sorted(ordered, key=lambda v: (v.applies_from or date.min), reverse=True)


def _cache_html(regactid: str, html: str) -> None:
    (CORPUS_DIR / f"{regactid}.html").write_text(html, encoding="utf-8")


# Cache reuse is keyed by regactid (a version id): a reconsolidation mints a new
# regactid → cache miss → fresh fetch, so reusing a cached `<regactid>.html` is
# always correct. Off by default (deterministic for tests); the CLI turns it on.
_USE_CACHE = False


def _cached_html(regactid: str) -> str | None:
    p = CORPUS_DIR / f"{regactid}.html"
    if _USE_CACHE and p.exists():
        text = p.read_text(encoding="utf-8")
        if text.strip():
            return text
    return None


def _stamp(chunk: Chunk, *, spec: LawSpec, depth: int, materialized: bool, vrsta: str) -> None:
    chunk.metadata.update(
        law_slug=spec.slug, cluster=spec.cluster, role=spec.role.value,
        vrsta=vrsta, in_corpus_depth=depth, materialized=materialized,
    )


def _law_chunks(spec: LawSpec, view: pis.LawView, eli: str | None, source_id: str) -> tuple[
    list[Chunk], dict
]:
    """Parse + chunk a seed/backbone law across its versions (Fork B).

    Current version → all članci (plain `…/clan-N` ids, the in-force slice).
    Prior versions (CHANGED_SLICES/FULL) → only članci whose text differs from
    the next-newer version, emitted as `…/clan-N@<regactid>` slices carrying the
    version's `[valid_from, valid_to]` interval (D-2 / §7).
    """
    versions = _versions_for(view, spec.version_policy)
    parsed: list[tuple[pis.LawVersion, ConsolidatedLaw]] = []
    for v in versions:
        html = _cached_html(v.regactid)
        if html is None:
            html = pis.fetch_consolidated_html(v)
            _cache_html(v.regactid, html)
        law = parse_consolidated_html(html, eli=eli)
        if not law.main_clanovi and v is versions[0]:
            raise ValueError("parsed 0 main članci — markup dialect not handled?")
        parsed.append((v, law))

    cur_v, cur_law = parsed[0]
    chunks = to_chunks(cur_law, source_id=source_id, applies_from=cur_v.applies_from)
    for c in chunks:
        c.metadata["version"] = cur_v.regactid
        c.provenance.version = cur_v.regactid
        _stamp(c, spec=spec, depth=0, materialized=True, vrsta=spec.vrsta)

    # Version slices: compare each older version to the one immediately newer.
    slice_count = 0
    for i in range(1, len(parsed)):
        (older_v, older_law) = parsed[i]
        (newer_v, newer_law) = parsed[i - 1]
        newer_by_num = {
            c.number: _norm("\n".join(s.text for s in c.stavovi)) for c in newer_law.main_clanovi
        }
        interval_from = older_v.applies_from
        interval_to = newer_v.applies_from
        for clan in older_law.main_clanovi:
            body = "\n".join(s.text for s in clan.stavovi)
            if (spec.version_policy is VersionPolicy.CHANGED_SLICES
                    and newer_by_num.get(clan.number) == _norm(body)):
                continue  # unchanged (whitespace-insensitive) → newer slice represents it
            cid = f"{source_id}/clan-{clan.number}@{older_v.regactid}"
            chunk = Chunk(
                chunk_id=cid, text=body or "(празан члан)",
                provenance=Provenance(
                    source_id=source_id, source_title=cur_law.title,
                    fragment=f"član {clan.number}", version=older_v.regactid,
                    valid_from=interval_from, valid_to=interval_to, url=eli,
                ),
                metadata={
                    "fragment": f"član {clan.number}", "version": older_v.regactid,
                    "is_version_slice": True, "stav_count": len(clan.stavovi),
                    "applies_from": str(interval_from or ""),
                    "contextual_prefix": f"član {clan.number} · {cur_law.title} "
                    f"(верзија {older_v.regactid}, важила {interval_from}–{interval_to})",
                },
            )
            _stamp(chunk, spec=spec, depth=0, materialized=True, vrsta=spec.vrsta)
            chunks.append(chunk)
            slice_count += 1

    record = {
        "slug": spec.slug, "cluster": spec.cluster, "role": spec.role.value,
        "vrsta": spec.vrsta, "materialized": True, "in_corpus_depth": 0,
        "title": cur_law.title, "registry_title": view.title, "law_guid": view.law_guid,
        "regactid": cur_v.regactid, "eli": eli, "source_id": source_id,
        "applies_from": str(cur_v.applies_from or ""),
        "law_published": str(cur_law.gazette_chain[0].published if cur_law.gazette_chain else ""),
        "law_valid_from": str(cur_law.valid_from or ""),
        "law_valid_to": str(cur_law.valid_to or ""),
        "law_repealed_by": cur_law.repealed_by,
        "gazette_chain": [g.issue for g in cur_law.gazette_chain],
        "versions": [v.regactid for v in versions],
        "version_policy": spec.version_policy.value,
        "main_clanovi": len(cur_law.main_clanovi),
        "repealed_clanovi": sum(
            1 for c in chunks
            if c.metadata.get("repealed") or c.metadata.get("status") == "repealed"
        ),
        "version_slices": slice_count,
        "chunks": len(chunks),
        "down_weight_in_graph": spec.down_weight_in_graph,
    }
    return chunks, record


def _materialize_reference(ref: pis.ActReference) -> tuple[list[Chunk], dict] | None:
    """Fetch + chunk a referenced act (depth-1). Bylaws → članak-level (Fork A,
    Option 1); CC decisions → a single holding-level chunk (Fork A)."""
    html = _cached_html(ref.ref_id)
    if html is None:
        html = pis.fetch_doc(ref.inline_url)
        if html.strip() == "" or "Dokument ne moze biti prikazan" in html or len(html) < 200:
            return None  # not retrievable as text (some slgl docs are PDF-only)
        _cache_html(ref.ref_id, html)
    source_id = f"ref/{ref.ref_id}"
    if ref.edge == "OCENA_USTAVNOSTI":
        outcome, holding = extract_cc_holding(html)
        chunk = Chunk(
            chunk_id=f"{source_id}/holding", text=holding,
            provenance=Provenance(source_id=source_id, source_title=ref.name,
                                  fragment="izreka", url=ref.inline_url),
            metadata={"cc_outcome": outcome, "fragment": "izreka",
                      "contextual_prefix": f"Одлука Уставног суда · {ref.name}"},
        )
        vrsta, chunks = "odluka_us", [chunk]
    else:  # bylaw — članak-level
        law = parse_consolidated_html(html)
        if not law.main_clanovi:
            return None
        chunks = to_chunks(law, source_id=source_id)
        vrsta = "podzakonski_akt"
    spec = LawSpec(slug=source_id, cluster="referenced", role=Role.SEED, vrsta=vrsta)
    for c in chunks:
        c.metadata["version"] = ref.ref_id
        _stamp(c, spec=spec, depth=1, materialized=True, vrsta=vrsta)
    record = {
        "slug": source_id, "cluster": "referenced", "role": "referenced", "vrsta": vrsta,
        "materialized": True, "in_corpus_depth": 1, "title": ref.name,
        "regactid": ref.ref_id, "source_id": source_id, "eli": ref.inline_url,
        "chunks": len(chunks),
    }
    return chunks, record


def _references(
    spec: LawSpec, view: pis.LawView, src_source_id: str, *, bylaw_cap: int | None
) -> tuple[list[Chunk], list[dict], list[dict]]:
    """Resolve, bound, and partition the law's typed references into edges +
    (materialized chunks / stub records). Returns (chunks, act_records, edges)."""
    refs = pis.get_references(view.current.regactid)
    chunks: list[Chunk] = []
    records: list[dict] = []
    edges: list[dict] = []
    seen_targets: set[str] = set()
    for edge, items in refs.items():
        # ADR-9: bylaws uncapped (bylaw_cap is None by default); the knob still
        # works if a cap is passed explicitly.
        cap = bylaw_cap if (edge == "PRAVNI_OSNOV_ZA" and bylaw_cap is not None) else None
        for i, ref in enumerate(items):
            in_corpus_slug = _PINNED.get(ref.ref_id)
            target_source = in_corpus_slug or f"ref/{ref.ref_id}"
            edges.append({
                "from": src_source_id, "to": target_source, "edge": edge,
                "ref_id": ref.ref_id, "name": ref.name,
                "in_corpus": in_corpus_slug is not None,
                "down_weight": spec.down_weight_in_graph,
            })
            if in_corpus_slug or ref.ref_id in seen_targets:
                continue  # already a node elsewhere
            # materialize? CC + bylaws: yes (bylaws uncapped, ADR-9); tangential: never
            do_fetch = edge in _MATERIALIZE and (cap is None or i < cap)
            if not do_fetch:
                records.append({  # stub
                    "slug": target_source, "cluster": "referenced", "role": "stub",
                    "vrsta": "akt", "materialized": False, "in_corpus_depth": 1,
                    "title": ref.name, "regactid": ref.ref_id, "source_id": target_source,
                    "eli": ref.inline_url, "chunks": 0,
                })
                seen_targets.add(ref.ref_id)
                continue
            try:
                got = _materialize_reference(ref)
            except Exception as exc:  # one bad doc must not sink the run
                got = None
                print(f"[ingest]     ! reference {ref.ref_id} failed: {exc}", flush=True)
            if got is None:
                records.append({  # demote to stub if not text-retrievable
                    "slug": target_source, "cluster": "referenced", "role": "stub",
                    "vrsta": "akt", "materialized": False, "in_corpus_depth": 1,
                    "title": ref.name, "regactid": ref.ref_id, "source_id": target_source,
                    "eli": ref.inline_url, "chunks": 0,
                })
            else:
                ref_chunks, rec = got
                chunks.extend(ref_chunks)
                records.append(rec)
            seen_targets.add(ref.ref_id)
    return chunks, records, edges


_TANGENTIAL_EDGES = {"POVEZAN_SA", "UTICAO_NA"}


def closure_metric(edges: list[dict]) -> dict:
    """The closure numbers behind the 'self-contained' claim (three
    distinct notions, reported separately so 9.3% isn't mistaken for the whole
    story):
    - `closure_pct` / `seed_to_seed_pct`: % of edges whose target is another
      *pinned seed/backbone law* (the strict number; kept under the old key).
    - `answer_relevant_*`: the same, but the denominator drops the tangential
      `POVEZAN_SA`/`UTICAO_NA` "see also" links (which dominate and can't be
      seed-to-seed by definition).
    - `by_edge_type`: per-category total / in-corpus / %.
    (Graph *completeness* — every target is a node, stub or materialized — is
    computed in `kg.build_kg` where the node set is known.)"""
    total = len(edges)
    in_corpus = sum(1 for e in edges if e["in_corpus"])
    by_edge: dict[str, dict] = {}
    for e in edges:
        d = by_edge.setdefault(e["edge"], {"total": 0, "in_corpus": 0})
        d["total"] += 1
        d["in_corpus"] += int(bool(e["in_corpus"]))
    rel = [e for e in edges if e["edge"] not in _TANGENTIAL_EDGES]
    rel_in = sum(1 for e in rel if e["in_corpus"])
    pct = lambda a, b: round(100 * a / b, 1) if b else 0.0  # noqa: E731
    return {
        "reference_edges": total,
        "in_corpus_edges": in_corpus,
        "closure_pct": pct(in_corpus, total),  # seed-to-seed (kept key)
        "seed_to_seed_pct": pct(in_corpus, total),
        "answer_relevant_edges": len(rel),
        "answer_relevant_in_corpus_pct": pct(rel_in, len(rel)),
        "by_edge_type": {
            k: {**v, "pct": pct(v["in_corpus"], v["total"])} for k, v in by_edge.items()
        },
    }


def _load_jsonl(name: str) -> list[dict]:
    path = CORPUS_DIR / name
    if not path.exists():
        return []
    return [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]


def _load_manifest() -> list[dict]:
    path = CORPUS_DIR / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def ingest(
    selector: str = "all", *, fetch_references: bool = True,
    bylaw_cap: int | None = DEFAULT_BYLAW_CAP, incremental: bool = True,
    use_cache: bool = False,
) -> dict:
    """Run the pipeline for `selector` (see module docstring), merging into
    existing storage when `incremental`. `use_cache` reuses cached
    `<regactid>.html` (safe — keyed by version id; see `_cached_html`). Offline-
    friendly: re-embed + KG rebuild are separate steps the caller triggers after."""
    global _USE_CACHE
    _USE_CACHE = use_cache
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    specs = registry.resolve_specs(selector)
    target_slugs = {s.slug for s in specs}

    # start from existing storage (incremental) minus the laws we're rebuilding;
    # the pre-filter snapshots stay around so a law whose re-ingest FAILS can be
    # restored instead of silently deleted from the corpus
    prior_manifest = _load_manifest() if incremental else []
    prior_chunks = _load_jsonl("chunks.jsonl") if incremental else []
    prior_edges = _load_jsonl("references.jsonl") if incremental else []
    manifest = [m for m in prior_manifest if m.get("slug") not in target_slugs]
    chunks_keep = [
        c for c in prior_chunks
        if c.get("metadata", {}).get("law_slug") not in target_slugs
    ]
    edges_keep = [e for e in prior_edges if e.get("from_slug") not in target_slugs]

    new_chunks: list[Chunk] = []
    new_records: list[dict] = []
    new_edges: list[dict] = []
    failures: list[dict] = []
    for spec in specs:
        print(f"[ingest] {spec.cluster} · {spec.slug} ({spec.role.value}) …", flush=True)
        try:
            view, eli = _resolve(spec)
            source_id = _source_id(eli, spec.slug)
            law_chunks, record = _law_chunks(spec, view, eli, source_id)
            new_chunks.extend(law_chunks)
            new_records.append(record)
            print(f"[ingest]   ✓ {record['title']} — {record['main_clanovi']} članci, "
                  f"{record['version_slices']} slices, {len(law_chunks)} chunks", flush=True)
            if fetch_references and spec.role is not Role.HUB:
                # hubs are materialized for closure but we don't fan out *from*
                # them (they'd pull in most of PIS); seeds + foundational do.
                ref_chunks, ref_records, edges = _references(
                    spec, view, source_id, bylaw_cap=bylaw_cap
                )
                for e in edges:
                    e["from_slug"] = spec.slug
                new_chunks.extend(ref_chunks)
                new_records.extend(ref_records)
                new_edges.extend(edges)
                print(f"[ingest]     refs: {len(edges)} edges, "
                      f"{sum(r['chunks'] for r in ref_records)} materialized chunks", flush=True)
        except Exception as exc:
            failures.append({"slug": spec.slug, "error": f"{type(exc).__name__}: {exc}",
                             "trace": traceback.format_exc(limit=3)})
            print(f"[ingest]   ✗ {spec.slug}: {exc}", flush=True)

    # a transient failure must not delete the law: restore its previous
    # manifest record, chunks, and edges wholesale (exit code still signals)
    failed_slugs = {f["slug"] for f in failures}
    if failed_slugs:
        manifest += [m for m in prior_manifest if m.get("slug") in failed_slugs]
        chunks_keep += [
            c for c in prior_chunks
            if c.get("metadata", {}).get("law_slug") in failed_slugs
        ]
        edges_keep += [e for e in prior_edges if e.get("from_slug") in failed_slugs]
        print(f"[ingest] ⚠ restored previous state for failed: {sorted(failed_slugs)}",
              flush=True)
    # the keep-filter drops re-ingested laws' annex chunks, and ingest does not
    # regenerate them — warn so the annex pass isn't forgotten
    dropped_annexes = sum(
        1 for c in prior_chunks
        if c.get("metadata", {}).get("law_slug") in (target_slugs - failed_slugs)
        and c.get("metadata", {}).get("is_annex")
    )
    if dropped_annexes:
        print(f"[ingest] ⚠ dropped {dropped_annexes} annex chunks of re-ingested laws — "
              "re-run `python -m lexsr.annex` to regenerate them", flush=True)

    # merge + dedupe referenced acts by source_id (a bylaw can be cited by many).
    # A MATERIALIZED record always beats a stub for the same act: law A may know
    # X only tangentially (stub) while law B materialized it — first-wins here
    # once left 40 acts stub-marked in the manifest with their chunks embedded,
    # making them structurally unreachable (`b.materialized = 1` filters).
    by_source: dict[str, dict] = {m["source_id"]: m for m in manifest}
    for rec in new_records:
        prior = by_source.get(rec["source_id"])
        if prior is None or (rec.get("materialized") and not prior.get("materialized")):
            by_source[rec["source_id"]] = rec
    manifest = list(by_source.values())

    seen_chunk_ids = {c["chunk_id"] for c in chunks_keep}
    merged_chunks = list(chunks_keep)
    for c in new_chunks:
        if c.chunk_id not in seen_chunk_ids:
            merged_chunks.append(c.model_dump(mode="json"))
            seen_chunk_ids.add(c.chunk_id)

    # constitutional invalidation — resolve struck CC holdings onto their
    # target articles' chunk metadata, so the vector index *and* the KG inherit
    # repeal status (invariant 2). Local import: kg pulls the index (lancedb).
    from lexsr import kg
    cc = kg.apply_cc_invalidation(merged_chunks, manifest)

    all_edges = edges_keep + new_edges
    _write_storage(manifest, merged_chunks, all_edges, failures)
    summary = {
        "laws": len([m for m in manifest if m["role"] != "stub"]),
        "stubs": len([m for m in manifest if m["role"] == "stub"]),
        "chunks": len(merged_chunks), "edges": len(all_edges),
        "failures": len(failures), **cc, **closure_metric(all_edges),
    }
    print(f"[ingest] done: {summary}", flush=True)
    return summary


def _write_storage(manifest: list[dict], chunks: list[dict], edges: list[dict],
                   failures: list[dict]) -> None:
    (CORPUS_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(CORPUS_DIR / "chunks.jsonl", "w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    with open(CORPUS_DIR / "references.jsonl", "w", encoding="utf-8") as f:
        for e in edges:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    # merge the closure keys into the existing stats — `lexsr.kg` owns the
    # graph-density keys, and clobbering them here left a closure-only subset
    # (stale until the next KG rebuild) in the file docs point reviewers at
    stats_path = CORPUS_DIR / "graph_stats.json"
    stats = json.loads(stats_path.read_text(encoding="utf-8")) if stats_path.exists() else {}
    stats.update(closure_metric(edges))
    stats_path.write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    (CORPUS_DIR / "failures.json").write_text(
        json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8")


def _main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    flags = {a for a in argv if a.startswith("--")}
    selector = args[0] if args else "all"
    summary = ingest(
        selector,
        fetch_references="--no-references" not in flags,
        incremental="--fresh" not in flags,
        use_cache="--no-cache" not in flags,  # CLI default: reuse cached HTML
    )
    if "--embed" in flags:
        from lexsr import index
        index.build_index()
    if "--kg" in flags:
        from lexsr import kg
        kg.build_kg()
    return 0 if summary["laws"] > 0 and summary["failures"] == 0 else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
