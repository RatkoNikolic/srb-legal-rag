"""`generate_json`/`generate_text` contract: retries on parse/shape failures,
fail-fast on deterministic truncation, honest cross-attempt accounting, and
failure logging. No network — scripted fakes."""

import json

import pytest

from kl_core.llm import GenAccounting, GenerationResult, generate_json, generate_text

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def _result(text, *, finish_reason=None, output_tokens=5):
    return GenerationResult(
        text=text, model="fake", prompt_tokens=10, output_tokens=output_tokens,
        cost_usd=0.001, latency_ms=2.0,
        metadata={"finish_reason": finish_reason} if finish_reason else {},
    )


class SequenceLLM:
    """Yields queued outcomes: a GenerationResult, or an Exception to raise."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def generate(self, prompt, **kwargs):
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_fail_then_succeed_sums_accounting():
    llm = SequenceLLM([_result("нисам JSON"), _result(json.dumps({"ok": True}))])
    parsed, acct = generate_json(llm, "p", json_schema=SCHEMA, max_output_tokens=64)
    assert parsed == {"ok": True}
    assert acct.attempts == 2
    # both attempts' tokens and cost are summed — failed attempts cost money
    assert acct.prompt_tokens == 20 and acct.output_tokens == 10
    assert acct.cost_usd == pytest.approx(0.002)
    assert len(acct.failures) == 1
    assert "JSONDecodeError" in acct.failures[0]["error"]
    assert acct.failures[0]["raw_preview"] == "нисам JSON"


def test_exhaustion_returns_none_with_full_log():
    llm = SequenceLLM([_result("a"), _result("b"), _result("c"), _result("d")])
    parsed, acct = generate_json(llm, "p", json_schema=SCHEMA, max_output_tokens=64)
    assert parsed is None
    assert acct.attempts == 3 and llm.calls == 3  # max n=3, never more
    assert len(acct.failures) == 3


def test_provider_shape_error_is_retried():
    # a Gemini safety block raises KeyError("candidates") before any result
    llm = SequenceLLM([KeyError("candidates"), _result(json.dumps({"ok": True}))])
    parsed, acct = generate_json(llm, "p", json_schema=SCHEMA, max_output_tokens=64)
    assert parsed == {"ok": True} and acct.attempts == 2
    assert acct.failures[0]["raw_preview"] == ""  # no result to preview


class CapTrackingLLM(SequenceLLM):
    """SequenceLLM that also records each call's max_output_tokens."""

    def __init__(self, outcomes):
        super().__init__(outcomes)
        self.caps = []

    def generate(self, prompt, **kwargs):
        self.caps.append(kwargs.get("max_output_tokens"))
        return super().generate(prompt, **kwargs)


def test_truncation_escalation_policy():
    """Truncation gets exactly one 2x cap escalation, and never a same-cap retry.

    A thinking model can overspend the output budget on thoughts, so one retry
    at double the cap recovers a genuinely long answer. A second escalation is
    refused because truncation at the escalated cap is deterministic at
    temperature 0, and a retry already at the ceiling is refused for the same
    reason -- it would reproduce the same truncation and bill for it twice.
    """
    # 1. one escalation recovers
    llm = CapTrackingLLM([
        _result('{"ok": tru', finish_reason="MAX_TOKENS"),
        _result(json.dumps({"ok": True})),
    ])
    parsed, acct = generate_json(llm, "p", json_schema=SCHEMA, max_output_tokens=1024)
    assert parsed == {"ok": True}, "escalated retry succeeds"
    assert llm.caps == [1024, 2048], "exactly one 2x escalation"
    assert acct.failures[0]["escalated_to"] == 2048, "escalation is logged"

    # 2. truncated at the escalated cap too -> stop, never a 3rd call
    llm = CapTrackingLLM([
        _result('{"ok": tru', finish_reason="MAX_TOKENS"),
        _result('{"ok": tru', finish_reason="MAX_TOKENS"),
    ])
    parsed, acct = generate_json(llm, "p", json_schema=SCHEMA, max_output_tokens=1024)
    assert parsed is None, "no escalation twice"
    assert acct.attempts == 2 and llm.caps == [1024, 2048], "stops after the escalated try"

    # 3. already at the ceiling -> fail fast, one call only
    llm = CapTrackingLLM([_result('{"ok": tru', finish_reason="MAX_TOKENS")])
    parsed, acct = generate_json(llm, "p", json_schema=SCHEMA, max_output_tokens=32768)
    assert parsed is None, "ceiling truncation fails fast"
    assert acct.attempts == 1 and llm.caps == [32768], "no same-cap retry"
    assert acct.failures[0]["finish_reason"] == "MAX_TOKENS", "reason recorded"


def test_non_dict_payload_is_a_failure():
    llm = SequenceLLM([_result("[1, 2]"), _result(json.dumps({"ok": False}))])
    parsed, acct = generate_json(llm, "p", json_schema=SCHEMA, max_output_tokens=64)
    assert parsed == {"ok": False} and acct.attempts == 2


def test_http_errors_propagate_immediately():
    import httpx2

    request = httpx2.Request("POST", "https://x")
    response = httpx2.Response(400, request=request)
    llm = SequenceLLM([
        httpx2.HTTPStatusError("bad request", request=request, response=response)
    ])
    with pytest.raises(httpx2.HTTPStatusError):
        generate_json(llm, "p", json_schema=SCHEMA, max_output_tokens=64)
    assert llm.calls == 1  # never retried here — _post owns HTTP retries


def test_generate_text_retries_shape_and_raises_after():
    llm = SequenceLLM([IndexError("no parts"), _result("текст")])
    result, acct = generate_text(llm, "p", max_output_tokens=64)
    assert result.text == "текст" and acct.attempts == 2

    llm = SequenceLLM([IndexError("a"), IndexError("b"), IndexError("c")])
    with pytest.raises(RuntimeError, match="3 attempts"):
        generate_text(llm, "p", max_output_tokens=64)


def test_accounting_model_survives_none_result():
    acct = GenAccounting()
    assert acct.model == "unknown" and acct.finish_reason is None
