"""Ablation config contract (rag_arms.md §1/§7): each toggle isolates one
retrieval capability; the default config reproduces the full production pipeline,
and the exact config rides every run's telemetry. Tests cover the config itself,
the advanced-arm gates (the richest single-stage pipeline), and the telemetry
recording chokepoint."""

from __future__ import annotations

import json
from datetime import date

from kl_core.llm import GenerationResult
from lexsr.arms.advanced import AdvancedArm
from lexsr.arms.base import ABLATION_SPEC, AblationConfig, Timer
from lexsr.arms.naive import NaiveArm


# ── the config itself ───────────────────────────────────────────────────────
def test_ablation_config_contract():
    """`AblationConfig` defaults to the full pipeline, labels only what differs,
    stays in lockstep with the UI spec, and rejects unknown toggles.

    The label groups runs in the telemetry store, so it must be empty for a
    default run and deterministic for a non-default one. `ABLATION_SPEC` is the
    surface the UI renders from — if it drifts from the config fields, a toggle
    silently stops being reachable.
    """
    import pytest

    default = AblationConfig()
    assert all(default.model_dump().values()), "every toggle on by default"
    assert default.nondefault() == {}, "default run has no deltas"
    assert default.label() == "default", "default label"

    cfg = AblationConfig(rerank=False, hybrid=False)
    assert cfg.nondefault() == {"hybrid": False, "rerank": False}, "only the deltas"
    assert cfg.label() == "no-hybrid,no-rerank", "deterministic label"

    assert {s["key"] for s in ABLATION_SPEC} == set(AblationConfig.model_fields), \
        "the UI surface cannot drift from the config"
    for spec in ABLATION_SPEC:
        assert spec["arms"] and spec["label"] and spec["description"], spec["key"]

    with pytest.raises(Exception):
        AblationConfig(not_a_real_toggle=True)  # extra=forbid


# ── fakes ────────────────────────────────────────────────────────────────────
def _row(clan: str, *, valid_to: str = "", vrsta: str = "zakon", act: str = "law-a") -> dict:
    return {
        "chunk_id": f"{act}/clan-{clan}",
        "law_slug": "zakon-o-radu",
        "source_title": "ЗАКОН о раду",
        "fragment": f"član {clan}",
        "text": f"Текст члана {clan}.",
        "valid_from": "2005-03-23",
        "valid_to": valid_to,
        "repealed": bool(valid_to),
        "url": "",
        "version": "",
        "vrsta": vrsta,
        "_distance": 0.1,
    }


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def where(self, clause):
        if "is_version_slice" in clause:  # the slice-flood guard
            self._rows = [r for r in self._rows if not r.get("is_version_slice")]
        return self

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    def to_list(self):
        return list(self._rows)


class _FakeTable:
    """Records whether the FTS (lexical) leg was queried."""

    def __init__(self, rows):
        self.rows = rows
        self.fts_called = False

    def search(self, query=None, *, query_type=None, **kwargs):
        if query_type == "fts":
            self.fts_called = True
        return _FakeQuery(list(self.rows))


class _FakeEmbedder:
    def embed(self, texts, *, task_type):
        class B:
            vectors = [[0.0]]
        return B()


class _RecordingLLM:
    """Counts generate() calls so we can assert the rerank LLM stage is skipped."""

    def __init__(self):
        self.calls = 0

    def generate(self, prompt, *, system=None, json_schema=None,
                 temperature=0.0, max_output_tokens=2048):
        self.calls += 1
        return GenerationResult(
            text=json.dumps({"ranked": []}), model="fake",
            prompt_tokens=1, output_tokens=1, cost_usd=0.0, latency_ms=1.0,
        )


# ── advanced-arm gates ────────────────────────────────────────────────────────
def test_hybrid_off_skips_fts_leg():
    table = _FakeTable([_row("1")])
    arm = AdvancedArm(llm=None, embedder=_FakeEmbedder(), table=table,
                      config=AblationConfig(hybrid=False))
    arm.retrieve("питање?", as_of=None, timer=Timer())
    assert table.fts_called is False
    # control: default config does hit the lexical leg
    table2 = _FakeTable([_row("1")])
    AdvancedArm(llm=None, embedder=_FakeEmbedder(), table=table2).retrieve(
        "питање?", as_of=None, timer=Timer()
    )
    assert table2.fts_called is True


def test_rerank_off_keeps_rrf_order_no_llm_call():
    # > TOP_K candidates so the rerank stage would normally fire and call the LLM.
    rows = [_row(str(i)) for i in range(12)]
    llm = _RecordingLLM()
    arm = AdvancedArm(llm=llm, embedder=_FakeEmbedder(), table=_FakeTable(rows),
                      config=AblationConfig(rerank=False))
    retrieved, _ = arm.retrieve("питање?", as_of=None, timer=Timer())
    assert llm.calls == 0  # no listwise-rerank LLM call
    assert retrieved  # still returns a ranked pool (RRF order)


def test_temporal_off_keeps_out_of_force_units():
    rows = [_row(c, valid_to="2010-01-01") for c in ("90", "91", "92")]
    arm = AdvancedArm(llm=None, embedder=_FakeEmbedder(), table=_FakeTable(rows),
                      config=AblationConfig(temporal_filter=False))
    retrieved, extras = arm.retrieve("питање?", as_of=date(2020, 1, 1), timer=Timer())
    assert extras["dropped_by_temporal_filter"] == 0
    assert len(retrieved) == 3  # repealed units retained — no filtering


def test_diversity_off_keeps_redundant_bylaw_chunks():
    # five chunks of ONE bylaw act; default caps to PER_ACT_CAP=3, off keeps all.
    rows = [_row(str(i), vrsta="podzakonski_akt", act="ref/9") for i in range(5)]
    on = AdvancedArm(llm=None, embedder=_FakeEmbedder(), table=_FakeTable(rows)).retrieve(
        "питање?", as_of=None, timer=Timer()
    )[0]
    off = AdvancedArm(llm=None, embedder=_FakeEmbedder(), table=_FakeTable(rows),
                      config=AblationConfig(diversity_cap=False)).retrieve(
        "питање?", as_of=None, timer=Timer()
    )[0]
    assert len(on) == 3  # capped
    assert len(off) == 5  # uncapped


# ── telemetry recording chokepoint ───────────────────────────────────────────
class _AnswerLLM:
    def generate(self, prompt, *, system=None, json_schema=None,
                 temperature=0.0, max_output_tokens=2048):
        return GenerationResult(
            text=json.dumps({"answer": "Одговор.", "citations": [0], "abstained": False}),
            model="fake", prompt_tokens=10, output_tokens=5, cost_usd=0.001, latency_ms=1.0,
        )


def test_run_records_exact_ablation_in_extras():
    arm = NaiveArm(llm=_AnswerLLM(), embedder=_FakeEmbedder(), table=_FakeTable([_row("1")]),
                   config=AblationConfig(rerank=False))
    result = arm.answer("питање?")
    assert result.extras["ablation_label"] == "no-rerank"
    assert result.extras["ablation"]["rerank"] is False
    assert result.extras["ablation"]["hybrid"] is True  # full dict, not just the delta


def test_default_run_records_default_label():
    arm = NaiveArm(llm=_AnswerLLM(), embedder=_FakeEmbedder(), table=_FakeTable([_row("1")]))
    result = arm.answer("питање?")
    assert result.extras["ablation_label"] == "default"
