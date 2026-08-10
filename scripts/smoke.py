"""Live integration smoke: full stack against localhost:8000 (real model calls).

Asserts the behaviors the benchmark depends on:
- all arms answer a Cyrillic and a Latin-script question with citations
- the BM25/FTS leg actually fires for Latin queries (transliteration fix)
- tax-cluster content is reachable (the paušal question — asserted on retrieval,
  not on the model's abstain choice, which is its judgment call)
- the whole-law-repealed ZZPL 2008 is visible to retrieval with valid_to
- out-of-corpus questions abstain
- telemetry endpoints are consistent with the runs just made

Usage: .venv/bin/python scripts/smoke.py
"""

from __future__ import annotations

import os
import sys

import httpx2

API = os.environ.get("LEXSR_SMOKE_API", "http://localhost:8000")
# 360 s: the providers' output caps/timeouts were raised (32k-token answers can
# legitimately generate for minutes) — the smoke client must outlast them.
client = httpx2.Client(base_url=API, timeout=360.0)

PASS, FAIL = 0, 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    status = "PASS" if condition else "FAIL"
    if condition:
        PASS += 1
    else:
        FAIL += 1
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""), flush=True)


def ask(
    question: str,
    arm: str,
    as_of: str | None = None,
    *,
    history: list[dict] | None = None,
    conversation_id: str | None = None,
) -> dict:
    response = client.post(
        "/api/ask",
        json={
            "question": question,
            "arm": arm,
            "as_of": as_of,
            "history": history or [],
            "conversation_id": conversation_id,
        },
    )
    response.raise_for_status()
    return response.json()


def main() -> int:
    corpus = client.get("/api/corpus").json()
    check("corpus has ≥29 laws", len(corpus) >= 29, f"{len(corpus)} laws")
    check(
        "corpus spans all ten clusters + backbone",
        {law["cluster"] for law in corpus} == {str(i) for i in range(1, 11)} | {"backbone"},
    )
    zzpl08 = next((law for law in corpus if law["slug"] == "zzpl-2008"), None)
    check(
        "ZZPL 2008 ingested as law-level repealed",
        bool(zzpl08 and zzpl08["law_valid_to"] == "2019-08-21"),
    )
    zoo = next((law for law in corpus if law["slug"] == "zoo"), None)
    check(
        "ZOO (1978, SFRJ gazette) has clean title + parsed chain",
        bool(zoo and zoo["title"].strip() == "ЗАКОН о облигационим односима"
             and len(zoo["gazette_chain"]) >= 4),
        f"chain={zoo['gazette_chain'] if zoo else None}",
    )

    baseline_runs = client.get("/api/stats").json()["total_runs"]

    # every arm, Cyrillic
    for arm in ("naive", "advanced", "graph"):
        run = ask("Колико износи минимална зарада и ко је утврђује?", arm)
        check(
            f"{arm}: Cyrillic question answered with citations",
            (not run["abstained"]) and len(run["citations"]) > 0,
            f"{round(run['timings']['total_ms'])} ms, ${run['cost_usd']:.4f}",
        )

    # Latin script through the hybrid arm — the BM25 fix path
    run = ask("Koliki je otkazni rok kod otkaza ugovora o radu od strane zaposlenog?", "advanced")
    check(
        "advanced: Latin-script question answered with citations",
        (not run["abstained"]) and len(run["citations"]) > 0,
    )
    check(
        "advanced: FTS leg contributed for Latin query",
        any("fts" in r["stage"] for r in run["retrieved"]),
        "stages: " + ",".join(sorted({r["stage"] for r in run["retrieved"]})),
    )

    # tax cluster reachable — the paušal question. Asserted on the RETRIEVED set
    # (porez-dohodak must reach the pool), not on abstained/citations: whether
    # the model answers from the general provisions is its judgment call, and a
    # slice-flood regression (2026-07-29) once made this abstain by starving
    # retrieval — that's the failure mode this check pins.
    run = ask("Koliki su mi porezi za paušalca koji nudi usluge kompjuterskog programiranja?",
              "advanced")
    check(
        "tax cluster: paušal question retrieves porez na dohodak",
        any("доходак" in c.get("source_title", "").lower() for c in run["retrieved"]),
        "; ".join(c.get("source_title", "")[:40] for c in run["retrieved"][:4]),
    )

    # whole-law repeal visible to retrieval
    run = ask("Да ли је Закон о заштити података о личности из 2008. године још увек на снази?",
              "advanced")
    mentions_repeal = ("не важи" in run["answer_text"].lower()
                       or "престао" in run["answer_text"].lower()
                       or "стављен ван снаге" in run["answer_text"].lower())
    check("ZZPL 2008: answer states the law is no longer in force",
          not run["abstained"] and mentions_repeal, run["answer_text"][:120])

    # out-of-corpus → abstain
    run = ask("Која је казна за кривично дело пореске утаје?", "advanced")
    check("out-of-corpus criminal-law question abstains", run["abstained"],
          (run["abstain_reason"] or "")[:80])

    # graph arm exercises expansion on the cross-reference backbone (ZOO)
    run = ask("Када застарева потраживање накнаде штете?", "graph")
    check(
        "graph: damages question answered (ZOO reachable)",
        not run["abstained"],
        f"expanded={run['extras'].get('expanded_count')}",
    )

    # agentic arm: the ReAct loop answers and records a tool trace
    run = ask("Шта прописује члан 69 Закона о раду о годишњем одмору?", "agentic")
    check(
        "agentic: answered from gathered evidence with a logged tool trace",
        not run["abstained"]
        and len(run["citations"]) > 0
        and isinstance(run["extras"].get("tool_trace"), list)
        and len(run["extras"]["tool_trace"]) > 0,
        f"steps={run['extras'].get('steps_used')}, tools={run['extras'].get('tools_called')}, "
        f"${run['cost_usd']:.4f}",
    )

    # LLM-resilience observability: every run's extras carry the per-stage
    # attempt/finish_reason record (llm_io) end-to-end
    llm_io = run["extras"].get("llm_io") or {}
    check(
        "observability: llm_io.answer records attempts + finish_reason",
        isinstance(llm_io.get("answer"), dict)
        and llm_io["answer"].get("attempts", 0) >= 1
        and "finish_reason" in llm_io["answer"],
        f"llm_io={ {k: v.get('attempts') for k, v in llm_io.items() if isinstance(v, dict)} }",
    )

    # conversation continuity: a follow-up that only resolves against turn 1.
    # Both golds live in the statute text (ZOR čl. 69 / čl. 73) — a question
    # whose literal answer is OUTSIDE the corpus (e.g. the minimum-wage dinar
    # amount, set by decision not statute) is a legitimate abstention for a
    # literal-minded generator and would make this check flaky.
    q1 = "Колико најмање радних дана годишњег одмора следује запосленом?"
    turn1 = ask(q1, "advanced", conversation_id="smoke-conv")
    turn2 = ask(
        "А да ли може да се користи у деловима?",
        "advanced",
        conversation_id="smoke-conv",
        history=[
            {"role": "user", "content": q1},
            {"role": "assistant", "content": turn1["answer_text"]},
        ],
    )
    condensed = (turn2["extras"].get("condensed_query") or "").lower()
    check(
        "continuity: follow-up condensed into a standalone query + answered in context",
        not turn1["abstained"]
        and condensed not in ("", "а да ли може да се користи у деловима?")
        and not turn2["abstained"]
        and len(turn2["citations"]) > 0,
        f"condensed={turn2['extras'].get('condensed_query')!r}",
    )

    # artifact coherence: the vector index must hold exactly the corpus's
    # chunk_ids (the ghost-row class — rows deleted from chunks.jsonl but
    # never from the store — and the reverse). Local check, not via the API:
    # smoke runs on the host with the venv + data/ present.
    try:
        import json as _json
        from pathlib import Path as _Path

        from lexsr.index import open_table

        corpus_ids = {
            _json.loads(line)["chunk_id"]
            for line in _Path("data/corpus/chunks.jsonl").open(encoding="utf-8")
        }
        table = open_table()
        index_ids = {
            r["chunk_id"]
            for r in table.search().select(["chunk_id"]).limit(len(corpus_ids) + 1000).to_list()
        }
        check(
            "index ≡ corpus (chunk_id sets match, no ghosts either way)",
            index_ids == corpus_ids,
            f"index-only={len(index_ids - corpus_ids)} corpus-only={len(corpus_ids - index_ids)}",
        )
    except Exception as exc:  # data/ not present (remote smoke) → report, don't crash
        check("index ≡ corpus (chunk_id sets match, no ghosts either way)", False, str(exc))

    # telemetry consistency
    stats = client.get("/api/stats").json()
    check("stats counted the new runs", stats["total_runs"] >= baseline_runs + 8,
          f"{stats['total_runs']} total")
    runs_list = client.get("/api/runs").json()
    detail = client.get(f"/api/runs/{runs_list[0]['id']}").json()
    check(
        "run drill-down carries retrieved chunks + stage timings",
        len(detail["retrieved"]) > 0 and "total_ms" in detail["timings"],
    )

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
