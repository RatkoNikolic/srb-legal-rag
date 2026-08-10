"""Stuffing-arm contract: oracle vs vector-top1 law selection, document-order
stuffing, and the naive (unannotated) context — all against a fake table."""

import re

from lexsr.arms.base import Timer
from lexsr.arms.stuffing import StuffingArm, _clan_order


def _row(law_slug: str, clan: str) -> dict:
    return {
        "chunk_id": f"sgrs/skupstina/zakon/2005/24/1/clan-{clan}",
        "law_slug": law_slug,
        "source_title": "ЗАКОН о раду",
        "fragment": f"član {clan}",
        "text": f"Текст члана {clan}.",
        "valid_from": "2005-03-15",
        "valid_to": "",
        "repealed": False,
        "url": "",
        "version": "",
        "_distance": 0.1,
    }


class FakeQuery:
    def __init__(self, rows):
        self._rows = rows
        self.where_clause = None

    def where(self, clause):
        self.where_clause = clause
        slugs = set(re.findall(r"'([^']*)'", clause))
        self._rows = [r for r in self._rows if r["law_slug"] in slugs]
        return self

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    def to_list(self):
        return list(self._rows)


class FakeTable:
    def __init__(self, rows):
        self.rows = rows

    def search(self, vector=None, **kwargs):
        return FakeQuery(list(self.rows))


class FakeEmbedder:
    def embed(self, texts, *, task_type):
        class B:
            vectors = [[0.0]]
        return B()


ROWS = [_row("zakon-o-radu", c) for c in ("10", "2", "2а")] + [_row("zup", "1")]


def _retrieve(law_slug=None):
    arm = StuffingArm(llm=None, embedder=FakeEmbedder(), table=FakeTable(ROWS))
    return arm.retrieve("Питање?", as_of=None, timer=Timer(), law_slug=law_slug)


def test_oracle_stuffs_whole_law_in_document_order():
    retrieved, extras = _retrieve(law_slug="zakon-o-radu")
    assert extras["law_selection"] == "oracle"
    assert extras["stuffed_chunks"] == 3
    assert [r.fragment for r in retrieved] == ["član 2", "član 2а", "član 10"]
    assert all(r.stage == "stuffed" for r in retrieved)


def test_fallback_selects_law_of_top1_vector_hit():
    retrieved, extras = _retrieve()
    assert extras["law_selection"] == "vector-top1"
    assert extras["law_slug"] == "zakon-o-radu"  # first fake row wins top-1
    assert extras["stuffed_chunks"] == 3


def test_oracle_accepts_bylaw_ref_slug():
    # a materialized bylaw's oracle target is `ref/NNNNN` — the slug guard must
    # accept the `/` (the fix that unblocks bylaw-targeted gold items).
    rows = [_row("ref/432868", c) for c in ("1", "2")] + [_row("zakon-o-radu", "10")]
    arm = StuffingArm(llm=None, embedder=FakeEmbedder(), table=FakeTable(rows))
    retrieved, extras = arm.retrieve("Питање?", as_of=None, timer=Timer(), law_slug="ref/432868")
    assert extras["law_selection"] == "oracle"
    assert extras["stuffed_chunks"] == 2
    assert {r.fragment for r in retrieved} == {"član 1", "član 2"}


def test_bad_slug_returns_empty_not_injected():
    retrieved, extras = _retrieve(law_slug="x' OR '1'='1")
    assert retrieved == [] and "stuffed_chunks" not in extras


def test_stays_naive_no_validity_annotation():
    assert StuffingArm.annotate_validity is False


def test_clan_order_is_numeric_with_suffix():
    ids = [f"…/clan-{c}" for c in ("10", "2", "2а", "100")]
    assert sorted(ids, key=_clan_order) == ["…/clan-2", "…/clan-2а", "…/clan-10", "…/clan-100"]


def test_interactive_path_is_size_capped():
    from lexsr.arms import stuffing

    big = []
    for i in range(1, 60):
        r = _row("zakon-o-radu", str(i))
        r["text"] = "x" * 5000  # 59 × 5000 ≈ 295k chars, over the interactive cap
        big.append(r)
    arm = StuffingArm(llm=None, embedder=FakeEmbedder(), table=FakeTable(big))
    retrieved, extras = arm.retrieve("нешто ван корпуса?", as_of=None, timer=Timer())
    assert extras["law_selection"] == "vector-top1"
    assert extras["truncated"] is True
    assert extras["stuffed_chars"] <= stuffing.MAX_INTERACTIVE_STUFF_CHARS


def test_oracle_path_is_never_capped():
    # whole-law-in-context is the oracle's measured identity → no truncation
    big = []
    for i in range(1, 60):
        r = _row("zakon-o-radu", str(i))
        r["text"] = "x" * 5000
        big.append(r)
    arm = StuffingArm(llm=None, embedder=FakeEmbedder(), table=FakeTable(big))
    retrieved, extras = arm.retrieve(
        "питање?", as_of=None, timer=Timer(), law_slug="zakon-o-radu"
    )
    assert extras["law_selection"] == "oracle"
    assert extras.get("truncated") in (None, False)
    assert extras["stuffed_chunks"] == 59


def test_multi_act_oracle_stuffs_all_acts_in_target_order():
    """All-gold-acts ceiling (the corrected oracle): every listed act is
    stuffed whole, ordered (act rank, document order) — the second act's
    articles never interleave into or evict the first's."""
    retrieved, extras = _retrieve(law_slug=["zup", "zakon-o-radu"])
    assert extras["law_selection"] == "oracle"
    assert extras["law_slugs"] == ["zup", "zakon-o-radu"]
    assert extras["law_slug"] == "zup"  # primary = first target
    assert [r.fragment for r in retrieved] == ["član 1", "član 2", "član 2а", "član 10"]
    assert extras["stuffed_chunks"] == 4


def test_multi_act_oracle_rejects_bad_slug_but_keeps_good():
    retrieved, extras = _retrieve(law_slug=["x' OR '1'='1", "zakon-o-radu"])
    assert extras["law_slugs"] == ["zakon-o-radu"]
    assert extras["stuffed_chunks"] == 3
