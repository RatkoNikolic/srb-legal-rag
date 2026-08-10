"""Arm plumbing contracts: the no-citation→abstain invariant, temporal
filtering, and validity annotation in the prompt context."""

import json
from datetime import date

import pytest

import lexsr.arms.base as base
from kl_core.llm import GenerationResult
from lexsr.arms.base import (
    RetrievedChunk,
    Timer,
    condense_query,
    format_context,
    generate_answer,
    in_force_on,
)


class FakeLLM:
    def __init__(self, payload: dict):
        self.payload = payload

    def generate(self, prompt, *, system=None, json_schema=None,
                 temperature=0.0, max_output_tokens=8192):
        return GenerationResult(
            text=json.dumps(self.payload, ensure_ascii=False),
            model="fake", prompt_tokens=10, output_tokens=5,
            cost_usd=0.001, latency_ms=1.0,
        )


class BrokenLLM:
    """Always returns unparseable text — exercises retry exhaustion."""

    def __init__(self):
        self.calls = 0
        self.model = "broken-fake"

    def generate(self, prompt, *, system=None, json_schema=None,
                 temperature=0.0, max_output_tokens=8192):
        self.calls += 1
        return GenerationResult(
            text="ovo nije JSON", model="broken-fake",
            prompt_tokens=10, output_tokens=5, cost_usd=0.001, latency_ms=1.0,
        )


def _chunk(repealed: bool = False) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id="sgrs/skupstina/zakon/2005/24/clan-73", score=0.9, stage="vector",
        source_title="ЗАКОН о раду", fragment="član 73", text="Годишњи одмор…",
        valid_from="2005-03-15", valid_to="2014-07-21" if repealed else "",
        repealed=repealed, url="https://example.org/eli", version="75/2014",
    )


def test_citations_map_to_provenance():
    answer, info = generate_answer(
        FakeLLM({"answer": "Одговор.", "citations": [0], "abstained": False}),
        "Питање?", [_chunk()], as_of=None, annotate_validity=True,
    )
    assert not answer.abstained
    assert answer.citations[0].fragment == "član 73"
    assert answer.citations[0].valid_from == date(2005, 3, 15)
    assert info["cost_usd"] == 0.001


def test_bylaw_citation_renders_delegation_chain(monkeypatch):
    # a citation to a delegated bylaw carries its enabling law so the UI can
    # render "Pravilnik X · на основу: Закон Y" instead of an orphan bylaw.
    monkeypatch.setattr(base, "_PARENT_MAP", {"ref/9": ("zakon-o-radu", "ЗАКОН о раду")})
    bylaw = RetrievedChunk(
        chunk_id="ref/9/clan-1", score=0.8, stage="graph-bylaw",
        source_title="ПРАВИЛНИК о евиденцији", fragment="član 1", text="…",
    )
    answer, _ = generate_answer(
        FakeLLM({"answer": "Образац прописује правилник.", "citations": [0], "abstained": False}),
        "Који образац?", [bylaw], as_of=None, annotate_validity=True,
    )
    cit = answer.citations[0]
    assert cit.parent_source_id == "zakon-o-radu"
    assert cit.parent_title == "ЗАКОН о раду"


def test_non_bylaw_citation_has_no_parent(monkeypatch):
    monkeypatch.setattr(base, "_PARENT_MAP", {"ref/9": ("zakon-o-radu", "ЗАКОН о раду")})
    answer, _ = generate_answer(
        FakeLLM({"answer": "Одговор.", "citations": [0], "abstained": False}),
        "Питање?", [_chunk()], as_of=None, annotate_validity=True,
    )
    assert answer.citations[0].parent_source_id is None
    assert answer.citations[0].parent_title is None


def test_no_citations_forces_abstain():
    # kl_core invariant: an Answer with empty citations is only legal abstained
    answer, _ = generate_answer(
        FakeLLM({"answer": "Тврдња без извора.", "citations": [], "abstained": False}),
        "Питање?", [_chunk()], as_of=None, annotate_validity=True,
    )
    assert answer.abstained
    assert answer.metadata["abstain_reason"] == "no citations returned"


def test_out_of_range_citation_indices_dropped():
    answer, _ = generate_answer(
        FakeLLM({"answer": "Одговор.", "citations": [0, 7, -1], "abstained": False}),
        "Питање?", [_chunk()], as_of=None, annotate_validity=True,
    )
    assert len(answer.citations) == 1


def test_in_force_on():
    row = {"valid_from": "2005-03-15", "valid_to": "2014-07-21"}
    assert in_force_on(row, date(2010, 1, 1))
    assert not in_force_on(row, date(2014, 7, 21))  # repeal day → no longer in force
    assert not in_force_on(row, date(2000, 1, 1))
    assert in_force_on({"valid_from": "", "valid_to": ""}, date(2020, 1, 1))


def test_context_annotation():
    annotated = format_context([_chunk(repealed=True)], annotate_validity=True)
    assert "ПРЕСТАО ДА ВАЖИ 2014-07-21" in annotated
    plain = format_context([_chunk(repealed=True)], annotate_validity=False)
    assert "ПРЕСТАО" not in plain  # naive arm stays naive


def test_condense_query_rewrites_followup():
    history = [
        {"role": "user", "content": "Колико траје годишњи одмор?"},
        {"role": "assistant", "content": "Најмање 20 радних дана."},
    ]
    standalone, info = condense_query(
        FakeLLM({"standalone_query": "Колико траје годишњи одмор за стране држављане?"}),
        history, "а за стране држављане?", Timer(),
    )
    assert standalone == "Колико траје годишњи одмор за стране држављане?"
    assert info["cost_usd"] == 0.001


def test_condense_query_falls_back_to_question_on_empty():
    standalone, _ = condense_query(
        FakeLLM({"standalone_query": ""}), [{"role": "user", "content": "x"}], "питање?", Timer(),
    )
    assert standalone == "питање?"  # never returns an empty retrieval query


def test_generate_answer_includes_history_block():
    history = [{"role": "user", "content": "ПРЕТХОДНО ПИТАЊЕ О ОДМОРУ"}]
    captured = {}

    class CapturingLLM(FakeLLM):
        def generate(self, prompt, **kwargs):
            captured["prompt"] = prompt
            return super().generate(prompt, **kwargs)

    generate_answer(
        CapturingLLM({"answer": "ok", "citations": [0], "abstained": False}),
        "а онда?", [_chunk()], as_of=None, annotate_validity=True, history=history,
    )
    assert "ПРЕТХОДНО ПИТАЊЕ О ОДМОРУ" in captured["prompt"]


def test_generation_failure_flagged_after_retries():
    # 3 unparseable attempts → abstention-by-failure with the generation_failed
    # flag (evals route it to errors) and the full failure log in llm_io
    llm = BrokenLLM()
    answer, info = generate_answer(
        llm, "Питање?", [_chunk()], as_of=None, annotate_validity=True,
    )
    assert llm.calls == 3  # retried to the cap, never more
    assert answer.abstained
    assert "3 attempts" in (answer.metadata.get("abstain_reason") or "")
    assert info["generation_failed"] is True
    assert len(info["llm_io"]["failures"]) == 3
    # honest accounting: all three attempts' tokens are summed
    assert info["prompt_tokens"] == 30 and info["cost_usd"] == pytest.approx(0.003)
    assert info["model"] == "broken-fake"  # synthesized from the llm, not lost


def test_condense_retries_then_falls_back_to_raw_question():
    llm = BrokenLLM()
    standalone, info = condense_query(
        llm, [{"role": "user", "content": "x"}], "оригинално питање?", Timer(),
    )
    assert llm.calls == 3
    assert standalone == "оригинално питање?"
    assert len(info["llm_io"]["failures"]) == 3
