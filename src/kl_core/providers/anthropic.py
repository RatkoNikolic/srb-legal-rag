"""Anthropic implementation of the LLM seam (httpx2, no SDK).

Mirrors `kl_core.providers.gemini.GeminiLLM` so the eval harness can swap the
judge model without touching the harness. This is **eval infra, not an arm**:
the cross-family judge (Claude Opus) defeats the same-family self-preference
objection — a Gemini generator graded by a non-Gemini family (
the benchmark design doc §4). Domain-free: it knows how to talk to the Messages API
and account for its own cost, nothing about laws.

Model + pricing (verified 2026-08-05, platform.claude.com pricing):
- claude-opus-5     $5.00 / 1M input tokens, $25.00 / 1M output tokens
- claude-opus-4-8   $5.00 / 1M input tokens, $25.00 / 1M output tokens
  (previous default — judged all batches ≤ 2026-08-04; keep for replays)

Structured output: native (`output_config.format` with a `json_schema` — GA on
claude-opus-5) — the API constrains the text response to the caller's
schema, so `result.text` is valid JSON directly, symmetric with the Gemini
provider's `response_json_schema`. The API requires `additionalProperties:
false` on every object node; `_strictify` derives that form from the caller's
schema so call sites stay provider-agnostic. (Replaced the earlier forced-
tool-call emulation.)
"""

from __future__ import annotations

import random
import time

import httpx2

from kl_core.llm import GenerationResult

API_BASE = "https://api.anthropic.com/v1"
API_VERSION = "2023-06-01"

OPUS_MODEL = "claude-opus-5"

PRICE_PER_MTOK = {
    OPUS_MODEL: {"input": 5.00, "output": 25.00},
    "claude-opus-4-8": {"input": 5.00, "output": 25.00},
}

# Anthropic uses 529 (overloaded) on top of the usual retryable codes.
_RETRYABLE = {429, 500, 502, 503, 504, 529}
_MAX_RETRIES = 5


def _strictify(schema: dict) -> dict:
    """Return a copy with `additionalProperties: false` on every object node —
    the structured-outputs API requires it; callers' schemas stay plain."""
    out = dict(schema)
    if out.get("type") == "object":
        out.setdefault("additionalProperties", False)
        if "properties" in out:
            out["properties"] = {k: _strictify(v) if isinstance(v, dict) else v
                                 for k, v in out["properties"].items()}
    if isinstance(out.get("items"), dict):
        out["items"] = _strictify(out["items"])
    return out


def _cost(model: str, prompt_tokens: int, output_tokens: int) -> float:
    # Unknown models (a custom override) cost nothing rather than crashing the run.
    price = PRICE_PER_MTOK.get(model, {"input": 0.0, "output": 0.0})
    return (prompt_tokens * price["input"] + output_tokens * price["output"]) / 1_000_000


class AnthropicLLM:
    """Claude via the Messages API. Implements the `LLMClient` protocol."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = OPUS_MODEL,
        # 300 s: matches the Gemini provider — raised output caps can generate
        # for minutes; a ceiling, not a latency change.
        timeout_s: float = 300.0,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self.model = model
        # `transport` is a test seam (httpx2.MockTransport) — the provider still
        # owns the real auth/version headers, so tests exercise them.
        self._client = httpx2.Client(
            base_url=API_BASE,
            headers={
                "x-api-key": api_key,
                "anthropic-version": API_VERSION,
                "content-type": "application/json",
            },
            timeout=timeout_s,
            transport=transport,
        )

    def _post(self, path: str, payload: dict) -> dict:
        for attempt in range(_MAX_RETRIES):
            try:
                response = self._client.post(path, json=payload)
            except (httpx2.TimeoutException, httpx2.NetworkError):
                # transient transport blip — retryable (mirrors the Gemini
                # provider); protocol/proxy errors propagate immediately.
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(min(2**attempt, 30) + random.uniform(0, 0.5))
                    continue
                raise
            if response.status_code in _RETRYABLE and attempt < _MAX_RETRIES - 1:
                retry_after = response.headers.get("retry-after", "")
                delay = min(int(retry_after), 60) if retry_after.isdigit() else min(2**attempt, 30)
                time.sleep(delay + random.uniform(0, 0.5))
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError("unreachable")

    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_schema: dict | None = None,
        temperature: float = 0.0,
        max_output_tokens: int = 8192,
    ) -> GenerationResult:
        # NOTE: `temperature` is accepted for protocol parity but never forwarded —
        # Opus 4.7+ removed sampling params (`temperature`/`top_p`/`top_k` → 400).
        # `thinking` is DISABLED explicitly: on claude-opus-5 thinking is on by
        # default and spends from `max_tokens` (it could truncate the structured
        # response), and judge-style grading needs no chain-of-thought.
        # Disabled is accepted at the default effort (high).
        payload: dict = {
            "model": self.model,
            "max_tokens": max_output_tokens,
            "thinking": {"type": "disabled"},
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system
        if json_schema is not None:
            # native structured outputs (GA): the response's text IS schema-
            # conforming JSON — symmetric with Gemini's response_json_schema
            payload["output_config"] = {
                "format": {"type": "json_schema", "schema": _strictify(json_schema)}
            }

        started = time.perf_counter()
        data = self._post("/messages", payload)
        latency_ms = (time.perf_counter() - started) * 1000

        blocks = data.get("content", [])
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")

        usage = data.get("usage", {})
        prompt_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        return GenerationResult(
            text=text,
            model=data.get("model", self.model),
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            cost_usd=_cost(self.model, prompt_tokens, output_tokens),
            latency_ms=latency_ms,
            metadata={"stop_reason": data.get("stop_reason")},
        )
