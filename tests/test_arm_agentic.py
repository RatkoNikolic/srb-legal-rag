"""Agentic-arm contract: the ReAct loop dispatches tools, respects the step
budget, threads gathered evidence into a cited answer, and logs a tool trace —
all driven by a scripted fake LLM (no network)."""

import json
import re

import pytest

from kl_core.llm import GenerationResult
from lexsr.arms.agentic import (
    AGENT_SYSTEM,
    BYLAW_ACTS,
    BYLAW_SUB_K,
    MAX_STEPS,
    POOL_CAP,
    AgenticArm,
)
from lexsr.arms.base import RetrievedChunk


class ScriptedLLM:
    """Returns the queued JSON payloads in order, one per generate() call.
    A plain-string payload is returned verbatim (an unparseable generation)."""

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = 0

    def generate(self, prompt, *, system=None, json_schema=None,
                 temperature=0.0, max_output_tokens=8192):
        payload = self.payloads[self.calls] if self.calls < len(self.payloads) else {}
        self.calls += 1
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return GenerationResult(
            text=text,
            model="fake", prompt_tokens=10, output_tokens=5, cost_usd=0.001, latency_ms=1.0,
        )


def _row(clan):
    return {
        "chunk_id": f"sgrs/skupstina/zakon/2005/24/1/clan-{clan}",
        "law_slug": "zakon-o-radu", "source_title": "ЗАКОН о раду",
        "fragment": f"član {clan}", "text": f"Текст члана {clan}.",
        "valid_from": "2005-03-15", "valid_to": "", "repealed": False,
        "url": "https://example.org/eli", "version": "", "_distance": 0.1,
    }


class FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def where(self, clause):
        if "is_version_slice" in clause:  # the slice-flood guard
            self._rows = [r for r in self._rows if not r.get("is_version_slice")]
        m = re.search(r"source_id = '([^']*)'", clause)
        if m:  # get_bylaws' scoped per-act sub-search
            self._rows = [r for r in self._rows if r.get("source_id") == m.group(1)]
        return self

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    def to_list(self):
        return list(self._rows)


class FakeTable:
    def __init__(self, rows):
        self.rows = rows

    def search(self, query=None, **kwargs):
        return FakeQuery(list(self.rows))


class FakeEmbedder:
    def __init__(self):
        self.embedded = []  # every text passed to embed(), in call order

    def embed(self, texts, *, task_type):
        self.embedded.extend(texts)

        class B:
            vectors = [[0.0]] * max(1, len(texts))
        return B()


def _arm(payloads, *, rows=None, kg=None, manifest=None, config=None, llm=None):
    # Loop-contract tests run with the seed search OFF so each scripted payload
    # maps 1:1 to a loop decision; the seed feature has its own dedicated tests.
    from lexsr.arms.base import AblationConfig
    arm = AgenticArm(
        llm=llm if llm is not None else ScriptedLLM(payloads),
        embedder=FakeEmbedder(),
        table=FakeTable(rows if rows is not None else [_row("69"), _row("70")]),
        kg_conn=kg,  # a real (mini) KGStore when a test exercises KG tools
        config=config or AblationConfig(seed_search=False),
    )
    arm._manifest = manifest if manifest is not None else []
    return arm


def test_loop_searches_then_finishes_and_cites():
    arm = _arm([
        {"thought": "треба ми члан о одмору", "tool": "search_corpus", "query": "годишњи одмор"},
        {"thought": "имам довољно", "tool": "finish"},
        {"answer": "Најмање 20 радних дана.", "citations": [0], "abstained": False},
    ])
    result = arm.answer("Колико траје годишњи одмор?")
    assert not result.answer.abstained
    assert result.answer.citations[0].fragment == "član 69"
    trace = result.extras["tool_trace"]
    assert [t["tool"] for t in trace] == ["search_corpus", "finish"]
    assert result.extras["steps_used"] == 2
    # condensation is skipped without history
    assert "condensed_query" not in result.extras


def test_step_budget_is_bounded():
    # never emits finish → loop must stop at MAX_STEPS dispatches
    searches = [{"thought": "опет", "tool": "search_corpus", "query": "x"}] * (MAX_STEPS + 3)
    answer = [{"answer": "x", "citations": [0], "abstained": False}]
    arm = _arm(searches + answer)
    result = arm.answer("Питање?")
    assert result.extras["steps_used"] == MAX_STEPS
    assert len(result.extras["tool_trace"]) == MAX_STEPS


def test_get_clan_dispatch_fetches_exact_article(make_kg):
    sid = "sgrs/skupstina/zakon/2005/24/1"
    store = make_kg(
        akti=[{"source_id": sid, "slug": "zakon-o-radu", "title": "ЗАКОН о раду",
               "eli": "https://example.org/eli"}],
        clanovi=[{"chunk_id": f"{sid}/clan-52", "source_id": sid, "law_slug": "zakon-o-radu",
                  "number": "52", "fragment": "član 52", "text": "Прековремени рад…",
                  "valid_from": "2005-03-15"}],
    )
    arm = _arm(
        [
            {"thought": "прати референцу", "tool": "get_clan",
             "law_slug": "zakon-o-radu", "broj": "52"},
            {"thought": "готово", "tool": "finish"},
            {"answer": "Види члан 52.", "citations": [0], "abstained": False},
        ],
        kg=store,
        manifest=[{"slug": "zakon-o-radu", "source_id": sid}],
    )
    result = arm.answer("Шта каже члан 52?")
    assert result.answer.citations[0].fragment == "član 52"
    assert result.retrieved[0].stage == "agent-get_clan"
    assert result.retrieved[0].source_title == "ЗАКОН о раду"


def test_out_of_corpus_abstains_via_list_laws():
    arm = _arm(
        [
            {"thought": "проверавам опсег", "tool": "list_laws"},
            {"thought": "није у корпусу", "tool": "finish"},
            {"answer": "", "citations": [], "abstained": True,
             "abstain_reason": "питање није у корпусу"},
        ],
        manifest=[{"slug": "zakon-o-radu", "title": "ЗАКОН о раду"}],
    )
    result = arm.answer("Колика је казна за убиство?")
    assert result.answer.abstained
    # research guardrail: list_laws gathered no evidence and no search ran, so
    # the harness forces one seeded search before the conclusion — the
    # abstention survives, but it is now a "searched, found this" abstention
    assert result.extras["forced_search"] is True
    assert result.retrieved != []  # forced search pulled the FakeTable rows
    forced = [t for t in result.extras["tool_trace"] if t.get("forced")]
    assert len(forced) == 1 and forced[0]["tool"] == "search_corpus"


def test_unparseable_decision_breaks_loop_cleanly():
    arm = _arm([{"not": "an action"}, {"answer": "", "citations": [], "abstained": True}])
    # the bad decision has no 'tool' → defaults to finish → clean exit
    result = arm.answer("Питање?")
    assert result.answer.abstained


def test_decision_parse_failures_are_retried_and_logged():
    # attempt 1+2 unparseable, attempt 3 a valid search — the step survives
    arm = _arm([
        "нисам JSON",
        "ни ја",
        {"thought": "тражим", "tool": "search_corpus", "query": "одмор"},
        {"thought": "доста", "tool": "finish"},
        {"answer": "Одговор.", "citations": [0], "abstained": False},
    ])
    result = arm.answer("Питање?")
    assert not result.answer.abstained
    retries = [t for t in result.extras["tool_trace"] if t["tool"] == "__decision_retry__"]
    assert len(retries) == 2 and all(t["error"] for t in retries)
    io = result.extras["llm_io"]["decision"]
    assert io["attempts"] == 4 and io["giveups"] == 0  # 3 for step 1 + 1 finish


def test_decision_giveup_forces_search_before_conclusion():
    # three unparseable decisions → give-up → the research guardrail runs one
    # seeded search so the answer stage never sees an empty, never-searched pool
    arm = _arm([
        "x", "y", "z",
        {"answer": "", "citations": [], "abstained": True, "abstain_reason": "нема података"},
    ])
    result = arm.answer("Питање?")
    assert result.extras["forced_search"] is True
    assert result.extras["llm_io"]["decision"]["giveups"] == 1
    assert result.retrieved != []  # FakeTable rows entered the pool
    assert result.extras["pool_size"] > 0


def test_search_that_found_results_disarms_the_guardrail():
    # a real search happened and the pool is non-empty → no forced search
    arm = _arm([
        {"thought": "тражим", "tool": "search_corpus", "query": "одмор"},
        {"thought": "крај", "tool": "finish"},
        {"answer": "Одговор.", "citations": [0], "abstained": False},
    ])
    result = arm.answer("Питање?")
    assert result.extras["forced_search"] is False
    assert not any(t.get("forced") for t in result.extras["tool_trace"])


def _chunk(cid, stage):
    return RetrievedChunk(chunk_id=cid, score=1.0, stage=stage, source_title="З", fragment=cid,
                          text="…")


def test_cap_pool_retention_policy():
    """`_cap_pool` retention, one contract, five situations it must satisfy.

    Class rules: `agent-seed` chunks are immutable (the flat-search superset
    guarantee), deliberate fetches (`agent-get_clan`, `agent-cc`) are never
    evicted by search volume, `agent-search` is research-class, and a bulk
    `agent-bylaw` fan-out gets exactly BYLAW_QUOTA slots. Cases 3-5 are the
    2026-08-06 eviction-bug wave (h06 forensics): a bylaw flood used to evict
    every search chunk (final pool 24/24 agent-bylaw), and an unbounded
    get_clan/expand flood used to push the pool ABOVE the cap because the room
    formula never trimmed protected chunks. Order is always preserved —
    citations are pool indices downstream."""

    # 1. a deliberately fetched article survives a search flood; oldest searches go
    fetched = _chunk("clan-69", "agent-get_clan")
    searches = [_chunk(f"s-{i}", "agent-search") for i in range(POOL_CAP + 5)]
    capped = AgenticArm._cap_pool([fetched] + searches)
    assert len(capped) == POOL_CAP, "deliberate+searches: capped"
    assert fetched in capped, "get_clan article is never evicted"
    assert "s-0" not in {c.chunk_id for c in capped}, "oldest searches dropped first"

    # 2. deliberate fetches filling the cap drop every search (-0 slice footgun)
    deliberate = [_chunk(f"d-{i}", "agent-get_clan") for i in range(POOL_CAP)]
    capped = AgenticArm._cap_pool(
        deliberate + [_chunk(f"s-{i}", "agent-search") for i in range(4)]
    )
    assert len(capped) == POOL_CAP, "deliberate-fills-cap: capped"
    assert all(c.stage == "agent-get_clan" for c in capped), "no searches survive"

    # 3. h06 replay: a bylaw flood must not evict seeds or searches
    seeds = [_chunk(f"seed-{i}", "agent-seed") for i in range(6)]
    searches = [_chunk(f"s-{i}", "agent-search") for i in range(12)]
    bylaws = [_chunk(f"b-{i}", "agent-bylaw") for i in range(24)]
    pool = seeds + searches + bylaws
    capped = AgenticArm._cap_pool(pool)
    kept = {c.chunk_id for c in capped}
    assert len(capped) == POOL_CAP, "bylaw flood: capped"
    assert all(s.chunk_id in kept for s in seeds), "seeds immutable"
    assert all(s.chunk_id in kept for s in searches), "h06 cost-answer chunk class kept"
    assert sum(1 for c in capped if c.stage == "agent-bylaw") == 6, "BYLAW_QUOTA, newest"
    assert [c.chunk_id for c in capped] == [
        c.chunk_id for c in pool if c.chunk_id in kept
    ], "order-preserving filter"

    # 4. the mirror flood: protected chunks are trimmed too, never overflow the cap
    deliberate = [_chunk(f"d-{i}", "agent-get_clan") for i in range(30)]
    capped = AgenticArm._cap_pool(
        seeds + deliberate + [_chunk(f"s-{i}", "agent-search") for i in range(8)]
    )
    assert len(capped) == POOL_CAP, "protected overflow bounded (old code returned 36)"
    assert all(s.chunk_id in {c.chunk_id for c in capped} for s in seeds), "seeds immutable"

    # 5. CC holdings are the same gated-fetch class as get_clan
    cc = _chunk("ref/350758/holding", "agent-cc")
    capped = AgenticArm._cap_pool(
        [cc] + [_chunk(f"s-{i}", "agent-search") for i in range(POOL_CAP + 5)]
    )
    assert cc in capped, "cc holding survives a search flood"


def test_get_bylaws_flood_capped_and_missing_query_falls_back_to_question(make_kg):
    # FIX_PLAN B1(b)+B2: the h06/h10/h15 malformed call (chunk_id, no query)
    # used to embed the law slug and pull 8×3=24 chunks; now the question is
    # embedded, the observation says so, and the fan-out is ≤ BYLAW_ACTS acts
    sid = "sgrs/skupstina/zakon/2023/35/2"
    akti = [{"source_id": sid, "slug": "zakon-o-bzr", "title": "ЗАКОН о БЗР", "eli": "e"}]
    edges, rows = [], []
    for b in range(6):  # more bylaw acts than the new fan-out
        bsid = f"ref/43700{b}"
        akti.append({"source_id": bsid, "slug": bsid, "title": f"Правилник {b}",
                     "vrsta": "pravilnik", "eli": "e"})
        edges.append((sid, bsid))
        for c in range(1, 6):
            rows.append({**_row(str(c)), "chunk_id": f"{bsid}/clan-{c}", "source_id": bsid,
                         "law_slug": bsid, "vrsta": "pravilnik"})
    kg = make_kg(akti=akti, pravni_osnov_za=edges)
    arm = _arm([], rows=rows, kg=kg, manifest=[{"slug": "zakon-o-bzr", "source_id": sid}])
    question = "Ко сноси трошкове мера заштите?"
    obs, chunks = arm._dispatch("get_bylaws", {"law_slug": sid, "chunk_id": "x"}, None, question)
    assert "НАПОМЕНА" in obs and "query" in obs  # the malformed call is called out
    assert arm.embedder.embedded == [question]  # the question, not the law slug
    assert 0 < len(chunks) <= BYLAW_ACTS * BYLAW_SUB_K  # ≤ 12 < POOL_CAP
    acts = {c.chunk_id.rsplit("/", 1)[0] for c in chunks}
    assert len(acts) <= BYLAW_ACTS


def test_law_timeline_resolves_eli_source_id():
    # FIX_PLAN B3: the policy reads ELI ids off chunk_ids; the timeline used to
    # answer "није у корпусу" for them (dead tool in the e18/h02 traces)
    manifest = [{
        "slug": "pio", "source_id": "sgrs/skupstina/zakon/2003/34/1",
        "title": "ЗАКОН о ПИО", "gazette_chain": ["34/2003", "86/2019"],
        "main_clanovi": 100, "law_published": "2003-04-04",
        "law_valid_from": "2003-04-05",
    }]
    arm = _arm([], manifest=manifest)
    out = arm._law_timeline("sgrs/skupstina/zakon/2003/34/1")
    assert "није у корпусу" not in out
    assert "34/2003" in out
    assert arm._law_timeline("pio") == out  # the slug form still works


def test_get_clan_normalizes_latin_article_suffix(make_kg):
    # FIX_PLAN B4: chunk ids carry Cyrillic suffixes (clan-14а); a Latin "14a"
    # from the policy used to miss silently
    sid = "sgrs/skupstina/zakon/2001/24/1"
    store = make_kg(
        akti=[{"source_id": sid, "slug": "zakon-o-porezu-na-dohodak",
               "title": "ЗАКОН о порезу на доходак грађана", "eli": "e"}],
        clanovi=[{"chunk_id": f"{sid}/clan-14а", "source_id": sid,
                  "law_slug": "zakon-o-porezu-na-dohodak", "number": "14а",
                  "fragment": "član 14а", "text": "Примања по основу погодности…",
                  "valid_from": "2001-01-01"}],
    )
    arm = _arm([], kg=store,
               manifest=[{"slug": "zakon-o-porezu-na-dohodak", "source_id": sid}])
    obs, chunks = arm._get_clan(sid, "14a")  # Latin 'a'
    assert chunks and chunks[0].chunk_id.endswith("clan-14а")


def test_holding_snippet_starts_at_operative_part():
    # FIX_PLAN I3: CC holdings open with court-composition boilerplate longer
    # than OBS_SNIPPET — the operative part must reach the policy's observation
    boiler = "Уставни суд у саставу: председник и судије, на седници, донео је " * 8
    text = boiler + "Утврђује се да одредба члана 14а став 1. тачка 2) није у сагласности с Уставом."
    c = RetrievedChunk(chunk_id="ref/350758/holding", score=1.0, stage="agent-search",
                       source_title="Одлука УС", fragment="izreka", text=text)
    out = AgenticArm._format_chunks([c])
    assert "Утврђује се" in out


def test_agent_system_has_finish_gate():
    # FIX_PLAN I1: the sufficiency-bias nudge — cover EVERY sub-question before finish
    assert "СВАКИ део питања" in AGENT_SYSTEM


@pytest.mark.parametrize("name", ["agentic"])
def test_registered_in_arms(name):
    from lexsr.arms import ARMS
    assert name in ARMS


def test_seed_search_populates_pool_before_loop():
    # production default: the pool is seeded with a raw-question search, so the
    # loop refines instead of re-deriving retrieval (superset of the flat leg)
    from lexsr.arms.base import AblationConfig
    arm = _arm(
        [
            {"thought": "довољно ми је почетно", "tool": "finish"},
            {"answer": "Одговор.", "citations": [0], "abstained": False},
        ],
        config=AblationConfig(),  # seed_search=True by default
    )
    result = arm.answer("Колико траје годишњи одмор?")
    assert not result.answer.abstained
    trace = result.extras["tool_trace"]
    assert trace[0]["tool"] == "search_corpus" and trace[0].get("seed") is True
    assert trace[0]["step"] == 0 and trace[0]["chunks_added"] > 0
    assert result.extras["forced_search"] is False  # seed disarms the guardrail
    assert result.retrieved != []


def test_repeated_identical_call_is_annotated():
    # the duplicate-call guard injects a "already called" note into the prompt
    # the policy sees next (e20-class step burning: same get_clan three times)
    captured = []

    class CapturingLLM(ScriptedLLM):
        def generate(self, prompt, **kwargs):
            captured.append(prompt)
            return super().generate(prompt, **kwargs)

    llm = CapturingLLM([
        {"thought": "тражим", "tool": "search_corpus", "query": "одмор"},
        {"thought": "опет исто", "tool": "search_corpus", "query": "одмор"},
        {"thought": "крај", "tool": "finish"},
        {"answer": "Одговор.", "citations": [0], "abstained": False},
    ])
    result = _arm(None, llm=llm).answer("Питање?")
    assert not result.answer.abstained
    # the decision prompt AFTER the repeated call carries the annotation
    assert any("већ позван" in p for p in captured)
