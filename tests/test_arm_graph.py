"""Graph-arm retrieval contract against a REAL (mini) KG store — the query-heavy
path (`graph.py` retrieve: 2-hop expansion, bylaw hop, CC path) previously had
no store-backed coverage at all. Also pins the SQL query contracts directly:
undirected UPUĆUJE_NA traversal, repealed-target exclusion, and the
deterministic document-order (seq) ORDER BY under LIMIT."""

from lexsr.arms.base import AblationConfig, Timer
from lexsr.arms.graph import _REF_QUERY, _RELATED_QUERY, GraphArm

LAW = "sgrs/zakon/2005/24"
BYLAW = "ref/900001"
CC = "ref/900002"


def _law_row(clan: str, **over) -> dict:
    row = {
        "chunk_id": f"{LAW}/clan-{clan}",
        "law_slug": "zakon-o-radu",
        "source_title": "ЗАКОН о раду",
        "fragment": f"član {clan}",
        "text": f"Текст члана {clan}.",
        "valid_from": "2005-03-23",
        "valid_to": "",
        "repealed": False,
        "url": "https://example.org/eli",
        "version": "",
        "vrsta": "zakon",
        "delegates_to_bylaw": True,
        "_distance": 0.1,
    }
    row.update(over)
    return row


class FakeQuery:
    """search() serves the base (vector-top-k) rows; .where("source_id = 'X'")
    switches to the scoped rows for that act — modelling the production split
    the bylaw hop exists for: the flat top-k doesn't surface bylaw chunks, the
    graph-scoped sub-search does."""

    def __init__(self, rows, scoped):
        self._rows, self._scoped = list(rows), scoped

    def where(self, clause):
        if "source_id = '" in clause:
            sid = clause.split("'")[1]
            self._rows = list(self._scoped.get(sid, []))
        if "is_version_slice" in clause:  # the slice-flood guard
            self._rows = [r for r in self._rows if not r.get("is_version_slice")]
        return self

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    def to_list(self):
        return list(self._rows)


class FakeTable:
    def __init__(self, rows, scoped=None):
        self.rows, self.scoped = rows, scoped or {}

    def search(self, query=None, **kwargs):
        return FakeQuery(self.rows, self.scoped)


class FakeEmbedder:
    def embed(self, texts, *, task_type):
        class B:
            vectors = [[0.0]]
        return B()


def _mini_store(make_kg):
    return make_kg(
        akti=[
            {"source_id": LAW, "slug": "zakon-o-radu", "title": "ЗАКОН о раду",
             "eli": "https://example.org/eli"},
            {"source_id": BYLAW, "slug": "pravilnik-x", "title": "ПРАВИЛНИК о тесту",
             "vrsta": "podzakonski_akt", "eli": "https://example.org/eli-pravilnik"},
            {"source_id": CC, "slug": "odluka-us-1", "title": "Одлука УС IУз-1/2020",
             "vrsta": "odluka_us", "eli": "https://example.org/eli-us"},
        ],
        clanovi=[
            {"chunk_id": f"{LAW}/clan-1", "source_id": LAW, "law_slug": "zakon-o-radu",
             "number": "1", "fragment": "član 1", "text": "Текст члана 1."},
            {"chunk_id": f"{LAW}/clan-2", "source_id": LAW, "law_slug": "zakon-o-radu",
             "number": "2", "fragment": "član 2", "text": "Текст члана 2."},
            {"chunk_id": f"{LAW}/clan-3", "source_id": LAW, "law_slug": "zakon-o-radu",
             "number": "3", "fragment": "član 3", "text": "Престао.", "repealed": 1,
             "valid_to": "2010-01-01"},
            {"chunk_id": f"{LAW}/clan-4", "source_id": LAW, "law_slug": "zakon-o-radu",
             "number": "4", "fragment": "član 4", "text": "Текст члана 4."},
            {"chunk_id": f"{LAW}/clan-9", "source_id": LAW, "law_slug": "zakon-o-radu",
             "number": "9", "fragment": "član 9", "text": "Значење појмова.",
             "is_definitions": 1},
            {"chunk_id": f"{CC}/holding", "source_id": CC, "law_slug": "odluka-us-1",
             "number": "", "fragment": "изрека", "text": "Утврђује се неуставност."},
        ],
        upucuje=[
            (f"{LAW}/clan-1", f"{LAW}/clan-2", "члан 2."),
            (f"{LAW}/clan-1", f"{LAW}/clan-3", "члан 3."),  # repealed target → excluded
            (f"{LAW}/clan-4", f"{LAW}/clan-1", "члан 1."),  # reverse edge → undirected pickup
        ],
        defined_in=[(f"{LAW}/clan-1", f"{LAW}/clan-9")],
        pravni_osnov_za=[(LAW, BYLAW)],
        ocena_ustavnosti=[(LAW, CC)],
    )


def test_graph_expansion_bylaw_and_cc_against_real_store(make_kg):
    store = _mini_store(make_kg)
    bylaw_chunk = _law_row("1", chunk_id=f"{BYLAW}/clan-1", law_slug="pravilnik-x",
                           source_title="", vrsta="podzakonski_akt",
                           delegates_to_bylaw=False, fragment="član 1 pravilnika")
    arm = GraphArm(
        llm=None, embedder=FakeEmbedder(),
        table=FakeTable([_law_row("1")], scoped={BYLAW: [bylaw_chunk]}),
        kg_conn=store,
        config=AblationConfig(rerank=False, related_law_hop=False),
    )
    retrieved, extras = arm.retrieve("питање?", as_of=None, timer=Timer())
    ids = {r.chunk_id for r in retrieved}
    stages = {r.chunk_id: r.stage for r in retrieved}

    # 1-hop UPUĆUJE_NA: directed target picked up, reverse edge traversed
    # undirected, repealed target excluded; DEFINED_IN pulls the glossary.
    assert f"{LAW}/clan-2" in ids and stages[f"{LAW}/clan-2"] == "graph-expand"
    assert f"{LAW}/clan-4" in ids  # cited-by (undirected)
    assert f"{LAW}/clan-9" in ids  # defined_in
    assert f"{LAW}/clan-3" not in ids  # repealed → never expanded into

    # law→bylaw hop: PRAVNI_OSNOV_ZA edge + scoped sub-search, titled from the akt
    bylaw_hits = [r for r in retrieved if r.stage == "graph-bylaw"]
    assert [r.chunk_id for r in bylaw_hits] == [f"{BYLAW}/clan-1"]
    assert bylaw_hits[0].source_title == "ПРАВИЛНИК о тесту"
    assert extras["bylaw_count"] == 1

    # CC path: OCENA_USTAVNOSTI → the decision's holding, provenance from the CC akt
    cc_hits = [r for r in retrieved if r.stage == "graph-cc"]
    assert [r.chunk_id for r in cc_hits] == [f"{CC}/holding"]
    assert cc_hits[0].url == "https://example.org/eli-us"
    assert extras["cc_count"] == 1


def test_ref_query_is_document_ordered_under_limit(make_kg):
    store = _mini_store(make_kg)
    rows = store.query(_REF_QUERY, {"cid": f"{LAW}/clan-1", "lim": 1_000_000})
    # deterministic document order (seq), not engine row order
    assert [r["chunk_id"] for r in rows] == [f"{LAW}/clan-2", f"{LAW}/clan-4"]
    # LIMIT keeps the lowest-seq neighbour — truncation is document-order-stable
    top = store.query(_REF_QUERY, {"cid": f"{LAW}/clan-1", "lim": 1})
    assert [r["chunk_id"] for r in top] == [f"{LAW}/clan-2"]
    assert top[0]["mention"] == "члан 2."


def test_related_query_guards_weight_direction_and_vrsta(make_kg):
    store = make_kg(
        akti=[
            {"source_id": "a", "slug": "a", "title": "A", "vrsta": "zakon"},
            {"source_id": "b", "slug": "b", "title": "B", "vrsta": "zakon"},
            {"source_id": "c", "slug": "c", "title": "C", "vrsta": "zakon", "materialized": 0},
            {"source_id": "d", "slug": "d", "title": "D", "vrsta": "podzakonski_akt"},
            {"source_id": "e", "slug": "e", "title": "E", "vrsta": "zakon"},
        ],
        povezan_sa=[
            ("a", "b", 0),  # kept
            ("c", "a", 0),  # stub target → dropped (undirected traversal still checked)
            ("a", "d", 0),  # bylaw → dropped (primary legislation only)
            ("e", "a", 1),  # down-weighted (hub) edge → dropped
        ],
    )
    rows = store.query(_RELATED_QUERY, {"sid": "a", "lim": 10})
    assert [r["source_id"] for r in rows] == ["b"]
