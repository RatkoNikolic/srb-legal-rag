"""Vector-reachability diagnostic: flat top-k surfacing a gold citation.
Fake embedder + table so the diagnostic is exercised without the live index."""

from lexsr import diagnose


class FakeEmbedder:
    def embed(self, texts, *, task_type):
        class _Batch:
            vectors = [[0.0, 0.0, 0.0]]
        return _Batch()


class FakeTable:
    def __init__(self, chunk_ids):
        self._ids = chunk_ids

    def search(self, _vector):
        return self

    def where(self, _clause):  # the slice-flood guard (mirrors the naive arm)
        return self

    def limit(self, _k):
        return self

    def to_list(self):
        return [{"chunk_id": cid} for cid in self._ids]


def test_single_cite_all_reachable_when_gold_in_topk():
    table = FakeTable(["src/x/clan-20", "src/y/clan-3"])
    result = diagnose.flat_reachable("q", ["src/x::član 20"], FakeEmbedder(), table)
    assert result == {"hits": ["src/x::član 20"], "misses": [], "all_reachable": True}


def test_single_cite_not_reachable_when_gold_absent():
    table = FakeTable(["src/z/clan-9", "src/y/clan-3"])
    result = diagnose.flat_reachable("q", ["src/x::član 20"], FakeEmbedder(), table)
    assert result == {"hits": [], "misses": ["src/x::član 20"], "all_reachable": False}


def test_multi_cite_partial_hit_is_not_all_reachable():
    # The cc_validity case: flat finds the article but misses the discriminating
    # izreka — all-of must report False (graph still has a chance) and name the miss.
    table = FakeTable(["src/x/clan-20", "src/y/clan-3"])
    result = diagnose.flat_reachable(
        "q", ["src/x::član 20", "src/x::izreka"], FakeEmbedder(), table
    )
    assert result == {
        "hits": ["src/x::član 20"],
        "misses": ["src/x::izreka"],
        "all_reachable": False,
    }


def test_multi_cite_all_present_is_all_reachable():
    table = FakeTable(["src/x/clan-20", "src/x/holding"])
    result = diagnose.flat_reachable(
        "q", ["src/x::član 20", "src/x::izreka"], FakeEmbedder(), table
    )
    assert result["all_reachable"] is True
    assert result["misses"] == []


def test_unparseable_citation_counts_as_miss():
    result = diagnose.flat_reachable("q", ["garbage"], FakeEmbedder(), FakeTable([]))
    assert result == {"hits": [], "misses": ["garbage"], "all_reachable": False}


def test_abstain_item_has_no_label():
    # no gold citation → nothing to find → None (not False)
    assert diagnose.flat_reachable("q", [], FakeEmbedder(), FakeTable([])) is None
