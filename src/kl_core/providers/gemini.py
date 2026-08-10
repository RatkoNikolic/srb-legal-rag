"""Gemini implementations of the LLM and Embedder seams (google-genai SDK).

Models + pricing (verified 2026-08-05, ai.google.dev/gemini-api/docs/pricing):
- gemini-3.6-flash       $1.50 / 1M input tokens, $7.50 / 1M output tokens
- gemini-3.5-flash       $1.50 / 1M input tokens, $9.00 / 1M output tokens
  (previous default — generated all batches ≤ 2026-08-04; keep for replays)
- gemini-embedding-2     $0.20 / 1M input tokens (3072 dims, 8192-token input)

Structured output is native: `response_json_schema` + `application/json` MIME —
the API constrains generation to the caller's JSON schema (no post-hoc parsing
contract beyond `json.loads`). Retry policy stays OURS (not the SDK's
`retry_options`) so both providers share one uniform layer: retryable HTTP
statuses + transient transport errors here in `_call`, parse/shape retries in
`kl_core.llm.generate_json` above.
"""

from __future__ import annotations

import random
import time

import httpx  # the SDK's transport (transitive dep) — caught for retry only
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from kl_core.embeddings import EmbeddingBatch, TaskType
from kl_core.llm import GenerationResult
from kl_core.llm import parse_json_text as _parse_json_text

FLASH_MODEL = "gemini-3.6-flash"
EMBED_MODEL = "gemini-embedding-2"

PRICE_PER_MTOK = {
    FLASH_MODEL: {"input": 1.50, "output": 7.50},
    "gemini-3.5-flash": {"input": 1.50, "output": 9.00},
    EMBED_MODEL: {"input": 0.20, "output": 0.0},
}

_RETRYABLE = {429, 500, 502, 503, 504}
_MAX_RETRIES = 5
_EMBED_BATCH_SIZE = 50


def _cost(model: str, prompt_tokens: int, output_tokens: int) -> float:
    price = PRICE_PER_MTOK[model]
    return (prompt_tokens * price["input"] + output_tokens * price["output"]) / 1_000_000


class _GeminiBase:
    # 300 s: the raised output caps can legitimately generate for minutes;
    # the timeout is a ceiling, not a latency change. HttpOptions.timeout is ms.
    def __init__(
        self, api_key: str, *, timeout_s: float = 300.0,
        client: genai.Client | None = None,
    ) -> None:
        # `client` is a test seam (a fake exposing .models.generate_content /
        # .models.embed_content) — mirrors the transport seam in AnthropicLLM.
        self._client = client or genai.Client(
            api_key=api_key,
            http_options=genai_types.HttpOptions(timeout=int(timeout_s * 1000)),
        )

    def _call(self, fn, /, **kwargs):
        """Uniform retry layer (mirrors AnthropicLLM._post): retryable HTTP
        statuses and transient transport blips, exponential backoff + jitter;
        non-retryable API errors and protocol errors propagate immediately."""
        for attempt in range(_MAX_RETRIES):
            try:
                return fn(**kwargs)
            except genai_errors.APIError as exc:
                if getattr(exc, "code", None) in _RETRYABLE and attempt < _MAX_RETRIES - 1:
                    time.sleep(min(2**attempt, 30) + random.uniform(0, 0.5))
                    continue
                raise
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(min(2**attempt, 30) + random.uniform(0, 0.5))
                    continue
                raise
        raise RuntimeError("unreachable")


class GeminiLLM(_GeminiBase):
    def __init__(
        self, api_key: str, *, model: str = FLASH_MODEL, timeout_s: float = 300.0,
        client: genai.Client | None = None,
    ):
        super().__init__(api_key, timeout_s=timeout_s, client=client)
        self.model = model

    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_schema: dict | None = None,
        temperature: float = 0.0,
        max_output_tokens: int = 8192,
        images: list[tuple[str, bytes]] | None = None,
    ) -> GenerationResult:
        """`images` is an optional list of (mime_type, raw_bytes) inline parts —
        gemini-3.6-flash is multimodal, used for OCR of statutory image annexes."""
        config = genai_types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            system_instruction=system or None,
            response_mime_type="application/json" if json_schema is not None else None,
            response_json_schema=json_schema,
        )
        contents: list = [prompt]
        for mime_type, raw in images or []:
            contents.append(genai_types.Part.from_bytes(data=raw, mime_type=mime_type))

        started = time.perf_counter()
        response = self._call(
            self._client.models.generate_content,
            model=self.model, contents=contents, config=config,
        )
        latency_ms = (time.perf_counter() - started) * 1000

        candidates = response.candidates or []
        if not candidates:  # safety block / empty response — a shape error;
            raise IndexError("no candidates in response")  # generate_json retries
        finish = candidates[0].finish_reason
        usage = response.usage_metadata
        prompt_tokens = (usage.prompt_token_count or 0) if usage else 0
        # thoughts are billed as output tokens on thinking models
        output_tokens = (
            ((usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0))
            if usage else 0
        )
        return GenerationResult(
            text=response.text or "",
            model=self.model,
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            cost_usd=_cost(self.model, prompt_tokens, output_tokens),
            latency_ms=latency_ms,
            metadata={"finish_reason": finish.name if finish is not None else None},
        )


class GeminiEmbedder(_GeminiBase):
    dimensions = 3072

    def __init__(
        self, api_key: str, *, model: str = EMBED_MODEL, timeout_s: float = 120.0,
        client: genai.Client | None = None,
    ):
        super().__init__(api_key, timeout_s=timeout_s, client=client)
        self.model = model

    def embed(self, texts: list[str], *, task_type: TaskType) -> EmbeddingBatch:
        vectors: list[list[float]] = []
        total_tokens = 0
        for start in range(0, len(texts), _EMBED_BATCH_SIZE):
            batch = texts[start : start + _EMBED_BATCH_SIZE]
            response = self._call(
                self._client.models.embed_content,
                model=self.model,
                # one Content per text — a plain list of strings would be read
                # as ONE content with many parts (a single embedding back)
                contents=[genai_types.Content(parts=[genai_types.Part(text=t)])
                          for t in batch],
                config=genai_types.EmbedContentConfig(task_type=task_type),
            )
            vectors.extend(list(e.values) for e in response.embeddings)
            # embed_content returns no per-text usage — rough estimate from
            # input size (chars/4; Cyrillic plausibly tokenizes denser, so treat
            # reported embed costs as an estimate, not a measurement).
            total_tokens += sum(len(t) // 4 + 1 for t in batch)
        return EmbeddingBatch(
            vectors=vectors,
            model=self.model,
            total_tokens=total_tokens,
            cost_usd=_cost(self.model, total_tokens, 0),
        )


# parse_json_text lives in kl_core.llm (next to the retry helper); re-exported
# here for import compatibility.
parse_json_text = _parse_json_text
