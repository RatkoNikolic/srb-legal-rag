# Goldens — benchmark QA ground truth

> **Licence: [CC BY 4.0](./LICENSE)** — these question sets are data, licensed
> separately from the repository's Apache-2.0 code. Reuse with attribution.

A **golden set** is one `*.jsonl` file in this directory; its name is the file
stem. The benchmark scopes everything to a chosen set — the question editor, the
run target, and the metrics filter — so several sets can coexist and be compared.

- **`golden-situational-60.jsonl`** — the **live default set**:
  60 concrete, first-person client questions about a real problem ("Купујем стан и
  уговор иде код бележника…"), not exam prompts. The tier **is** the citation
  doc-spread, by an explicit definition: **easy** = answerable from one or two
  chunks of a *single* document · **medium** = several chunks across *≥2*
  documents · **hard** = leans on a *peripheral* doc (bylaw / CC decision) **and**
  spans ≥2 documents — abstain items (no gold answer, no citations) are exempt
  from the spread rule. Balanced **20/20/20**; the hard tier is spread across ~10
  clusters (not notary-concentrated). Built over grounded anchors so every gold
  answer is corpus-grounded (`bench.validate_item`). This is what
  `CURRENT_GOLDEN` in `lexsr.config` points at. At 60 items the set is small and
  no legal professional has reviewed it: gold answers are source-authored and
  mechanically grounding-checked only, which catches fabrication and citation
  drift but not a wrong reading of a correctly-cited article.
  Ten items have been corrected since the set was first built, each after a
  per-item, all-arm failure analysis exposed a defect in the gold rather than in
  an arm: an inverted Constitutional-Court holding, a missed first-order-heir
  exemption, a question/gold parse mismatch, three items whose phrasing pinned
  the wrong moment of conclusion, a missing statutory exception, the conversion
  of one item into the set's only **abstain target**, and two dated items whose
  stated year disagreed with their own `as_of`. Because the gold changed,
  per-item scores are only comparable between runs judged against the same
  revision of this file.
- **`golden-situational-30.jsonl`** — the **dev subset**: a balanced **10/10/10**
  strict subset of the 60, for cheap iteration. Both sets are locked by
  `test_situational_goldens_contract`.


Format: one `kl_core.evals.TaskItem` per line. `gold_citations` use the
`citation_key()` format `"{source_id}::{fragment}"`; `query.filters.law_slug`
carries the oracle target law for the context-stuffing arm. Abstain items have
`gold_answer: null` and no oracle law.

The set is selected in the Benchmark tab (a set picker drives Questions / Run /
Results), or on the CLI:

    .venv/bin/python -m lexsr.evals --arms naive,advanced,graph,context-stuffing,agentic --golden goldens/golden-situational-30.jsonl

(omit `--golden` to run the live default set, `golden-situational-60`).
