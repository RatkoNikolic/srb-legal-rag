# srb-legal-rag

A Serbian legal question-answering system, and an exploratory benchmark built on it: **the same legal question answered by five instrumented retrieval architectures over one shared, deeply-constructed knowledge base**, with accuracy, provenance fidelity, temporal correctness, latency and cost measured side by side.

The corpus is Serbian statutory law from the official *Pravno-informacioni sistem RS*. The knowledge base measured here held 536 materialized acts and 25,675 retrievable article-level units, each carrying validity dates and a citable provenance record. **The corpus itself is not in this repository** — the pipeline that builds it is (see [Building the knowledge base](#building-the-knowledge-base)).

## The result

Five arms, 60 situational client questions, single seed:

| arm | accuracy | 95% CI | prov. F1 | $/query |
|---|---|---|---|---|
| Agentic (ReAct loop) | **0.867** | [0.758, 0.931] | 0.611 | 0.068 |
| Advanced + Graph | 0.767 | [0.646, 0.856] | 0.540 | 0.037 |
| Advanced (hybrid + rerank) | 0.733 | [0.610, 0.829] | 0.558 | 0.030 |
| Naive RAG | 0.483 | [0.362, 0.607] | 0.456 | 0.015 |
| Context-stuffing (oracle ceiling) | 0.917 | [0.819, 0.964] | 0.652 | 0.197 |

Every engineered arm beats the naive baseline decisively (McNemar p ≤ 0.0003). **Typed graph structure does not beat strong flat retrieval** on this corpus (p = 0.6875) — a negative result, instrumented so the cause is measurable rather than asserted. The agentic arm leads on every quality axis, though its accuracy lead does not survive multiple-comparison correction.

Judge agreement (cross-family Krippendorff's α) is **0.764**, below the conventional 0.80 threshold. The report diagnoses it: 81% of the disagreement is the correct-vs-partial boundary (ordinal α = **0.842**, above threshold), and **the ranking and every engineered-vs-naive p-value hold when re-derived from the second judge's labels**. On the accuracy-deciding call alone, binarised α = **0.781** — still marginally under 0.80, so the caveat is bounded rather than removed.

**→ [Full campaign report](./docs/benchmarking/benchmark_report_situational_60_5arm_20260808.md)** — methodology, all metrics, significance tests, and what the evaluation does not establish.

## How it works

Six self-contained design documents, each covering one part of the system and citing the literature behind its choices:

| | |
|---|---|
| [data_sources](./docs/design/data_sources.md) | where the law comes from, depth levels, what is and isn't fetchable |
| [ingestion_pipeline](./docs/design/ingestion_pipeline.md) | fetch → parse → chunk → embed → graph |
| [knowledge_base](./docs/design/knowledge_base.md) | the data model, the temporal model, the two stores |
| [rag_arms](./docs/design/rag_arms.md) | the five arms and the shared contract |
| [benchmark](./docs/design/benchmark.md) | questions, metrics, methodology, the negative result |
| [ui](./docs/design/ui.md) | the web app |

[architecture](./docs/architecture.md) is the map: package boundary, seams, stack choices.

## Two invariants

1. **No claim without provenance.** Every retrievable unit carries its law, article, version and ELI URL. An answer with no citations is only legal when it abstains.
2. **Correctness is time-indexed.** Statutes amend. A confidently-cited repealed article is worse than no answer, so every unit carries validity dates and constitutional invalidation is modelled as a temporal death.

## Run it

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). No Git LFS, no large
objects — the repository is ~6 MB and contains code, tests, questions and documentation only.

```bash
git clone <repo> && cd srb-legal-rag
uv venv && uv pip install -e ".[dev]"
cp .env.example .env                     # add your own GEMINI_API_KEY
```

## Building the knowledge base

**This repository ships the machinery, not the corpus.** There is no statute text in it. What
ships is the pipeline that constructs a knowledge base, the registry that defines *which* laws go
in, the benchmark questions, and the measurement record behind the published results.

Building your own KB is three commands:

```bash
python -m lexsr.ingest all    # crawl PIS → parse → chunk → annexes   (~7 min, rate-limited)
python -m lexsr.kg            # build the typed temporal KG           (free, local, ~2 s)
python -m lexsr.index         # embed into LanceDB                    (~$1.50, GEMINI_API_KEY)
```

Step 1 resolves each law in `src/lexsr/registry.py` against the *Pravno-informacioni sistem RS*,
fetches its consolidated text and every dated version slice, parses člán-level units with their
three validity dates, then walks the typed reference graph and fetches each delegated bylaw and
Constitutional-Court decision it finds. Targets that return no article structure become graph-only
stub nodes. Steps 2 and 3 are purely local and deterministic.

**It crawls the source. It does not re-parse a local snapshot.** Step 1 is a live, rate-limited
(3 s) crawl of a third-party system, identifying itself by user agent and touching only the free
section. Running it makes you the crawler — please read the position in
[data_sources](./docs/design/data_sources.md) §6–§7 before you do.

### Your rebuild will not match the published one

This was tested, not assumed, and the differences are not small:

| | measured for the report | a rebuild today |
|---|---|---|
| retrievable units | 25,675 | 25,188 |
| article chunks | 24,598 | **25,188** — every original act intact, **+17 new bylaws (+590)** |
| annex chunks (`table_html` / `ocr`) | 910 / 167 | **0 / 0** |
| stub nodes | 1,707 | 1,740 |
| reference edges | 3,547 | 3,575 |
| cross-law edges | 264 | 270 |

The causes, in order of size:

- **The source moves, and mostly it grows.** Serbian law is amended continuously: bylaws are
  published, withdrawn and re-issued, and version timelines shift. Act by act, the rebuild lost
  nothing and added 17 delegated bylaws that did not exist when the campaign ran. A KB is a
  point-in-time capture, and no amount of shipped data fixes that.
- **Annexes need a paid step.** The 1,077 annex chunks come from HTML-table extraction and
  vision-OCR of embedded images; the OCR half is model output and varies run to run even on
  identical inputs.
- **Materialize-vs-stub flips.** A target that yielded no article structure once may parse the
  next time, moving acts between retrievable and graph-only.
- **Transient fetch failures are absorbed**, by design, so one unreachable document silently
  shrinks the corpus rather than failing the run.

**So re-running the benchmark against your own KB will not reproduce the numbers in the report,
and any difference you observe is a difference in corpora, not in retrieval architectures.**
Comparing arms *within* your own KB is valid — that is the controlled comparison the benchmark is
built to make.

**The exact knowledge base used for the published campaign — the full chunk set, the typed KG and
the vector index — is available on request: ratkonikolic92@gmail.com.**

Then:

```bash
.venv/bin/uvicorn lexsr.api:app --port 8000     # API + UI on http://localhost:8000
.venv/bin/python -m pytest -q                   # 224 tests
cd web && npm install && npm run dev            # UI dev server (optional)
```

Re-running the benchmark needs `ANTHROPIC_API_KEY` too (the grader is a different model family from the generator, by design):

```bash
python -m lexsr.evals --arms naive,advanced,graph,agentic,context-stuffing --dual-judge
```

## Layout

```
src/kl_core/   domain-free seams, types, eval harness
src/lexsr/     the Serbian-legal skin: ingestion, stores, the five arms, API
web/           React UI
goldens/       ground-truth question sets
tests/         224 contract tests, pinned against real statute fixtures
data/corpus/   metadata describing the measured knowledge base; no statute text
```

## Legal position

Serbian statute text is excluded from copyright (Zakon o autorskom i srodnim pravima, čl. 6) and free access to it is statutory. The crawler identifies itself, rate-limits, and touches only the free section; commercial aggregators are never scraped. The full position, including the residual risk under the source's terms of use, is documented in [data_sources](./docs/design/data_sources.md) §6–§7.

## Acknowledgements

**[Saša Mirković](https://github.com/SasaMirkovic)** co-designed the **ingestion pipeline** and the **knowledge base**,
and advised on the retrieval architecture and the evaluation design.

Responsibility for the results reported here, and for any errors in them, is the author's.

## Licence

Two licences, because this repository holds two different kinds of work.

- **Code** — everything under `src/`, `tests/`, `web/` and `scripts/` — is
  [Apache-2.0](./LICENSE). Use it commercially, modify it, embed it; keep the notice,
  state your changes. It carries an explicit patent grant.
- **Benchmark question sets** — every `*.jsonl` under [`goldens/`](./goldens/) — are
  [CC BY 4.0](./goldens/LICENSE). Reuse them, including in published work, with
  attribution.

The questions are data rather than software, and they are the original contribution
most likely to be reused on its own, so they carry a licence written for that purpose.

The Serbian statutes the gold answers cite are **not** covered by either licence:
statute text is excluded from copyright (Zakon o autorskom i srodnim pravima, čl. 6).
What is licensed is the authored material — the questions, the answers, the selection
and arrangement of citations, and the labels.
