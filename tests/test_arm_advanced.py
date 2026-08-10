"""Advanced-arm retrieval contract — focus on the temporal-filter starvation
guard (rag_arms.md §3): the as_of filter must not empty the context."""

from datetime import date

from lexsr.arms.advanced import AdvancedArm
from lexsr.arms.base import Timer


def _row(clan: str, valid_to: str) -> dict:
    return {
        "chunk_id": f"sgrs/zakon/2005/24/clan-{clan}",
        "law_slug": "zakon-o-radu",
        "source_title": "ЗАКОН о раду",
        "fragment": f"član {clan}",
        "text": f"Текст члана {clan}.",
        "valid_from": "2005-03-23",
        "valid_to": valid_to,
        "repealed": bool(valid_to),
        "url": "",
        "version": "",
        "_distance": 0.1,
    }


class FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def where(self, clause):
        # mirrors the LanceDB predicates the arms use: the slice-flood guard
        # (`is_version_slice = false`) and per-act scoping (`source_id = '…'`)
        if "is_version_slice" in clause:
            self._rows = [r for r in self._rows if not r.get("is_version_slice")]
        if "source_id = '" in clause:
            sid = clause.split("'")[1]
            self._rows = [r for r in self._rows if r.get("source_id", "") == sid]
        return self

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    def to_list(self):
        return list(self._rows)


class FakeTable:
    def __init__(self, rows):
        self.rows = rows

    def search(self, query=None, **kwargs):
        return FakeQuery(list(self.rows))


class FakeEmbedder:
    def embed(self, texts, *, task_type):
        class B:
            vectors = [[0.0]]
        return B()


# all three candidates were repealed in 2010 → out of force on a 2020 as_of
ROWS = [_row(c, "2010-01-01") for c in ("90", "91", "92")]


def test_temporal_filter_does_not_starve_generation():
    arm = AdvancedArm(llm=None, embedder=FakeEmbedder(), table=FakeTable(ROWS))
    retrieved, extras = arm.retrieve("питање?", as_of=date(2020, 1, 1), timer=Timer())
    # filter would drop everything; the guard keeps the top candidates instead
    assert extras["temporal_filter_starved"] is True
    assert len(retrieved) > 0  # context preserved, validity annotated downstream


def test_in_force_candidates_pass_filter_normally():
    rows = [_row(c, "") for c in ("90", "91")]  # still in force
    arm = AdvancedArm(llm=None, embedder=FakeEmbedder(), table=FakeTable(rows))
    retrieved, extras = arm.retrieve("питање?", as_of=date(2020, 1, 1), timer=Timer())
    assert extras["temporal_filter_starved"] is False
    assert {r.fragment for r in retrieved} == {"član 90", "član 91"}


def test_slice_flood_does_not_starve_the_vector_leg():
    """The paušal regression (2026-07-29): a heavily-amended article's dated
    @version slices flooded the vector top-k; post-hoc route_slices then dropped
    them all, leaving the leg empty and the arm abstaining. With no as_of the
    arms must exclude slices AT SEARCH TIME so current rows fill the pool."""
    slices = [
        {**_row("40", ""), "chunk_id": f"sgrs/zakon/2001/24/clan-40@{reg}",
         "is_version_slice": True}
        for reg in ("420191", "427685", "433484")
    ]
    current = _row("40", "")
    arm = AdvancedArm(llm=None, embedder=FakeEmbedder(),
                      table=FakeTable(slices + [current]))
    retrieved, _ = arm.retrieve("питање?", as_of=None, timer=Timer())
    ids = {r.chunk_id for r in retrieved}
    assert current["chunk_id"] in ids  # the current text survives the flood
    assert not any("@" in cid for cid in ids)  # no dated slices without as_of


def test_slices_stay_retrievable_with_as_of():
    """With an as_of the slices ARE the point — in_force_on picks the right one."""
    old = {**_row("40", "2010-01-01"), "chunk_id": "sgrs/zakon/2001/24/clan-40@420191",
           "is_version_slice": True, "valid_from": "2005-03-23"}
    current = {**_row("40", ""), "valid_from": "2010-01-01"}
    arm = AdvancedArm(llm=None, embedder=FakeEmbedder(), table=FakeTable([old, current]))
    retrieved, _ = arm.retrieve("питање?", as_of=date(2008, 6, 1), timer=Timer())
    ids = {r.chunk_id for r in retrieved}
    assert old["chunk_id"] in ids  # the historical slice answers the dated question
    assert current["chunk_id"] not in ids  # not yet in force on the as_of


def test_graph_structure_off_reproduces_advanced():
    """The superset/ladder property (rag_arms §1/§3): the graph arm = advanced's
    smart backbone (its seeds) + structural expansion, so with **every structure
    toggle off** it must reduce to advanced exactly — same backbone, same chunks.
    A built-in correctness check on the 'graph ⊇ advanced' claim."""
    from lexsr.arms.base import AblationConfig
    from lexsr.arms.graph import GraphArm

    rows = [_row(c, "") for c in ("90", "91", "92", "93", "94")]  # 5 in-force, < TOP_K → no rerank LLM
    # Identical backbone config; rerank/diversity off so no LLM call and no
    # same-act cap surprise — the equivalence is a property of the shared backbone.
    backbone = dict(hybrid=True, temporal_filter=True, diversity_cap=False, rerank=False)
    adv = AdvancedArm(
        llm=None, embedder=FakeEmbedder(), table=FakeTable(rows), config=AblationConfig(**backbone)
    )
    graph = GraphArm(
        llm=None, embedder=FakeEmbedder(), table=FakeTable(rows), kg_conn=None,
        config=AblationConfig(
            **backbone, graph_expand=False, bylaw_hop=False, cc_path=False, related_law_hop=False
        ),
    )
    a_chunks, _ = adv.retrieve("питање?", as_of=None, timer=Timer())
    g_chunks, _ = graph.retrieve("питање?", as_of=None, timer=Timer())
    assert [c.chunk_id for c in g_chunks] == [c.chunk_id for c in a_chunks]
    assert len(a_chunks) == 5  # all seeds, no structure added
