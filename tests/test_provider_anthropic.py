"""AnthropicLLM (cross-family eval judge) contract: Messages-API wire shape,
forced-tool-use JSON, honest cost accounting, and the Opus no-sampling rule.
No network — an httpx2 MockTransport stands in for the API."""

import json

import httpx2

from kl_core.providers.anthropic import AnthropicLLM

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string"}, "rationale": {"type": "string"}},
    "required": ["verdict", "rationale"],
}


def _llm_with(handler) -> AnthropicLLM:
    # inject a MockTransport — the provider still sets its own auth/version headers
    return AnthropicLLM("test-key", transport=httpx2.MockTransport(handler))


def test_structured_output_returns_json_text_and_accounts_cost():
    captured = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured["headers"] = request.headers
        captured["body"] = json.loads(request.content)
        return httpx2.Response(200, json={
            "model": "claude-opus-5",
            "content": [{"type": "text",
                         "text": '{"verdict": "correct", "rationale": "matches"}'}],
            "usage": {"input_tokens": 100, "output_tokens": 20},
            "stop_reason": "end_turn",
        })

    llm = _llm_with(handler)
    result = llm.generate("grade this", system="be strict",
                          json_schema=JUDGE_SCHEMA, temperature=0.0, max_output_tokens=4096)

    # native structured outputs: the text IS schema-conforming JSON
    assert json.loads(result.text) == {"verdict": "correct", "rationale": "matches"}
    # cost = (100 * $5 + 20 * $25) / 1e6
    assert result.cost_usd == (100 * 5.0 + 20 * 25.0) / 1_000_000
    assert result.prompt_tokens == 100 and result.output_tokens == 20

    body = captured["body"]
    fmt = body["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    # _strictify: additionalProperties pinned false; caller's schema untouched
    assert fmt["schema"]["additionalProperties"] is False
    assert fmt["schema"]["properties"] == JUDGE_SCHEMA["properties"]
    assert "additionalProperties" not in JUDGE_SCHEMA
    assert "tools" not in body and "tool_choice" not in body
    assert body["max_tokens"] == 4096
    assert body["system"] == "be strict"
    # Opus 4.7+ rejects sampling params — they must never be forwarded
    assert "temperature" not in body and "top_p" not in body
    # claude-opus-5 thinks by default and thinking spends from max_tokens —
    # the judge pins it off so the structured response can't be truncated
    assert body["thinking"] == {"type": "disabled"}
    # correct auth/version headers
    assert captured["headers"]["x-api-key"] == "test-key"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"


def test_plain_text_generation_without_schema():
    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        assert "tools" not in body  # no schema → no forced tool
        return httpx2.Response(200, json={
            "model": "claude-opus-5",
            "content": [{"type": "text", "text": "hello"}],
            "usage": {"input_tokens": 5, "output_tokens": 2},
        })

    result = _llm_with(handler).generate("hi")
    assert result.text == "hello"


def test_transport_error_is_retried(monkeypatch):
    monkeypatch.setattr("kl_core.providers.anthropic.time.sleep", lambda *_: None)
    calls = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx2.ReadTimeout("slow", request=request)
        return httpx2.Response(200, json={
            "model": "claude-opus-5",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    result = _llm_with(handler).generate("hi")
    assert result.text == "ok" and calls["n"] == 2


def test_retries_on_overloaded_then_succeeds(monkeypatch):
    monkeypatch.setattr("kl_core.providers.anthropic.time.sleep", lambda *_: None)
    calls = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx2.Response(529, json={"error": "overloaded"})
        return httpx2.Response(200, json={
            "model": "claude-opus-5",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    result = _llm_with(handler).generate("hi")
    assert result.text == "ok" and calls["n"] == 2
