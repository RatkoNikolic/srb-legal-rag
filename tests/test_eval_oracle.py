"""Context-stuffing oracle wiring: the arm is reported as the *gold-law ceiling*,
so its target law must be resolved from the gold annotation — not silently fall
back to vector-top1 law selection. Pins that every gold-bearing item in the live
golden resolves to a corpus slug (the bug: the situational sets carry no explicit
`filters.law_slug`, so the oracle was never actually an oracle)."""

from kl_core.evals import TaskItem
from kl_core.types import Query
from lexsr.config import CURRENT_GOLDEN
from lexsr.evals import _oracle_slug_map, load_golden, oracle_law_slug


def _item(gold_citations, *, filters=None) -> TaskItem:
    return TaskItem(
        item_id="t",
        query=Query(text="?", filters=filters or {}),
        category="easy",
        gold_answer="a" if gold_citations else None,
        gold_citations=gold_citations,
    )


def test_resolves_first_gold_citation_law():
    m = {"sgrs/skupstina/zakon/2001/24/1": "zakon-o-radu"}
    item = _item(["sgrs/skupstina/zakon/2001/24/1::član 5"])
    assert oracle_law_slug(item, m) == "zakon-o-radu"


def test_explicit_filter_wins_over_gold_citation():
    m = {"sgrs/skupstina/zakon/2001/24/1": "zakon-o-radu"}
    item = _item(["sgrs/skupstina/zakon/2001/24/1::član 5"], filters={"law_slug": "zoo"})
    assert oracle_law_slug(item, m) == "zoo"


def test_bylaw_ref_slug_resolves():
    m = {"ref/432868": "ref/432868"}
    item = _item(["ref/432868::član 2"])
    assert oracle_law_slug(item, m) == "ref/432868"


def test_abstain_item_or_unknown_law_returns_none():
    assert oracle_law_slug(_item([]), {}) is None  # abstain item, no gold cite
    assert oracle_law_slug(_item(["unknown/law::član 1"]), {}) is None  # not in corpus


def test_every_gold_bearing_item_in_live_golden_resolves():
    """The regression pin: with the real manifest, EVERY non-abstain item in the
    live 60-item golden must resolve to a corpus slug — otherwise the 5th arm's
    'gold-law ceiling' is a mislabelled vector-top1 stuffing run."""
    sid_to_slug = _oracle_slug_map()
    assert sid_to_slug, "manifest missing — corpus not checked out?"
    items = load_golden(CURRENT_GOLDEN)
    gold_bearing = [it for it in items if it.gold_citations]
    unresolved = [it.item_id for it in gold_bearing if oracle_law_slug(it, sid_to_slug) is None]
    assert not unresolved, f"oracle law_slug unresolved for {unresolved}"


def test_multiact_item_resolves_all_gold_acts_in_order():
    from lexsr.evals import oracle_law_slugs
    m = {
        "sgrs/skupstina/zakon/2019/52/2": "zakon-o-trgovini",
        "ref/436685": "ref/436685",
    }
    item = _item(["sgrs/skupstina/zakon/2019/52/2::član 12", "ref/436685::član 3"])
    assert oracle_law_slugs(item, m) == ["zakon-o-trgovini", "ref/436685"]


def test_izreka_act_included_but_last():
    from lexsr.evals import oracle_law_slugs
    m = {"sgrs/skupstina/zakon/2001/24/1": "zakon-o-radu", "ref/409286": "ref/409286"}
    item = _item(["ref/409286::izreka", "sgrs/skupstina/zakon/2001/24/1::član 179"])
    assert oracle_law_slugs(item, m) == ["zakon-o-radu", "ref/409286"]


def test_duplicate_acts_deduplicated():
    from lexsr.evals import oracle_law_slugs
    m = {"sgrs/skupstina/zakon/2001/24/1": "zakon-o-radu"}
    item = _item([
        "sgrs/skupstina/zakon/2001/24/1::član 5",
        "sgrs/skupstina/zakon/2001/24/1::član 7",
    ])
    assert oracle_law_slugs(item, m) == ["zakon-o-radu"]
