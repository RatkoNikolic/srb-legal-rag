# srb-legal-rag — benchmark report

*Five retrieval architectures · `golden-situational-60` · 2026-08-08*

**Batch** `situational-60-20260808-5arm-v12` · 2026-08-08 · 5 arms × 60 items = **300 answers** (298 first-pass + 2 cells re-run after a generator truncation, §3.3) · judge **Claude Opus 5** + juror **Gemini 3.6 Flash**, Krippendorff's **α = 0.764** (below the 0.80 threshold — §4.8) · total cost **$20.91** · single seed, temperature 0.

> **Read this first — what to distrust.**
> 1. **Judge agreement is α = 0.764, below the 0.80 threshold** — diagnosed in §4.8: 81% of the disagreement is the correct-vs-partial boundary (ordinal α = 0.842), it is directional rather than noisy, and **the ranking and every engineered-vs-naive p-value are unchanged when re-derived from the juror's labels**. Margins of 2–3 items are still not safe; margins of 16+ are.
> 2. **The temporal cell is a mechanism demonstration, not a measurement** — 3 items, effective n = 1, and that one discriminating item was edited post-hoc (§3.1, §4.5).
> 3. **Two cells were re-run after a harness truncation and both scored correct** (§3.3); two tier readings depend on them.
> 4. **Single seed.** The intervals in §4.1 are item-sampling only and overlap for every engineered arm.
> 5. **The generator is a floating alias with measured run-to-run variance of ~6% of items.** At n = 60 that is ~3.6 items of churn; **any difference of ≤ 4 items here sits inside that envelope and should not be read as an effect** — which covers the oracle-vs-agentic gap (3 items) and the graph-vs-advanced delegated-norm difference (2 items).
>
> This report is self-contained on the batch named above. The per-run telemetry it is computed from — one SQLite database holding this batch and no other — is not distributed with the code, and is available on request (§2.1); every figure below is stated with the counts and denominators needed to check it internally. The scoring rules were revised before this run after an internal review found the provenance metrics period-blind and the temporal split determined by arm configuration rather than measured; §3.2 states each change and why.

This is the self-contained record of the campaign: the system under test, the benchmark, the full results with exact numbers, and the analysis. Audience: a reader who has not seen the repo.

---

## 1. Executive summary

Five retrieval architectures answered the same 60 situational Serbian legal questions over the same deeply-constructed knowledge base, with accuracy, provenance fidelity, temporal correctness, latency, and cost measured side by side:

| Finding | Evidence (this campaign) |
| --- | --- |
| **F1 — Retrieval engineering beats naive RAG.** | Agentic 0.87, Advanced+Graph 0.77, Advanced 0.73 vs Naive 0.48; paired exact McNemar **p = 0.0000 / 0.0001 / 0.0003**, naive losing 24, 18 and 16 of the discordant pairs. |
| **F2 — *Our* typed graph layer adds nothing over *our* strong flat backbone (the GraphRAG negative result, scoped).** | Advanced+Graph 0.77 vs Advanced 0.73: **2/4 discordant, p = 0.6875**. **Scope:** this implementation on this corpus. The equivalence guarantee (structure-off ≡ advanced) rules out an under-built *arm*, but not an under-tuned *hop* — the named cause is a non-relevance-ranked delegation fan-out, a fixable engineering property rather than a property of graph structure. |
| **F3 — The agentic loop leads on every quality axis; its accuracy lead does not survive multiple-comparison correction.** | Among the deployable arms: best correctness (0.933) and provenance F1 (0.611); lowest hallucination of *any* arm and misgrounding tied with the oracle (0.07/0.07), and **zero over-abstentions**. On accuracy, +0.133 over advanced (**2/10 discordant, p = 0.0386**) — across the six pairwise tests among the four deployable arms neither Bonferroni (α = 0.0083) nor the less conservative Holm step-down (α = 0.0167 at this rank) admits it. Against graph it is 3/9, p = 0.146. The defensible claim is the quality profile, not the accuracy lead. |
| **F4′ — Temporal validity is a knowledge-base property, demonstrated not measured.** | Where the in-force text at `as_of` is a superseded slice, an arm whose retrieval excludes slices *cannot* cite in-force law — structurally. The observed pattern is consistent with that (oracle 2/2, naive 1/2, validity-filtering arms 0/2) but is **not evidence of it**: the outcome is fixed by each arm's retrieval configuration before generation, so no arm could have scored otherwise. The dated cell has an **effective n of 1**, and the flag is a forced conditional — an arm avoids it by not citing the gold article at all, which naive does on e20 and agentic on e17, both at citation recall 0 (§4.5). |
| **F5 — The oracle ceiling is 0.92, and this campaign cannot resolve the gap to it.** | Context-stuffing with every gold act scores 0.917 at **$0.197/query (12.8× naive, 6.5× advanced)** and stays temporally blind by construction. Agentic vs oracle is **3/6 discordant, p = 0.508** — a null result on 9 discordant items, with essentially no power to detect the observed 3-item difference. |
| **F6 — Provenance is recall-bound, not citation-bound.** | Citation recall (0.53–0.87) tracks retrieval recall (0.56–0.97) with a gap ≤ 0.055 on four of five arms (≤ 0.035 on three). When provenance fails, the gold unit was usually never retrieved. |
| **F7 — (not a finding) The abstention axis is effectively untested.** | The one unanswerable item (e18): all five arms abstained and scored correct. n = 1; the item is unanswerable because of an addressing limitation rather than a property of law (§5.6); and judge agreement is not measured on this axis at all (§4.8). |

---

## 2. The system under test

Four parts, each described here in brief and in full in its own design document. This
report is readable on its own; the documents carry the rejected alternatives, the
literature behind each choice, and the open questions.

| part | in brief | in full |
|---|---|---|
| Ingestion | §2.1 | [ingestion_pipeline](../design/ingestion_pipeline.md) · [data_sources](../design/data_sources.md) |
| Knowledge base | §2.2 | [knowledge_base](../design/knowledge_base.md) |
| The five arms | §2.3 | [rag_arms](../design/rag_arms.md) |
| Benchmark design | §3 | [benchmark](../design/benchmark.md) |

### 2.1 Ingestion — how a corpus becomes a knowledge base

A registry of laws, not a folder of files, is the input: 33 seed laws across ten thematic
clusters plus a six-act shared backbone, declared in `src/lexsr/registry.py`. Everything
else is derived.

1. **Resolve and fetch.** Each registry entry is matched against the official
   *Pravno-informacioni sistem RS*, and its consolidated text plus every dated version
   slice is retrieved. The crawl is rate-limited, identifies itself, and touches only the
   freely-published section.
2. **Parse.** A dialect-aware parser splits each act into `član`-level units and recovers
   the three Serbian legal dates — *objavljivanje*, *stupanje na snagu*, *početak primene*
   — along with gazette chains and repeal markers. The markup varies materially between
   publication eras, so the parser is pinned against real fixtures per dialect.
3. **Expand.** The typed reference graph of each act is walked, and every delegated bylaw
   and Constitutional-Court decision it points to is fetched in turn. A target that yields
   no article structure is kept as a **stub node** — visible to the graph, not retrievable.
4. **Annexes.** Tabular annexes are extracted as HTML; image annexes go through vision OCR.
5. **Derive stores.** The flat artifacts are loaded into a vector + full-text index and a
   typed temporal knowledge graph.

Two properties of this design bear directly on the results. **Depth is chosen over breadth**
— the delegated-bylaw layer is followed exhaustively rather than sampled, which is what
makes the delegated-norm question tier answerable at all. And **step 1 crawls a living
source**, so a knowledge base is a point-in-time capture; §2.2 states what that costs in
reproducibility.

Full design, including the rejected alternatives and the refresh story:
[ingestion_pipeline](../design/ingestion_pipeline.md); the source tiers and what is and is
not fetchable: [data_sources](../design/data_sources.md).

### 2.2 The knowledge base (shared by every arm)

One corpus, built deep rather than broad, is the single foundation for all five arms — the controlled-variable design that makes the comparison valid.

- **Source:** the official *Pravno-informacioni sistem RS* (PIS), Serbia's statutory legal-information system. Statute text is excluded from copyright (Zakon o autorskom i srodnim pravima, čl. 6); free access is statutory.
- **Scope:** 33 seed laws across ten thematic clusters + a 6-act shared backbone, **plus every reachable delegated bylaw attempted** (475 of 1,021 delegation targets materialize; the remaining ~546 fetch as HTML but carry no article structure and stay stub nodes) and 21 Constitutional-Court decisions — **536 materialized acts**, 1,707 stub nodes, **25,675 retrievable units** (article-level chunks incl. 3,257 dated version slices, ~910 HTML-table annexes, ~167 vision-OCR'd image annexes).
- **Chunking:** one chunk per *(član × version-slice)* — the article is the established provenance and retrieval unit in statutory-retrieval research (Louis & Spanakis, ACL 2022 — BSARD; Chen et al., EMNLP 2024; Fadillah et al. 2026).
- **Stores:** a dense-vector + BM25 full-text index (LanceDB; gemini-embedding-2; hybrid retrieval per Karpukhin et al., EMNLP 2020 and Robertson & Zaragoza 2009) and a **typed temporal knowledge graph** in stdlib SQLite (11 edge types incl. `UPUCUJE_NA` cross-references, `PRAVNI_OSNOV_ZA` law→bylaw delegation, `OCENA_USTAVNOSTI` constitutional-validity links, term-aware `DEFINED_IN`; typed legal KGs per Filtz et al., *AI & Law* 2021).
- **The temporal model (the novelty axis):** every unit carries `[valid_from, valid_to]` valid-time intervals (Snodgrass, *ACM TODS* 1987; Jensen & Snodgrass, *IEEE TKDE* 1999; SQL:2011), with the Serbian three-date specialisation — *objavljivanje*, *stupanje na snagu* (incl. the constitutional +8-day default of Ustav čl. 196), *početak primene* — and **constitutional invalidation as a first-class temporal death**. Modelling per Akoma Ntoso (OASIS 2018) and LegalRuleML (Athan et al., ICAIL 2013); provenance identifiers are ELI URIs.
- **Two invariants enforced everywhere:** (1) *no claim without provenance*; (2) *correctness is time-indexed*.

**Reproducibility of this knowledge base — measured, not assumed.** The repository that
accompanies this report ships the *pipeline*, not the corpus: no statute text is distributed. A
knowledge base is constructed by crawling the source, and that construction was executed and
compared against the artifacts these results were measured on. It **does not reproduce them**:

- **The source moves, and it grows.** Serbian law is amended continuously, so a knowledge base is a
  point-in-time capture rather than a function of any shipped input. Comparing the two manifests act
  by act: **no act was lost and none was re-consolidated** — the rebuild is very nearly a strict
  superset. It materialized **17 delegated bylaws that did not exist in the corpus at campaign time
  (+590 article chunks)**, added 33 stub nodes (1,707 → 1,740), and grew the reference graph by 28
  edges (3,547 → 3,575, closure 9.3% → 9.2%). Two acts differ by a single repealed-article count,
  which is a genuine amendment landing between the runs.
- **All 1,077 annex chunks are absent** (910 HTML-table, 167 vision-OCR). They require a separate
  extraction step whose OCR half is model output, varying run to run even on identical inputs. This
  is the largest single difference: 25,675 units at campaign time against 25,188 on the rebuild —
  the article layer *gained* 590 while the annex layer lost all 1,077.
- **Nothing reproduces bit-for-bit, including the graph.** Cross-law edges moved 264 → 270, semantic
  edges 12,047 → 12,118, nodes 21,341 → 21,395. Only `in_corpus` reference edges (330) and graph
  completeness (100%) matched.

Consequently **re-running this benchmark against a rebuilt knowledge base will not reproduce the
numbers in this report**, and any difference so obtained is a difference in corpora, not in
retrieval architectures. Comparison *among the arms within a single knowledge base* — which is the
controlled comparison this benchmark makes — remains valid. The exact knowledge base used for this
campaign, including the typed KG and the vector index, **is available on request:
ratkonikolic92@gmail.com**.

Full data model, temporal semantics and store layout:
[knowledge_base](../design/knowledge_base.md).

### 2.3 The five arms (a monotonic capability ladder)

Each arm adds one class of capability over the previous, so metric deltas are attributable (hold retriever + reranker constant, report each method's gain over the common backbone).

1. **Context-Stuffing (the oracle ceiling, excluded from the deployable ranking).** Every gold-cited act is taken from the annotation (perfect act selection a priori) and stuffed whole into context, in gold order — the retrieval-vs-reasoning upper bound with the full gold-supporting-fact set, exactly as gold-context settings are used in HotpotQA (Yang et al., EMNLP 2018: "an upper bound on performance") and KILT (Petroni et al., NAACL 2021). Retrieves current consolidated text only (`is_version_slice = false`) — deliberately temporal-naive, which is a *design disclosure*, not a measured weakness.
2. **Naive RAG (the deployable floor).** Flat dense top-8, no fusion, no rerank, no temporal filter (Lewis et al., NeurIPS 2020; CRAG, Yang et al., NeurIPS 2024 D&B). Also excludes version slices — a real naive deployment would not index them.
3. **Advanced RAG (the smart flat backbone).** Dense + BM25 fused by Reciprocal Rank Fusion (Cormack et al., SIGIR 2009), an `as_of` in-force temporal filter, zero-shot listwise LLM reranking (Sun et al., EMNLP 2023), and a bylaw-aware per-act diversity cap (Carbonell & Goldstein, SIGIR 1998).
4. **Advanced + Graph (GraphRAG).** A strict superset of advanced: the *same* backbone output as seeds, plus typed-graph structure — bounded 2-hop `UPUCUJE_NA`/`DEFINED_IN` expansion, a delegation-gated law→bylaw hop with scoped hybrid sub-search, the constitutional-validity path, and a guarded related-law hop (Edge et al., arXiv:2404.16130; structure-off reproduces advanced exactly, pinned by a unit test).
5. **Agentic RAG.** A bounded single-agent ReAct loop (Yao et al., ICLR 2023), ≤10 steps over 7 typed tools forming a superset of the ladder's retrieval access, with a class-based pool-retention rule, a pre-loop seed search, and harness-integrity guards.

Every engineered capability is also a per-run **ablation toggle** (Self-RAG, Asai et al., ICLR 2024), and the exact configuration rides every run's telemetry.

**Generation is held constant:** the same generator (`gemini-3.6-flash`, official SDK, native structured outputs), prompts, chunking, and citation-mandatory answer contract for all arms. Only retrieval is the treatment.

---

Per-arm design, the shared contract, the ablation surface and the known recall ceiling:
[rag_arms](../design/rag_arms.md).

### 2.4 Where this sits in the research

Every component is chosen against published work rather than by preference, and the design
documents carry the citation for each. The short version:

- **The retrieval ladder.** Retrieve-then-generate follows Lewis et al. (NeurIPS 2020); the
  naive arm is the "straightforward RAG" reference point of CRAG (Yang et al., NeurIPS 2024
  D&B). The advanced backbone is dense + sparse hybrid (Karpukhin et al., EMNLP 2020;
  Robertson & Zaragoza 2009) fused by reciprocal rank fusion (Cormack et al., SIGIR 2009),
  with zero-shot listwise reranking (Sun et al., EMNLP 2023) and the anti-redundancy half of
  MMR (Carbonell & Goldstein, SIGIR 1998). The graph arm follows the GraphRAG paradigm (Edge
  et al., arXiv:2404.16130); the agentic arm is a bounded ReAct loop (Yao et al., ICLR 2023).
  Per-capability ablation follows the component-ablation convention (e.g. Self-RAG, Asai et
  al., ICLR 2024).
- **The unit of retrieval.** One chunk per *(član × version-slice)*: the article is the
  established provenance and retrieval unit in statutory retrieval (Louis & Spanakis, ACL
  2022 — BSARD; Chen et al., EMNLP 2024; Fadillah et al. 2026).
- **The temporal model.** Valid-time intervals per Snodgrass (*ACM TODS* 1987) and Jensen &
  Snodgrass (*IEEE TKDE* 1999), standardized in SQL:2011; legal-document temporal modelling
  per Akoma Ntoso (OASIS 2018) and LegalRuleML (Athan et al., ICAIL 2013); identifiers are
  ELI URIs. The Serbian three-date specialisation and constitutional-invalidation-as-temporal-
  death are this work's own contribution, and are flagged as such rather than attributed.
- **The measurement.** Cross-family LLM judging addresses the self-preference objection
  (Zheng et al., NeurIPS 2023 D&B), with graded correctness per RAGAs (Es et al., EACL 2024)
  and ARES (Saad-Falcon et al., NAACL 2024); citation attribution per ALCE (Gao et al., EMNLP
  2023), Liu et al. (Findings of EMNLP 2023) and AIS (Rashkin et al., *CL* 2023); the
  retrieval-versus-citation stage split per GraphRAG-Bench (Xiao et al., arXiv:2506.02404)
  and Xiang et al. (arXiv:2506.05690); legal-hallucination measurement per Magesh et al.
  (*JELS* 2025) and Dahl et al. (*J. Legal Analysis* 2024); significance by paired exact
  McNemar (Dietterich, *Neural Computation* 1998).
- **The gap this occupies.** Temporal legal retrieval is a recognised open problem — SAT-Graph
  RAG (de Martim, arXiv:2505.00039) is temporal-by-design but reports **no empirical
  evaluation**, and names the missing empirical temporal benchmark as a gap in the field. The
  nearest neighbours each cover part of the ground: NitiBench (arXiv:2502.10868) is Thai and
  compares RAG against long context; LegalSearch-R1 (arXiv:2605.25920) is Chinese temporal
  with a single method; BSARD is retrieval only; LexRAG (arXiv:2502.20640) is multi-turn
  consultation without a knowledge-layer comparison. What this study adds is the side-by-side
  architecture comparison on a low-resource civil-law corpus with provenance and temporal
  instrumentation — and, as §4.5 makes clear, a negative methodological finding about how such
  a temporal metric behaves.

Question typology, difficulty definitions, metric derivations and the negative result in
full: [benchmark](../design/benchmark.md). Full reference list: §8.

## 3. The benchmark

The question design, the difficulty definitions, the metric derivations and the
construction charter are set out in full in [benchmark](../design/benchmark.md); this section
states what was measured and how.

### 3.1 The golden set (`golden-situational-60`)

60 first-person, situational client questions — statute-unnamed, so there is no lexical gimme of the law title (the voice of BSARD's Droits-Quotidiens questions). Balanced **20/20/20 easy/medium/hard**, where the tier is the **citation document-spread**; capabilities: 32 lookup, 17 delegated-norm, 5 cross-reference, 3 temporal, 3 cc_validity; **one abstain target** (e18).

Construction is governed by a symmetry charter (source-authored gold blind to any arm's output; client voice uniformly; complete gold citations; equal pipeline and budget; hypotheses pre-registered) and two mechanical validators: **gold grounding** (every gold answer lexically supported, ≥50% content-word recall, by the verbatim statute text it cites) and a **flat-reachability diagnostic** run symmetrically as a label, never a filter — 24/59 (40.7%) of answerable items are fully flat-reachable; on the delegated-norm tier only **1/17**.

**Two items were corrected before this run, and this is a disclosed post-hoc revision.** e17 and e20 each named a year in the question text that disagreed with their own `as_of`, and for which the corpus holds no version slice at all — the graded task was "state the law as of `as_of`" while the question read "under the rules in force in 2014/2020". In both cases `as_of` already sat inside the correct slice, so the stated year was aligned to it and the gold citation pinned to that slice (`@421995`, `@435993`). Both are now as-of-date retrieval where the in-force text at `as_of` is a superseded slice.

**The revision is disclosed *and* bounded.** It is an explicit exception to the charter's blind-authoring clause, taken for item coherence, and made after analysing arm behaviour on those items. The resulting circularity is stated rather than left to be found: **e20 is the only item that discriminates between arms in the temporal capability row (§4.5), it was edited after arm behaviour was analysed, and it was edited in the direction that aligns the question's period with the `as_of` mechanism three of the five arms implement.** No temporal claim here rests on it — which is why F4′ is a mechanism demonstration and not a result. On every other axis the edit is inert: recomputing all paired tests with e17 and e20 removed leaves every p-value unchanged except naive-vs-advanced (0.0003 → 0.0005). The published p-values do not depend on the edited items.

**e18 carries the same year/`as_of` mismatch and was deliberately left alone** (it asks about 2017 with `as_of` 2024-03-01). Correcting it would change what the sole abstain target tests, and the mismatch is inert there: the item is graded purely on whether the arm abstained, `as_of` only gates the temporal-citation axis, and no arm cited anything on e18. It is flagged rather than fixed so the asymmetry stays visible.

### 3.2 Metrics

Graded **answer correctness** by a **cross-family LLM judge** — Claude Opus 5 judges Gemini-generated answers, defeating the same-family self-preference objection (Zheng et al., NeurIPS 2023 D&B; graded correctness per RAGAs, Es et al., EACL 2024; ARES, Saad-Falcon et al., NAACL 2024) — with a second-family juror (Gemini) grading every item and **Krippendorff's α** reported. **On dated items both graders receive the `as_of` date** and are instructed to treat a wrong-period answer as a factual contradiction; without this the grader compares substance against a dateless reference and cannot see period at all.

Deterministic metrics computed without any model: **citation precision/recall/F1** against gold provenance units (ALCE, Gao et al., EMNLP 2023; Liu et al., Findings of EMNLP 2023; AIS, Rashkin et al., *CL* 2023), **retrieval recall** and the **retrieval−citation gap** (GraphRAG-Bench, Xiao et al., arXiv:2506.02404; Xiang et al., arXiv:2506.05690), **misgrounding** and **hallucination** rates (Magesh et al., *JELS* 2025; Dahl et al., *J. Legal Analysis* 2024), **temporal correctness** and **`temporally_wrong_and_cited`** (TempLAMA, Dhingra et al., TACL 2022; SituatedQA, Zhang & Choi, EMNLP 2021), over-/under-abstention split, latency, and $/query. Significance: paired exact McNemar (Dietterich, *Neural Computation* 1998).

**Three scoring rules changed before this run**, each fixing a defect an internal metric review identified:

- **Citations are graded period-aware.** An article is stored one row per *(član × version-slice)* and all slices previously collapsed to one key, so citing the 2022 text of a 2018 question scored a perfect hit. Grading now uses subsumption: an **unpinned** gold citation matches any version (the right default — most articles were never amended), a **`@version`-pinned** one matches exactly one slice. 22 of 103 gold citations map to ≥2 stored rows (20 after the two pins), so without this the metric could not support a temporal claim at all.
- **Misgrounding is independent of the temporal axis.** It is now single-limbed (`citation_precision == 0`). Folding the temporal flag in made the two non-independent — a temporally-naive arm's *entire* misgrounding score was the temporal flag and nothing else.
- **Safety rates use a fixed denominator** (every answerable item) with abstentions shown separately, and **`misgrounded`/`temp-wrong-cited` are graded on citing abstentions**. A delivered-only base rewarded refusal: hallucination denominators previously ranged 51–59 of 60 across arms, so the rates were not comparable.

### 3.3 Run protocol

All 300 answers generated same-day in one contiguous run, single seed, temperature 0, on the stack (`gemini-3.6-flash` generator/juror via the official `google-genai` SDK; `claude-opus-5` judge over raw HTTP; `gemini-embedding-2`).

**Two cells were repaired and this is disclosed.** `advanced`/h11 and `context-stuffing`/h13 each hit the generator's 32,768-token output ceiling mid-JSON and were quarantined as batch errors. The retry layer declines a same-cap retry on the stated grounds that temperature-0 truncation reproduces identically; **both re-ran successfully on the first attempt**, falsifying that assumption. Each quarantined row was deleted and the single cell re-run under the same `batch_id`, so the batch is 300/300 with zero remaining errors. The truncations were harness failures, not arm behaviour — but the retry policy is discarding recoverable cells and should be fixed before the next campaign.

**Both repaired cells scored `correct`, and this is outcome-relevant.** Had they stayed quarantined and been scored as failures, advanced would read 0.717 (hard 12/20, tying graph) and the oracle 0.900 (hard 15/20 — *below* agentic's 16/20). Two readings in this report therefore rest on the repairs: §4.3's advanced-above-graph hard-tier note and §4.7's "matches the oracle on hard". The policy was **one attempt per quarantined cell**, applied to every quarantined cell and to no others; no cell was re-run because of its score. Judge agreement (§4.8) was computed on the 298-answer first pass and does not cover these two cells.

**This is a single-seed run**, so it carries no across-seed variance estimate; every accuracy figure is a point estimate on 60 items (§7).

---

## 4. Results

### 4.1 Headline (all 60 items, per arm)

| arm | accuracy | 95% CI | correctness | prov. F1 | cit. P | cit. R | retr. recall | gap | misground | halluc. | over-abstain | avg $/q | p50 s | p95 s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **Agentic** | **0.867** | [0.758, 0.931] | **0.933** | **0.611** | 0.543 | 0.761 | 0.795 | 0.034 | **0.07** | **0.07** | **0.00** | 0.068 | 25.8 | 82.3 |
| **Advanced+Graph** | 0.767 | [0.646, 0.856] | 0.867 | 0.540 | 0.468 | 0.715 | 0.768 | 0.054 | 0.10 | 0.10 | 0.02 | 0.037 | 16.2 | 22.4 |
| **Advanced** | 0.733 | [0.610, 0.829] | 0.842 | 0.558 | 0.496 | 0.709 | 0.743 | 0.034 | 0.12 | 0.10 | 0.05 | 0.030 | 14.9 | 22.1 |
| **Naive** | 0.483 | [0.362, 0.607] | 0.658 | 0.456 | 0.419 | 0.530 | 0.555 | 0.025 | 0.22 | 0.20 | 0.12 | 0.015 | 8.2 | 14.9 |
| Context-Stuffing (oracle) | 0.917 | [0.819, 0.964] | 0.950 | 0.652 | 0.563 | 0.874 | 0.966 | 0.092 | 0.07 | 0.08 | 0.00 | 0.197 | 11.1 | 25.4 |

Batch: 300 answers (2 repaired, §3.3), total **$20.91**. Per-arm totals: naive $0.92 · advanced $1.82 · graph $2.25 · agentic $4.08 · oracle $11.84. Citation/retrieval columns average over the 59 items where they are defined (the abstain target carries no gold citations); prov. F1 averages over all 60 — which hands every arm a free 1.0 from the abstain target (a correct abstention scores `fidelity = correctness`), i.e. +0.017 uniformly. It does not move the ranking, but the absolute prov. F1 figures read ~0.017 high. Safety rates use the fixed answerable denominator (59) for every arm. Intervals are Wilson 95% on the accuracy proportion — **item-sampling only, not across-seed** — and every engineered arm's interval overlaps every other's; only naive separates cleanly.

### 4.2 Significance (paired exact McNemar, wins/losses among discordant items)

| pair | discordant (row/col) | p |
| --- | --- | --- |
| naive vs advanced | 1/16 | **0.0003** |
| naive vs graph | 1/18 | **0.0001** |
| naive vs agentic | 1/24 | **0.0000** |
| advanced vs graph | 2/4 | 0.6875 |
| advanced vs agentic | 2/10 | **0.0386** |
| graph vs agentic | 3/9 | 0.1460 |
| agentic vs oracle | 3/6 | 0.5078 |

Every engineered arm beats naive decisively. Graph and advanced remain statistically indistinguishable. **Agentic's +0.133 over advanced reaches p = 0.0386** — but with six pairwise comparisons the Bonferroni threshold is α = 0.0083, which it does not clear, and against graph it does not approach significance. **Agentic and the oracle are indistinguishable** (p = 0.508): the deployable-vs-ceiling gap has closed to within noise at this n.

### 4.3 By difficulty tier (correct / n)

| tier | naive | advanced | graph | agentic | oracle |
| --- | --- | --- | --- | --- | --- |
| easy (20) | 16/20 | 19/20 | **20/20** | **20/20** | 20/20 |
| medium (20) | 6/20 | 12/20 | 14/20 | **16/20** | 19/20 |
| hard (20) | 7/20 | 13/20 | 12/20 | **16/20** | 16/20 |

The easy tier is saturated for the engineered arms; discrimination lives in medium/hard. **Agentic's lead is spread across both** (+4 medium over advanced, +3 hard) rather than concentrated in one, and on hard it now equals the oracle (16/20).

### 4.4 By capability (correct / n)

| capability | naive | advanced | graph | agentic | oracle |
| --- | --- | --- | --- | --- | --- |
| lookup (32) | 18/32 | 24/32 | 27/32 | **28/32** | 31/32 |
| delegated_norm (17) | 5/17 | 11/17 | 9/17 | **13/17** | 14/17 |
| cross_reference (5) | 2/5 | 4/5 | 4/5 | **5/5** | 5/5 |
| temporal (3) | 2/3 | 3/3 | 3/3 | 3/3 | 3/3 |
| cc_validity (3) | 2/3 | 2/3 | **3/3** | **3/3** | 2/3 |

Delegated-norm — the tier built to reward the law→bylaw hop — is led by **agentic (13/17)**, with flat advanced (11/17) above graph (9/17). That difference is **2 discordant items, p = 0.50** — not an effect, and inside the drift envelope. It is worth recording only for what it decomposes: graph's nominal overall lead over advanced comes from the **lookup** tier (0/3 discordant in graph's favour, p = 0.25), *not* from the structural tier the graph layer exists for. Neither cell is significant; together they are a further instance of F2 rather than an argument against it. The binding constraint remains the non-relevance-ranked fan-out (laws delegate to up to 79 materialized bylaws). temporal and cc_validity are n = 3 cells: **direction only**, and §4.5 explains why the temporal cell is weaker still.

### 4.5 Temporal correctness — a demonstration, not a measurement

The dated set holds three items. Their per-arm outcomes:

| item | naive | advanced | graph | agentic | oracle |
| --- | --- | --- | --- | --- | --- |
| e17 | ✓ (twac) | ✓ | ✓ | ✓ | ✓ (twac) |
| e18 (abstain target) | ✓ | ✓ | ✓ | ✓ | ✓ |
| e20 | ✗ | ✓ | ✓ | ✓ | ✓ (twac) |

**e17 and e18 are correct for all five arms.** The entire capability row is decided by **e20 alone** — the effective n for discriminating between arms is **1**, not 3.

`temporally_wrong_and_cited` splits cleanly — naive 1/2, oracle 2/2, and all three validity-filtering arms 0/2 — but **that split is arm configuration, not a measured effect**. Naive and the oracle both retrieve with `is_version_slice = false`, so where the in-force text at `as_of` *is* a superseded slice they cannot cite it; advanced/graph/agentic filter candidates to in-force-at-`as_of` before generation, so they cannot cite an out-of-force one. Neither outcome could have come out otherwise. This is the substance of **F4′**: a deterministic mechanism demonstration of the invariant-2 hazard, worth showing and not worth calling an effect.

**The zeros are not all the same zero.** The flag fires only when the arm cites the gold article, so an arm can clear it by citing the wrong law entirely: naive clears e20 and agentic clears e17, both at `citation_precision = citation_recall = 0`. Read with the retrieval-determined outcomes, this is a metric that measures an arm's filter and its citation luck rather than its temporal reasoning — which is the methodological finding, and the reason the column is not published as a rate. All three dated items are also `easy`-tier: the novelty axis has no representation in medium or hard.

A measured temporal result would need a dated tier of materially-amended articles: e17's pinned slice and its current text are 0.94 similar, which is why every arm passes it, and only e20 discriminates. Period-addressable citations exist; a tier able to exercise them does not.

### 4.6 The provenance decomposition (why F1 tops out at 0.61)

Citation recall tracks retrieval recall arm-for-arm — naive 0.530/0.555 (gap 0.025), advanced 0.709/0.743 (0.034), graph 0.715/0.768 (0.054), agentic 0.761/0.795 (0.034), oracle 0.874/0.966 (0.092). **When provenance fails, the gold unit was usually never in the pool** — the failure is at retrieval, not citation discipline. This is the stage attribution the metric set was built to expose (Xiao et al.; Xiang et al.), and it locates the remaining headroom in *recall*: the delegated-norm fan-out and the ~546 unchunkable bylaw stubs.

The oracle's 0.092 gap is the largest and is pure citation selectivity — with retrieval recall 0.966 it has the text and still under-cites.

### 4.7 The oracle ceiling, and why the gap has closed

With every gold-cited act in context, whole-act stuffing reaches **0.917**. Three facts keep it a *reference*, not a recommendation. **(1) Its input assumption is the problem statement:** it is handed every act the answer rests on, and knowing that act set a priori is precisely what retrieval exists to do. **(2) It pays for its window:** $0.197/query — 12.8× naive, 6.5× advanced, 2.9× agentic. **(3) It is temporally naive by construction** (§4.5).

The oracle's margin over agentic is 3 items (0.917 vs 0.867), and the paired test on 9 discordant items returns p = 0.508 — this campaign cannot distinguish them, which is a statement about power, not about parity. Agentic's hard-tier 16/20 equals the oracle's, but the oracle's 16th hard item is a repaired cell (§3.3); scored as a failure, agentic leads on hard. The honest reading is that the deployable-vs-ceiling gap is now small enough that n = 60 at one seed cannot measure it — a reason to run the ≥3-seed tier, not a result.

### 4.8 Judge agreement — below threshold, and diagnosed

Cross-family α = **0.764** (Opus 5 judge vs Gemini 3.6 Flash juror, **n = 293 of the 295 gradable pairs** — the two repaired cells of §3.3 were re-graded by the judge only and carry no juror label). **This is below the conventional 0.80 reliability threshold.** A second juror pass over the stored answers, keeping the per-item join the campaign discarded, replicates it (nominal α = **0.754** on all 295) and makes it diagnosable.

**Where the disagreement is.** The two graders differ on **26 of 295 gradings (8.8%)**, and the disagreement is neither random nor uniform:

- **81% of it sits on one boundary.** 21 of 26 disagreements are `correct` vs `partially_correct`; only 5 involve `incorrect` at all. Read on the ordered scale the verdicts actually form, **ordinal α = 0.842** — above threshold. Nominal α penalises a correct-vs-partial disagreement exactly as heavily as correct-vs-incorrect, which is the wrong distance function for this scale and is most of why the headline coefficient reads low.
- **It is directional, not noisy.** In **20 of 26** cases the juror is the more lenient of the two: Opus says `partially_correct`, Gemini says `correct`. That is a calibration offset between graders, not disorder.
- **It scales inversely with answer quality** — naive 15.3%, advanced and graph 10.2%, agentic 5.1%, oracle 3.4% — and with difficulty: easy 2.1%, medium 10.0%, hard 14.0%. Weaker, partially-right answers are simply harder to grade.
- **The date clause is exonerated.** Disagreement on dated items is **0 of 10**. The plausible-sounding hypothesis that the date-aware grading clause depressed agreement is refuted.

**What it costs the findings, tested rather than argued.** On the decision the ranking actually rests on — `correct` at threshold 1.0 — binarised α is **0.781**, still marginally below 0.80. So the caveat is not dissolved. But re-deriving every result from the *juror's* labels leaves the conclusions intact:

| | judged by Opus | judged by the Gemini juror |
|---|---|---|
| ranking | oracle > agentic > graph > advanced > naive | **identical** |
| advanced vs naive | 16/1, p = 0.0003 | 15/1, p = 0.0005 |
| graph vs naive | 18/1, p = 0.0001 | 16/1, p = 0.0003 |
| agentic vs naive | 24/1, p = 0.0000 | 21/1, p = 0.0000 |

The juror grades everything more generously (naive 0.475 → 0.576, agentic 0.864 → 0.915), and the ordering and every significance verdict survive unchanged. **F1 is robust to judge choice; F3's 2/10 margin is not tested by this and remains the weaker claim.** Treat accuracy figures as carrying real judge variance on the partial-credit boundary, and note that no judge–human validation has been performed (§7).

---

## 5. Caveats (carried on every quoted number)

1. **Single seed, temperature 0** — no across-seed variance estimate. The Wilson intervals in §4.1 capture item-sampling variance only and are wide; indicative until the ≥3-seed tier.
2. **Unpinned generator** — `gemini-3.6-flash` is a floating alias with measured drift (~6% correctness-flip rate on byte-identical prompts, quantified on its predecessor); this campaign is internally same-day-consistent, but cross-time comparisons carry the caveat.
3. **Judge agreement is below threshold** (α = 0.764; ordinal 0.842, binarised 0.781 — §4.8). Diagnosed rather than merely disclosed: the disagreement concentrates on the correct-vs-partial boundary and the ranking survives re-derivation from the juror's labels, but the accuracy-deciding call still sits marginally under 0.80.
4. **Agentic's lead does not survive multiple-comparison correction** (p = 0.0386 vs Bonferroni α = 0.0083 across six tests).
5. **Small capability cells** — temporal and cc_validity are n = 3, and the temporal cell has an effective n of **1** (§4.5); the abstain axis is n = 1.
6. **The abstain target is manufactured by a system limitation.** e18's 2017 rule *is* in the corpus (`clan-80б@419333`, valid 2015-01-02 → 2018-09-30) but was unaddressable when the item was authored, so "unanswerable" describes the benchmark, not the law. F7 is n = 1 on an artifact.
7. **Two cells were repaired after generator truncation** (§3.3) — disclosed, same batch, harness-side, one attempt each. **Both repaired cells scored correct**, which is what puts advanced's hard tier above graph's and the oracle's hard tier level with agentic's; both readings invert if the repairs are scored as failures.
8. **Source-authored gold**, mechanically grounding-checked but not expert-validated; e17/e20 carry a second disclosed post-hoc revision (§3.1).
9. **LLM-as-judge** — mitigated by the cross-family design, not eliminated; no judge–human validation has been performed.

---

## 6. What the results mean

**What this campaign costs, stated first.** It is a weaker evidential position than a benchmark of this kind should hold on three axes, and a stronger one on two. **Weaker:** judge agreement is below the reliability threshold on the accuracy-deciding call, though now diagnosed and shown not to disturb the ranking; the temporal finding is a mechanism demonstration with an effective n of 1; and the one item that discriminates in that cell was edited post-hoc. **Stronger:** the provenance metrics can see period at all, and the safety metrics are comparable across arms. Nothing in the release turns on the temporal claim.

1. **The ladder pays, and the receipts are significant** (F1) — every engineered arm beats the floor at p ≤ 0.0003, with a naive Wilson interval that does not overlap any engineered arm's.
2. **The negative result is well-instrumented and correctly scoped** (F2) — structure-off provably reproduces advanced, the ablation attributes the non-effect to the hop, and the cause is a measured corpus property (cross-reference sparsity plus a non-relevance-ranked delegation fan-out). The claim is about *this* graph layer on *this* corpus, not graph structure in general. A negative result this well-instrumented is a contribution, not an absence (Han et al., arXiv:2502.11371; Xiang et al., arXiv:2506.05690).
3. **The agentic arm is the headline, on its quality profile rather than its p-value** (F3) — best deployable correctness, provenance and safety, zero over-abstentions, at ~2× advanced's cost. The accuracy lead fails multiple-comparison correction and should be presented as promising, not established.
4. **Temporal validity is a demonstrated mechanism, not a measured effect** (F4′) — and the trap it documents (a per-citation validity metric whose value is decided by the arm's retrieval filter, not by the answer) awaits anyone else building a temporal legal benchmark. That methodological finding is the more durable contribution.

---

## 7. What this evaluation does not establish

The campaign is a single-seed measurement on 60 items graded by an LLM judge. These are the things it leaves open, stated so that no number above is read for more than it carries.

- **Judge reliability is bounded, not resolved.** Cross-family agreement sits below the conventional threshold on the accuracy-deciding call (binarised α = 0.781), even though the ranking is unchanged under either grader (§4.8). Agreement with a qualified Serbian legal reader is **unmeasured** — no judge–human validation has been performed, and no amount of judge–judge agreement substitutes for it.
- **There is no across-seed variance estimate.** The intervals in §4.1 capture item-sampling only. Whether agentic's accuracy lead is stable across seeds is untested, and that lead already fails multiple-comparison correction.
- **The temporal result is a mechanism demonstration, not a measured effect** (§4.5). The dated tier holds three items, one of which discriminates between arms, and that one was edited post-hoc; two of the three articles are textually near-identical across the version boundary, so they cannot separate an arm that retrieved the wrong period.
- **The gold set is source-authored and mechanically grounding-checked, not expert-validated.** No qualified practitioner has reviewed the answers or the citation lists, and h03 shows citation precision 1.0 with recall 0.5 on every arm — the arms cite a strict subset of its gold, which points at a citation list that may be over-specified.
- **The generator is an unpinned floating alias** with measured run-to-run variance of roughly 6% of items, so differences of ≤ 4 items at n = 60 sit inside the drift envelope.
- **No external system was run on this corpus.** Every comparison is between arms built here, so the results say what each architecture buys relative to the others, not how any of them compares to a published system.
- **Five of the eight designed question categories are populated.** Amendment-diff and yes/no entailment carry no items, and the abstain axis carries one — itself an artifact of an addressing limitation rather than a property of law (§5.6).

## 8. Acknowledgements

[Saša Mirković](https://github.com/SasaMirkovic) co-designed the ingestion pipeline and the
knowledge base that every arm in this evaluation retrieves over, and advised on the retrieval
architecture and on the design of this evaluation. Responsibility for the results reported here,
and for any errors in them, is the author's.

## 9. References

Es, S., et al. (2024). RAGAs. *EACL 2024*. · Saad-Falcon, J., et al. (2024). ARES. *NAACL 2024*. · Zheng, L., et al. (2023). Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena. *NeurIPS 2023 D&B*. · Krippendorff, K. *Content Analysis: An Introduction to Its Methodology*. Sage. · Dietterich, T. (1998). Approximate Statistical Tests for Comparing Supervised Classification Learning Algorithms. *Neural Computation 10(7)*. · Efron, B., & Tibshirani, R. (1993). *An Introduction to the Bootstrap*. · Gebru, T., et al. (2021). Datasheets for Datasets. *CACM 64(12)*. · Gao, T., et al. (2023). Enabling LLMs to Generate Text with Citations (ALCE). *EMNLP 2023*. · Liu, N. F., et al. (2023). Evaluating Verifiability in Generative Search Engines. *Findings of EMNLP 2023*. · Rashkin, H., et al. (2023). Measuring Attribution in NLG (AIS). *Computational Linguistics 49(4)*. · Yang, Z., et al. (2018). HotpotQA. *EMNLP 2018*. · Petroni, F., et al. (2021). KILT. *NAACL 2021*. · Dhingra, B., et al. (2022). TempLAMA. *TACL 10*. · Zhang, M., & Choi, E. (2021). SituatedQA. *EMNLP 2021*. · Lewis, P., et al. (2020). Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks. *NeurIPS 2020*. · Karpukhin, V., et al. (2020). Dense Passage Retrieval. *EMNLP 2020*. · Robertson, S., & Zaragoza, H. (2009). The Probabilistic Relevance Framework: BM25 and Beyond. *FnTIR 3(4)*. · Cormack, G. V., et al. (2009). Reciprocal Rank Fusion. *SIGIR 2009*. · Sun, W., et al. (2023). Is ChatGPT Good at Search? *EMNLP 2023*. · Carbonell, J., & Goldstein, J. (1998). The Use of MMR. *SIGIR 1998*. · Yao, S., et al. (2023). ReAct. *ICLR 2023*. · Asai, A., et al. (2024). Self-RAG. *ICLR 2024*. · Edge, D., et al. (2024). From Local to Global: A Graph RAG Approach. arXiv:2404.16130. · Xiao, Y., et al. (2025). GraphRAG-Bench. arXiv:2506.02404. · Xiang, Z., et al. (2025). When to use Graphs in RAG. arXiv:2506.05690. · Han, H., et al. (2025). RAG vs. GraphRAG. arXiv:2502.11371. · Louis, A., & Spanakis, G. (2022). BSARD. *ACL 2022*. · Yang, X., et al. (2024). CRAG. *NeurIPS 2024 D&B*. · Magesh, V., et al. (2025). Hallucination-Free? *J. Empirical Legal Studies*. · Dahl, M., et al. (2024). Large Legal Fictions. *J. Legal Analysis 16(1)*. · Filtz, E., et al. (2021). The Linked Legal Data Landscape. *AI & Law 29(4)*. · NitiBench (2025). arXiv:2502.10868, *EMNLP 2025*. · Fan, et al. (2026). Can LLMs Time Travel? arXiv:2605.25920. · de Martim, H. (2025). SAT-Graph RAG. arXiv:2505.00039, *JURIX 2025*. · LexRAG (2025). arXiv:2502.20640, *SIGIR 2025*. · Athan, T., et al. (2013). OASIS LegalRuleML. *ICAIL 2013*. · OASIS (2018). Akoma Ntoso 1.0. · Council of the EU (2012, 2017). ELI Council Conclusions. *OJ C 325; C 441*. · Snodgrass, R. T. (1987). TQuel. *ACM TODS 12(2)*. · Jensen, C. S., & Snodgrass, R. T. (1999). Temporal Data Management. *IEEE TKDE 11(1)*. · Fadillah, A., et al. (2026). LawRAG. *Data Technologies and Applications 60(2)*. · Chen, T., et al. (2024). Dense X Retrieval. *EMNLP 2024*.
