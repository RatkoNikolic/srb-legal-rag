"""LLM seam: generation behind a protocol, instrumentation mandatory.

The benchmark treats the answer-generating model as a *parameter*, not a
commitment (the arms are compared under a fixed generator; generators can be
swapped to produce robustness rows). Every call returns token counts and
cost — $/query is a first-class benchmark metric, so an LLM client that
cannot account for itself is not a valid implementation of this seam.

`generate_json`/`generate_text` are the mandatory resilience layer over the
protocol: every production call site goes through them so parse/shape failures
are retried (≤3 attempts), truncation is retried exactly once at a 2× escalated
cap (same-cap truncation retries are banned — temperature-0 truncation
reproduces identically at the same budget), and every attempt — failed ones
included — is accounted and logged (`GenAccounting`). Transport-level and
HTTP-status retries live in the providers (Anthropic `_post` / Gemini `_call`)
— this layer never re-retries those.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, Field


class GenerationResult(BaseModel):
    """One LLM generation, fully accounted."""

    text: str
    model: str
    prompt_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class LLMClient(Protocol):
    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_schema: dict | None = None,
        temperature: float = 0.0,
        max_output_tokens: int = 8192,
    ) -> GenerationResult:
        """Generate a completion. If `json_schema` is given, the returned
        `text` must be valid JSON conforming to it (provider-enforced)."""
        ...


def parse_json_text(result: GenerationResult) -> dict:
    """Parse a JSON-mode generation, tolerating accidental code fences."""
    text = result.text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(text)


# finish/stop markers that mean "output truncated at the cap" — provider-specific
# spellings (Gemini `finish_reason`, Anthropic `stop_reason`).
_TRUNCATION_MARKERS = {"MAX_TOKENS", "max_tokens"}


def _finish_reason(result: GenerationResult | None) -> str | None:
    if result is None:
        return None
    md = result.metadata or {}
    return md.get("finish_reason") or md.get("stop_reason")


@dataclass
class GenAccounting:
    """Aggregate accounting over all attempts of one logical generation.
    Failed attempts cost real money — they are summed in, never dropped."""

    result: GenerationResult | None = None  # last attempt that returned (None if all raised)
    attempts: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    failures: list[dict] = field(default_factory=list)

    def _absorb(self, result: GenerationResult) -> None:
        self.result = result
        self.prompt_tokens += result.prompt_tokens
        self.output_tokens += result.output_tokens
        self.cost_usd += result.cost_usd
        self.latency_ms += result.latency_ms

    @property
    def model(self) -> str:
        return self.result.model if self.result is not None else "unknown"

    @property
    def finish_reason(self) -> str | None:
        return _finish_reason(self.result)

    def io_summary(self) -> dict:
        """Compact per-stage observability record for run extras (`llm_io`)."""
        out: dict = {"attempts": self.attempts, "finish_reason": self.finish_reason}
        if self.failures:
            out["failures"] = self.failures
        return out


def _log_failure(acct: GenAccounting, error: Exception,
                 result: GenerationResult | None) -> None:
    acct.failures.append({
        "attempt": acct.attempts,
        "error": f"{type(error).__name__}: {error}",
        "finish_reason": _finish_reason(result),
        "output_tokens": result.output_tokens if result is not None else 0,
        "raw_preview": (result.text[:200] if result is not None else ""),
    })


# A truncation retry escalates the cap 2× up to this ceiling (matches the
# largest cap in the system; beyond it a generation is pathological, not
# legitimate). Same-cap truncation retries are never made — temperature-0
# truncation reproduces identically at the same budget.
_ESCALATION_CEILING = 32768


def generate_json(
    llm: LLMClient,
    prompt: str,
    *,
    system: str | None = None,
    json_schema: dict,
    max_output_tokens: int,
    attempts: int = 3,
) -> tuple[dict | None, GenAccounting]:
    """Schema-constrained generation with parse/shape retries.

    Retries (≤ `attempts` total calls): JSON parse failures, non-dict payloads,
    and response-shape errors raised by `generate()` (e.g. a Gemini safety
    block with no candidates). Truncation (the marker set above) is retried
    exactly once at a 2× escalated cap (a thinking generator can overspend the
    budget on thoughts — the 2026-08-05 guardrail run traced every decision/
    rerank failure to this); if the cap is already at the ceiling, truncation
    fails fast. Never retries HTTP errors: retryable statuses and transport
    blips are already retried inside the providers' `_post`, and what escapes
    it is terminal.

    Returns `(parsed, acct)`; `parsed is None` means every attempt failed and
    the caller applies its site-specific fallback.
    """
    acct = GenAccounting()
    cap = max_output_tokens
    escalated = False
    for _ in range(attempts):
        acct.attempts += 1
        result: GenerationResult | None = None
        try:
            result = llm.generate(
                prompt, system=system, json_schema=json_schema,
                max_output_tokens=cap,
            )
            parsed = parse_json_text(result)
            if not isinstance(parsed, dict):
                raise ValueError(f"expected JSON object, got {type(parsed).__name__}")
            acct._absorb(result)
            return parsed, acct
        except (ValueError, KeyError, IndexError) as exc:  # parse + provider shape
            if result is not None:
                acct._absorb(result)
            _log_failure(acct, exc, result)
            if _finish_reason(result) in _TRUNCATION_MARKERS:
                new_cap = min(cap * 2, _ESCALATION_CEILING)
                if escalated or new_cap <= cap:
                    break  # already escalated (or at ceiling) — deterministic
                escalated = True
                cap = new_cap
                acct.failures[-1]["escalated_to"] = new_cap
    return None, acct


def generate_text(
    llm: LLMClient,
    prompt: str,
    *,
    attempts: int = 3,
    **kwargs: Any,
) -> tuple[GenerationResult, GenAccounting]:
    """Plain-text generation with shape retries (no parse step) — for non-JSON
    sites (e.g. image OCR; `kwargs` passes provider extras like `images=`).
    Raises the last shape error after `attempts`; HTTP errors propagate
    immediately (see `generate_json`)."""
    acct = GenAccounting()
    last_error: Exception | None = None
    for _ in range(attempts):
        acct.attempts += 1
        try:
            result = llm.generate(prompt, **kwargs)
            acct._absorb(result)
            return result, acct
        except (KeyError, IndexError) as exc:  # provider response-shape only
            last_error = exc
            _log_failure(acct, exc, None)
    raise RuntimeError(
        f"generation failed after {acct.attempts} attempts: {last_error}"
    ) from last_error
