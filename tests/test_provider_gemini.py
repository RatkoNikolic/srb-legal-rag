"""GeminiLLM (google-genai SDK) contract: structured-output config on the wire,
uniform retry in `_call` (API statuses + transport blips), honest accounting
incl. thought tokens, shape-error signaling. No network — a fake SDK client."""

import httpx
import pytest
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from kl_core.providers.gemini import GeminiEmbedder, GeminiLLM


def _response(text='{"ok": true}', *, finish=genai_types.FinishReason.STOP,
              prompt_tokens=10, out_tokens=5, thought_tokens=2, candidates=True):
    return genai_types.GenerateContentResponse(
        candidates=[genai_types.Candidate(
            content=genai_types.Content(parts=[genai_types.Part(text=text)]),
            finish_reason=finish,
        )] if candidates else [],
        usage_metadata=genai_types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt_tokens,
            candidates_token_count=out_tokens,
            thoughts_token_count=thought_tokens,
        ),
    )


class FakeModels:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes[len(self.calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    embed_content = generate_content  # embed tests reuse the same fake


class FakeClient:
    def __init__(self, outcomes):
        self.models = FakeModels(outcomes)


def _llm(outcomes) -> GeminiLLM:
    return GeminiLLM("test-key", client=FakeClient(outcomes))


def test_structured_output_config_on_the_wire():
    llm = _llm([_response()])
    result = llm.generate("hi", system="be terse",
                          json_schema={"type": "object"}, max_output_tokens=4096)
    assert result.text == '{"ok": true}'
    call = llm._client.models.calls[0]
    assert call["model"] == "gemini-3.6-flash"
    cfg = call["config"]
    assert cfg.response_mime_type == "application/json"
    assert cfg.response_json_schema == {"type": "object"}
    assert cfg.max_output_tokens == 4096
    assert cfg.system_instruction == "be terse"
    # accounting: thoughts are billed as output (5 + 2), cost from the 3.6 sheet
    assert result.output_tokens == 7
    assert result.cost_usd == pytest.approx((10 * 1.50 + 7 * 7.50) / 1_000_000)
    assert result.metadata["finish_reason"] == "STOP"


def test_retryable_api_error_then_success(monkeypatch):
    monkeypatch.setattr("kl_core.providers.gemini.time.sleep", lambda *_: None)
    llm = _llm([
        genai_errors.APIError(503, {"error": {"message": "overloaded"}}),
        _response(),
    ])
    assert llm.generate("hi").text == '{"ok": true}'
    assert len(llm._client.models.calls) == 2


def test_non_retryable_api_error_propagates():
    llm = _llm([genai_errors.APIError(400, {"error": {"message": "bad"}})])
    with pytest.raises(genai_errors.APIError):
        llm.generate("hi")
    assert len(llm._client.models.calls) == 1


def test_transport_error_is_retried_then_succeeds(monkeypatch):
    monkeypatch.setattr("kl_core.providers.gemini.time.sleep", lambda *_: None)
    llm = _llm([
        httpx.ConnectError("boom"),
        httpx.ReadTimeout("slow"),
        _response(),
    ])
    assert llm.generate("hi").text == '{"ok": true}'
    assert len(llm._client.models.calls) == 3


def test_empty_candidates_raise_shape_error():
    # a safety block yields no candidates — the generate_json layer retries these
    llm = _llm([_response(candidates=False)])
    with pytest.raises(IndexError):
        llm.generate("hi")


def test_truncation_finish_reason_surfaces():
    llm = _llm([_response(finish=genai_types.FinishReason.MAX_TOKENS)])
    result = llm.generate("hi")
    assert result.metadata["finish_reason"] == "MAX_TOKENS"


def test_embedder_batches_and_estimates_cost():
    emb_response = genai_types.EmbedContentResponse(
        embeddings=[genai_types.ContentEmbedding(values=[0.1] * 4)] * 2
    )
    embedder = GeminiEmbedder("test-key", client=FakeClient([emb_response]))
    batch = embedder.embed(["текст један", "текст два"], task_type="RETRIEVAL_QUERY")
    assert len(batch.vectors) == 2 and batch.model == "gemini-embedding-2"
    call = embedder._client.models.calls[0]
    assert call["config"].task_type == "RETRIEVAL_QUERY"
