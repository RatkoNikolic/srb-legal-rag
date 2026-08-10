"""Eval scoring contracts: reference-guided judge mapping, citation P/R/F1,
temporal correctness from validity intervals, deterministic abstain scoring,
and the golden-set file itself (format + grounding invariants)."""

import json
from datetime import date
from pathlib import Path

import pytest

from kl_core.evals import (
    TaskItem,
    aggregate,
    citation_key,
    citation_keys,
    score_item,
)
from kl_core.llm import GenerationResult
from kl_core.types import Answer, Provenance, Query

GOLDENS = Path(__file__).parent.parent / "goldens"


class FakeJudge:
    def __init__(self, verdict: str):
        self.verdict = verdict
        self.calls = 0

    def generate(self, prompt, *, system=None, json_schema=None,
                 temperature=0.0, max_output_tokens=2048):
        self.calls += 1
        return GenerationResult(
            text=json.dumps({"verdict": self.verdict, "rationale": "because"}),
            model="fake-judge", prompt_tokens=10, output_tokens=5,
        )


def _prov(fragment="član 69", valid_to=None):
    return Provenance(
        source_id="sgrs/skupstina/zakon/2005/24/1",
        source_title="ЗАКОН о раду",
        fragment=fragment,
        valid_from=date(2005, 3, 15),
        valid_to=valid_to,
    )


def _item(as_of=None, gold_answer="Најмање 20 радних дана.", category="lookup"):
    return TaskItem(
        item_id="t1",
        query=Query(text="Колико дана годишњег одмора?", as_of=as_of),
        category=category,
        gold_answer=gold_answer,
        gold_citations=["sgrs/skupstina/zakon/2005/24/1::član 69"],
    )


def test_correct_verdict_and_citation_match():
    answer = Answer(text="20 радних дана.", citations=[_prov()])
    score = score_item(_item(), answer, "naive", judge=FakeJudge("correct"))
    assert score.correct and score.correctness == 1.0
    assert score.citation_precision == 1.0 and score.citation_recall == 1.0
    assert score.provenance_fidelity == 1.0
    assert score.judge_rationale == "because"


def test_partially_correct_gets_graded_credit_not_binary_credit():
    answer = Answer(text="Постоји минимум.", citations=[_prov()])
    score = score_item(_item(), answer, "naive", judge=FakeJudge("partially_correct"))
    assert score.correctness == 0.5 and not score.correct


def test_wrong_citation_hits_fidelity_not_correctness():
    answer = Answer(text="20 радних дана.", citations=[_prov(fragment="član 36")])
    score = score_item(_item(), answer, "naive", judge=FakeJudge("correct"))
    assert score.correct
    assert score.provenance_fidelity == 0.0 and score.citation_precision == 0.0


def test_abstain_item_scores_without_judge():
    judge = FakeJudge("correct")
    item = TaskItem(item_id="a1", query=Query(text="Ван корпуса?"),
                    category="abstain", gold_answer=None)
    abstained = Answer(text="", citations=[], abstained=True)
    confabulated = Answer(text="Измишљено.", citations=[_prov()], abstained=False)
    assert score_item(item, abstained, "naive", judge=judge).correct
    assert not score_item(item, confabulated, "naive", judge=judge).correct
    assert judge.calls == 0  # abstain items are deterministic


def test_gold_item_without_judge_raises():
    with pytest.raises(ValueError):
        score_item(_item(), Answer(text="x", citations=[_prov()]), "naive")


def test_temporal_correctness_from_validity_interval():
    # cited unit repealed 2014-07-21; as_of 2016 → temporally incorrect
    repealed = Answer(text="…", citations=[_prov(valid_to=date(2014, 7, 21))])
    score = score_item(_item(as_of=date(2016, 1, 1)), repealed, "naive",
                       judge=FakeJudge("correct"))
    assert score.temporally_correct is False
    # same citation, as_of while still in force → correct
    score = score_item(_item(as_of=date(2010, 1, 1)), repealed, "naive",
                       judge=FakeJudge("correct"))
    assert score.temporally_correct is True
    # no as_of → not applicable
    score = score_item(_item(), repealed, "naive", judge=FakeJudge("correct"))
    assert score.temporally_correct is None


GOLD_KEY = "sgrs/skupstina/zakon/2005/24/1::član 69"


def test_retrieval_recall_decomposes_from_citation_recall():
    # The retriever surfaced the gold unit, but the generator cited the wrong one
    # → retrieval_recall=1 while citation_recall=0 (the LegalBench-RAG split).
    answer = Answer(text="20 радних дана.", citations=[_prov(fragment="član 36")])
    score = score_item(_item(), answer, "naive", judge=FakeJudge("correct"),
                       retrieved_keys=[GOLD_KEY])
    assert score.retrieval_recall == 1.0 and score.retrieval_precision == 1.0
    assert score.citation_recall == 0.0
    # cited only a non-gold source → misgrounded, and hallucinated despite a
    # "correct" verdict (right text, wrong/absent provenance).
    assert score.misgrounded is True and score.hallucinated is True


def test_retrieval_recall_none_when_pool_absent_or_abstain():
    score = score_item(_item(), Answer(text="x", citations=[_prov()]),
                       "naive", judge=FakeJudge("correct"))
    assert score.retrieval_recall is None  # no pool supplied
    abstain_item = TaskItem(item_id="a", query=Query(text="?"), category="hard",
                            capability="abstain", gold_answer=None)
    s2 = score_item(abstain_item, Answer(text="", citations=[], abstained=True),
                    "naive", retrieved_keys=[GOLD_KEY])
    assert s2.retrieval_recall is None and s2.abstain_target is True


def test_temporal_and_grounding_are_independent_axes():
    # in_force mode, as_of after the cited article's repeal → cited a repealed
    # unit as authority. The temporal flag fires, but the citation IS the gold
    # unit, so grounding is fine: `misgrounded` must NOT inherit the temporal
    # verdict. Folding the two together made a defect in either contaminate both
    # — with a temporally-naive arm the oracle's entire misgrounding score was this
    # flag and nothing else.
    repealed = Answer(text="…", citations=[_prov(valid_to=date(2014, 7, 21))])
    score = score_item(_item(as_of=date(2016, 1, 1)), repealed, "naive",
                       judge=FakeJudge("correct"), retrieved_keys=[GOLD_KEY])
    assert score.temporally_correct is False
    assert score.temporally_wrong_and_cited is True
    assert score.misgrounded is False and score.hallucinated is False
    # in force on the as_of → no temporal flag either
    ok = score_item(_item(as_of=date(2010, 1, 1)), repealed, "naive",
                    judge=FakeJudge("correct"), retrieved_keys=[GOLD_KEY])
    assert ok.temporally_wrong_and_cited is False and ok.misgrounded is False


def test_misgrounded_is_precision_zero_regardless_of_time():
    # Cites a unit outside the gold set entirely → grounding failure, on an item
    # with no `as_of` at all, so it cannot be coming from the temporal limb.
    off_gold = Answer(text="…", citations=[_prov(fragment="član 999")])
    score = score_item(_item(), off_gold, "naive", judge=FakeJudge("correct"))
    assert score.citation_precision == 0.0
    assert score.misgrounded is True
    assert score.temporally_wrong_and_cited is None  # no as_of → not applicable
    # judged correct, but misgrounded → still a hallucination by the RegLab limb
    assert score.hallucinated is True


def test_citing_abstention_is_still_graded_for_safety():
    # "I can't determine this, but see član X" where X is repealed is the
    # invariant-2 hazard in its purest form. Gating the safety family on
    # delivery made those citations unscoreable.
    hedged = Answer(text="Нисам сигуран…", abstained=True,
                    citations=[_prov(fragment="član 999", valid_to=date(2014, 7, 21))])
    score = score_item(_item(as_of=date(2016, 1, 1)), hedged, "naive",
                       judge=FakeJudge("incorrect"))
    assert score.temporally_wrong_and_cited is True  # graded despite abstaining
    assert score.misgrounded is True
    # hallucination stays scoped to *delivered* answers — a refusal is not a
    # false statement, however badly sourced.
    assert score.hallucinated is None


def _versioned(version, valid_to=None):
    p = _prov(valid_to=valid_to)
    return p.model_copy(update={"version": version})


def test_period_aware_citation_matching():
    """Citations are graded by subsumption over `citation_keys`.

    An article is stored one row per *(član × version-slice)*, and all slices
    share the unpinned key — so without this the whole provenance family is
    blind to period: citing the 2022 text of a 2018 question scored a perfect
    1.0. An **unpinned** gold citation matches any
    version (the right default — most articles were never amended); a
    **`@version`-pinned** one matches exactly one slice. Retrieval recall must
    use the same rule, or the retrieval−citation gap compares two different
    notions of a hit."""

    # unpinned gold subsumes every version, including an unversioned unit
    for version in ("421995", "433526", None):
        score = score_item(_item(), Answer(text="…", citations=[_versioned(version)]),
                           "naive", judge=FakeJudge("correct"))
        assert score.citation_recall == 1.0, f"unpinned gold, version={version}"
        assert score.citation_precision == 1.0, f"unpinned gold, version={version}"

    pinned = _item()
    pinned.gold_citations = ["sgrs/skupstina/zakon/2005/24/1::član 69@421995"]

    # pinned gold matches its own slice only
    right = score_item(pinned, Answer(text="…", citations=[_versioned("421995")]),
                       "naive", judge=FakeJudge("correct"))
    assert right.citation_recall == 1.0 and right.citation_precision == 1.0, "pinned: right slice"

    wrong = score_item(pinned, Answer(text="…", citations=[_versioned("433526")]),
                       "naive", judge=FakeJudge("correct"))
    assert wrong.citation_recall == 0.0 and wrong.citation_precision == 0.0, "pinned: wrong slice"
    assert wrong.misgrounded is True, "right article, wrong period → grounding failure"

    # the retrieved pool is keyed the same way
    pooled = score_item(pinned, Answer(text="…", citations=[_versioned("421995")]),
                        "naive", judge=FakeJudge("correct"),
                        retrieved_keys=[citation_keys(_versioned("433526"))])
    assert pooled.retrieval_recall == 0.0, "pool held only the wrong slice"


def test_reports_repeal_mode_does_not_penalise_the_out_of_force_citation():
    # An item that asks "is this rule still in force?" must cite the repealed
    # article to *report* its repeal — that is the subject of the claim, not the
    # authority for it. No golden item exercises this mode today (60/60 are
    # `in_force`), so the branch is pinned here rather than left untested
    #.
    item = _item(as_of=date(2016, 1, 1))
    item.temporal_mode = "reports_repeal"
    repealed = Answer(text="Престао да важи.",
                      citations=[_prov(valid_to=date(2014, 7, 21))])
    score = score_item(item, repealed, "naive", judge=FakeJudge("correct"))
    assert score.temporally_correct is True      # surfacing the dead unit IS the answer
    assert score.temporally_wrong_and_cited is None  # never flagged in this mode
    # citing only a still-in-force unit fails: it never showed the repeal
    in_force = Answer(text="Важи.", citations=[_prov()])
    miss = score_item(item, in_force, "naive", judge=FakeJudge("correct"))
    assert miss.temporally_correct is False


def test_judge_prompt_is_date_aware_on_dated_items():
    # The grader must see `as_of`, or it compares substance against a dateless
    # reference and cannot penalise a wrong-period answer.
    class CapturingJudge(FakeJudge):
        prompt = None

        def generate(self, prompt, **kw):
            CapturingJudge.prompt = prompt
            return super().generate(prompt, **kw)

    answer = Answer(text="…", citations=[_prov()])
    score_item(_item(as_of=date(2018, 6, 1)), answer, "naive",
               judge=CapturingJudge("correct"))
    assert "2018-06-01" in CapturingJudge.prompt
    assert "AS OF" in CapturingJudge.prompt

    CapturingJudge.prompt = None
    score_item(_item(), answer, "naive", judge=CapturingJudge("correct"))
    assert "AS OF" not in CapturingJudge.prompt  # undated items unchanged


def test_misgrounded_and_hallucination_none_for_abstentions():
    # An abstention is a refusal, not a hallucination.
    item = _item()
    score = score_item(item, Answer(text="", citations=[], abstained=True),
                       "naive", judge=FakeJudge("incorrect"))
    assert score.misgrounded is None and score.hallucinated is None
    # the over-abstention itself is an aggregate-level rate, surfaced below
    assert score.abstained is True and score.abstain_target is False


def test_aggregate_abstention_quality_and_safety_rates():
    judge = FakeJudge("correct")
    answerable = _item()
    abstain_item = TaskItem(item_id="ab", query=Query(text="?"), category="hard",
                            capability="abstain", gold_answer=None)
    scores = [
        # answered an unanswerable item → false answer on unanswerable
        score_item(abstain_item, Answer(text="Измишљено.", citations=[_prov()]),
                   "naive", judge=judge),
        # refused an answerable item → over-abstention
        score_item(answerable, Answer(text="", citations=[], abstained=True),
                   "naive", judge=judge),
        # a clean correct answer with the pool surfacing the gold unit
        score_item(answerable, Answer(text="20 радних дана.", citations=[_prov()]),
                   "naive", judge=judge, retrieved_keys=[GOLD_KEY]),
    ]
    agg = aggregate(scores)["naive"]
    assert agg["false_answer_rate_unanswerable"] == 1.0  # 1/1 unanswerable answered
    assert agg["over_abstention_rate"] == 0.5  # 1 of 2 answerable items refused
    assert agg["abstain_accuracy"] == 0.0  # the one abstain target was answered (wrong)
    assert agg["retrieval_recall"] == 1.0  # the one item with a pool surfaced gold
    assert agg["retrieval_citation_gap"] is not None


def test_graph_stats_section_renders_when_present():
    # The construction-stage decomposition: structural stats from
    # graph_stats.json, rendered as a markdown table when the file exists.
    from lexsr.evals import _graph_stats_lines

    lines = _graph_stats_lines()
    if lines:  # the tracked corpus ships graph_stats.json; tolerate a fresh checkout
        assert any("mean out-degree" in ln for ln in lines)
        assert any("|--:|" in ln for ln in lines)  # a rendered table row


def test_aggregate_groups_by_backend():
    a = Answer(text="20 радних дана.", citations=[_prov()])
    scores = [
        score_item(_item(), a, "naive", judge=FakeJudge("correct"), cost_usd=0.01),
        score_item(_item(), a, "naive", judge=FakeJudge("incorrect"), cost_usd=0.03),
    ]
    agg = aggregate(scores)
    assert agg["naive"]["accuracy"] == 0.5
    assert agg["naive"]["correctness"] == 0.5
    assert agg["naive"]["mean_cost_usd"] == 0.02


def test_citation_key_normalizes():
    assert citation_key(_prov()) == "sgrs/skupstina/zakon/2005/24/1::član 69"


SIT60 = GOLDENS / "golden-situational-60.jsonl"
SIT30 = GOLDENS / "golden-situational-30.jsonl"


def test_situational_goldens_contract():
    """The situational sets (60 + a 30 subset): concrete client-voice questions,
    balanced 3-tier spread where the tier IS the citation doc-spread —
    easy = 1 document · medium = ≥2 documents · hard = ≥2 documents incl. a
    peripheral bylaw/CC. Every item grounded against the real corpus."""
    import collections

    from lexsr import bench

    rows60 = [json.loads(line) for line in SIT60.open(encoding="utf-8")]
    rows30 = [json.loads(line) for line in SIT30.open(encoding="utf-8")]
    items60 = [TaskItem(**r) for r in rows60]
    items30 = [TaskItem(**r) for r in rows30]
    assert len(items60) == 60 and len(items30) == 30
    assert collections.Counter(i.category for i in items60) == {"easy": 20, "medium": 20, "hard": 20}
    assert collections.Counter(i.category for i in items30) == {"easy": 10, "medium": 10, "hard": 10}

    # Every item resolves + is grounded against the live corpus. This needs the
    # derived `chunks.jsonl`, which is rebuilt locally rather than shipped
    # (`python -m lexsr.ingest all`) — so skip rather than fail on a fresh clone
    # that has the corpus snapshot but not yet the derivative.
    if not bench._nonempty_chunks():
        pytest.skip("chunks.jsonl not built — run `python -m lexsr.ingest all` first")
    for r in rows60 + rows30:
        assert bench.validate_item(r) == [], (r["item_id"], bench.validate_item(r))

    # the tier is defined by citation doc-spread — assert each item honors it
    def _src(c):
        return c.split("::")[0]

    def _peri(c):
        s, frag = _src(c), c.split("::")[-1].lower()
        return s.startswith("ref/") or "izreka" in frag or "изрека" in frag

    for i in items60:
        if i.gold_answer is None:
            # abstain target (e18, goldens v1.1): carries no citations by
            # design — the doc-spread tiers apply to answerable items only
            assert i.gold_citations == [], i.item_id
            continue
        docs = {_src(c) for c in i.gold_citations}
        per = any(_peri(c) for c in i.gold_citations)
        if i.category == "easy":
            assert len(docs) == 1 and not per, i.item_id
        elif i.category == "medium":
            assert len(docs) >= 2 and not per, i.item_id
        else:  # hard: leans on a peripheral doc AND spans ≥2 documents
            assert len(docs) >= 2 and per, i.item_id

    # the small set is a strict subset of the large one
    assert {i.item_id for i in items30} <= {i.item_id for i in items60}

    # items shared by both sets must carry identical content (the v1.1 gold
    # corrections are mirrored — a divergent 30-set would silently bench v1.0)
    by_id60 = {r["item_id"]: r for r in rows60}
    for r in rows30:
        assert r == by_id60[r["item_id"]], f"{r['item_id']} diverges between sets"


def test_goldens_v11_corrections_pinned():
    """The 2026-08-06 gold corrections state the verified legal direction
    (FIX_PLAN G1/G2/G5): h02 — the CC struck only the budget-sector exemption,
    the housing benefit stays taxable; m02 — a first-order heir pays no
    inheritance tax; e18 — an abstain target (the KB cannot ground the 2017
    pension-adjustment rule)."""
    rows = {json.loads(line)["item_id"]: json.loads(line)
            for line in SIT60.open(encoding="utf-8")}
    h02 = rows["h02"]["gold_answer"]
    assert "и даље на снази" in h02 and "и даље опорезује" in h02
    assert "осим решавања стамбених потреба" in h02  # the annulled exemption clause
    m02 = rows["m02"]["gold_answer"]
    assert "не плаћа порез на наслеђе" in m02 and "првог наследног реда" in m02
    assert "sgrs/skupstina/zakon/2001/26/1::član 21" in rows["m02"]["gold_citations"]
    assert rows["e18"]["gold_answer"] is None and rows["e18"]["gold_citations"] == []


class FlakyJudge:
    """Unparseable twice, then a valid verdict — exercises judge retries."""

    def __init__(self):
        self.calls = 0

    def generate(self, prompt, *, system=None, json_schema=None,
                 temperature=0.0, max_output_tokens=8192):
        self.calls += 1
        text = ("ne-JSON" if self.calls < 3
                else json.dumps({"verdict": "correct", "rationale": "ok"}))
        return GenerationResult(text=text, model="fake-judge",
                                prompt_tokens=10, output_tokens=5)


class DegenerateJudge:
    """Parses fine but never carries a verdict (a truncated forced tool call)."""

    def generate(self, prompt, *, system=None, json_schema=None,
                 temperature=0.0, max_output_tokens=8192):
        return GenerationResult(text="{}", model="fake-judge")


def test_judge_retries_unparseable_then_grades():
    judge = FlakyJudge()
    answer = Answer(text="20 радних дана.", citations=[_prov()])
    score = score_item(_item(), answer, "naive", judge=judge)
    assert judge.calls == 3
    assert score.correct and score.correctness == 1.0


def test_judge_missing_verdict_raises_not_silent_zero():
    answer = Answer(text="20 радних дана.", citations=[_prov()])
    with pytest.raises(Exception, match="unparseable"):
        score_item(_item(), answer, "naive", judge=DegenerateJudge())
