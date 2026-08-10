// Typed client for the lexsr API. One fetch helper, one type per payload.

export interface Citation {
  source_id: string;
  source_title: string;
  fragment: string | null;
  version: string | null;
  valid_from: string | null;
  valid_to: string | null;
  url: string | null;
  parent_source_id: string | null; // enabling law of a delegated bylaw
  parent_title: string | null;
}

export interface RetrievedChunk {
  chunk_id: string;
  score: number;
  stage: string;
  source_title: string;
  fragment: string;
  text: string;
  valid_from: string; // entry into force (stupanje na snagu)
  valid_to: string;
  applies_from: string; // start of application (početak primene), if distinct
  published: string; // base-gazette publication date (objavljivanje)
  status: string; // in_force | repealed
  structural_path: string; // heading breadcrumb (Глава › пододељак)
  article_heading: string; // naziv člana
  repealed: boolean;
  url: string;
  version: string;
  delegates_to_bylaw?: boolean; // article delegates detail to a bylaw (gates the law→bylaw hop)
}

export interface Turn {
  role: "user" | "assistant";
  content: string;
}

export interface ToolStep {
  step: number;
  tool: string;
  thought?: string;
  args?: Record<string, string>;
  result_chars?: number;
  chunks_added?: number;
  // LLM-resilience observability: a "__decision_retry__" entry logs one failed
  // decision attempt; `forced` marks the research-guardrail search.
  error?: string;
  finish_reason?: string | null;
  raw_preview?: string;
  forced?: boolean;
  seed?: boolean;
}

// Per-stage LLM attempt/failure record riding run extras (`llm_io`).
export interface LlmIoStage {
  attempts?: number;
  finish_reason?: string | null;
  failures?: { attempt: number; error: string }[];
  giveups?: number;
}

export interface EvalScore {
  correct: boolean;
  correctness: number;
  provenance_fidelity: number;
  citation_precision: number | null;
  citation_recall: number | null;
  temporally_correct: boolean | null;
  judge_rationale: string | null;
  category?: string;
  // retriever-side reach + provenance-safety, per run (may be absent on
  // runs scored before the metric expansion).
  retrieval_recall?: number | null;
  retrieval_precision?: number | null;
  abstain_target?: boolean;
  temporally_wrong_and_cited?: boolean | null;
  misgrounded?: boolean | null;
  hallucinated?: boolean | null;
}

export interface Run {
  id: string;
  ts: string;
  question: string;
  arm: string;
  as_of: string | null;
  answer_text: string;
  abstained: boolean;
  abstain_reason: string | null;
  citations: Citation[];
  retrieved: RetrievedChunk[];
  timings: Record<string, number>;
  prompt_tokens: number;
  output_tokens: number;
  cost_usd: number;
  model: string;
  conversation_id: string | null;
  batch_id: string;
  eval: EvalScore | null;
  extras: Record<string, unknown>;
}

export interface Batch {
  batch_id: string;
  runs: number;
  first_ts: string;
  last_ts: string;
  arms: string[];
  scored: number;
  golden: string | null; // the golden set this batch ran against (null for interactive)
  goldens: string[];
  ablation: string | null; // the capability-toggle config this batch ran under (null = mixed/none)
  ablations: string[];
}

// One ablation toggle, as the backend describes it (/api/ablations) — the UI
// renders controls generically from this so it can't drift from AblationConfig.
export interface AblationToggle {
  key: string;
  label: string;
  group: string; // "cross-cutting" | "advanced" | "graph"
  arms: string[]; // which arms the toggle affects
  description: string;
}

export interface AblationSpec {
  spec: AblationToggle[];
  defaults: Record<string, boolean>;
}

// A run's capability toggles; partial overrides of the production defaults.
export type Ablation = Record<string, boolean>;

export interface GoldenSet {
  name: string;
  items: number;
  is_default: boolean;
}

export interface BatchArmStats {
  runs: number;
  scored: number;
  accuracy: number | null;
  correctness: number | null;
  citation_precision: number | null;
  citation_recall: number | null;
  provenance_f1: number | null;
  temporal_accuracy: number | null;
  abstain_rate: number;
  // retriever-side reach + provenance-safety + abstention quality
  retrieval_recall: number | null;
  retrieval_citation_gap: number | null;
  misgrounded_rate: number | null;
  temporally_wrong_and_cited_rate: number | null;
  hallucination_rate: number | null;
  over_abstention_rate: number | null;
  false_answer_rate_unanswerable: number | null;
  avg_cost_usd: number | null;
  avg_latency_ms: number;
}

export interface BatchStats {
  batch_id: string;
  per_arm: Record<string, BatchArmStats>;
}

export interface GoldenItem {
  item_id: string;
  query: { text: string; filters?: { law_slug?: string }; as_of?: string | null };
  category: string; // easy | medium | hard
  gold_answer: string | null;
  gold_citations: string[];
}

export interface Job {
  job_id: string;
  batch_id: string;
  arms: string[];
  categories: string[];
  ablation?: string; // the capability-toggle config the batch ran under
  status: string; // running | done | error
  done: number;
  total: number;
  last: string | null;
  started: string;
  error: string | null;
}

export interface BatchItemArm {
  correct: boolean | null;
  correctness: number | null;
  provenance_f1: number | null;
  citation_precision: number | null;
  citation_recall: number | null;
  abstained: boolean;
  cost_usd: number | null;
  latency_ms: number | null;
}

export interface BatchItem {
  item_id: string;
  question: string;
  tier: string;
  arms: Record<string, BatchItemArm>;
}

export interface RunSummary {
  id: string;
  ts: string;
  question: string;
  arm: string;
  as_of: string | null;
  abstained: boolean;
  cost_usd: number;
  total_ms: number;
  answer_preview: string;
  batch_id: string;
  correct: boolean | null;
}

export interface ArmStats {
  runs: number;
  abstained: number;
  abstain_rate: number;
  cost_usd: number;
  avg_cost_usd: number;
  avg_total_ms: number;
  p50_total_ms: number;
  p95_total_ms: number;
  prompt_tokens: number;
  output_tokens: number;
}

export interface Stats {
  total_runs: number;
  total_cost_usd: number;
  per_arm: Record<string, ArmStats>;
}

export interface CorpusGraphNode {
  slug: string;
  title: string;
  cluster: string;
  clanova: number;
  repealed: number;
  amendments: number;
  internal_refs: number;
}

export interface CorpusGraph {
  nodes: CorpusGraphNode[];
  edges: { source: string; target: string; weight: number }[];
}

export interface ArticleGraphNode {
  chunk_id: string;
  law_slug: string;
  cluster: string;
  number: string;
  fragment: string;
  repealed: boolean;
}

export interface ArticleGraph {
  slugs: string[];
  nodes: ArticleGraphNode[];
  edges: { source: string; target: string }[];
}

export interface GraphStats {
  nodes: number;
  // the UPUĆUJE_NA (numbered cross-reference) layer — intrinsically sparse
  semantic_edges: number;
  mean_out_degree: number;
  isolated_nodes: number;
  isolated_pct: number;
  cross_law_edges: number;
  cross_law_pct: number;
  max_out_degree: number;
  // the layer the graph arm actually traverses (UPUĆUJE_NA ∪ DEFINED_IN)
  expansion_edges?: number;
  expansion_mean_out_degree?: number;
  expansion_isolated_pct?: number;
  arm_reachable_pct?: number;
}

export interface CorpusSummary {
  laws: number;
  materialized_bylaws: number;
  cc_decisions: number;
  referenced_materialized: number;
  stubs: number;
  acts_total: number;
}

export interface CorpusLaw {
  slug: string;
  title: string;
  registry_title?: string;
  cluster?: string;
  role?: string;
  law_guid: string;
  regactid: string;
  law_published: string; // publication of the base gazette (objavljivanje)
  law_valid_from: string; // entry into force (stupanje na snagu)
  applies_from: string; // start of application (početak primene)
  eli: string | null;
  gazette_chain: string[];
  main_clanovi: number;
  repealed_clanovi: number;
  chunks: number;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init);
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

export const api = {
  arms: () => request<string[]>("/api/arms"),
  ablations: () => request<AblationSpec>("/api/ablations"),
  ask: (
    question: string,
    arm: string,
    asOf: string | null,
    history: Turn[] = [],
    conversationId: string | null = null,
    ablation: Ablation | null = null,
  ) =>
    request<Run>("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question,
        arm,
        as_of: asOf || null,
        history,
        conversation_id: conversationId,
        // only send when something is actually toggled — keeps default runs clean
        ablation: ablation && Object.keys(ablation).length ? ablation : null,
      }),
    }),
  runs: (limit = 200, batchId?: string) =>
    request<RunSummary[]>(
      `/api/runs?limit=${limit}${batchId ? `&batch_id=${encodeURIComponent(batchId)}` : ""}`,
    ),
  run: (id: string) => request<Run>(`/api/runs/${id}`),
  stats: (batchId?: string) =>
    request<Stats>(`/api/stats${batchId ? `?batch_id=${encodeURIComponent(batchId)}` : ""}`),
  batches: () => request<Batch[]>("/api/batches"),
  batchStats: (id: string) =>
    request<BatchStats>(`/api/batches/${encodeURIComponent(id)}/stats`),
  batchItems: (id: string) =>
    request<BatchItem[]>(`/api/batches/${encodeURIComponent(id)}/items`),
  corpus: () => request<CorpusLaw[]>("/api/corpus"),
  corpusSummary: () => request<CorpusSummary>("/api/corpus/summary"),
  corpusStats: () => request<GraphStats>("/api/corpus/stats"),
  corpusGraph: () => request<CorpusGraph>("/api/corpus/graph"),
  corpusArticleGraph: (slugs?: string[]) =>
    request<ArticleGraph>(
      `/api/corpus/article-graph${slugs && slugs.length ? `?slugs=${slugs.join(",")}` : ""}`,
    ),

  goldenSets: () => request<GoldenSet[]>("/api/golden/sets"),
  golden: (set?: string) =>
    request<GoldenItem[]>(`/api/golden${set ? `?golden=${encodeURIComponent(set)}` : ""}`),
  goldenUpsert: (item: GoldenItem, set?: string) =>
    request<GoldenItem>(`/api/golden${set ? `?golden=${encodeURIComponent(set)}` : ""}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(item),
    }),
  goldenDelete: (id: string, set?: string) =>
    request<{ deleted: string }>(
      `/api/golden/${encodeURIComponent(id)}${set ? `?golden=${encodeURIComponent(set)}` : ""}`,
      { method: "DELETE" },
    ),
  benchmarkRun: (cfg: {
    arms: string[];
    categories?: string[] | null;
    limit?: number | null;
    batch_id?: string | null;
    golden?: string | null;
    ablation?: Ablation | null;
  }) =>
    request<Job>("/api/benchmark/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cfg),
    }),
  benchmarkJobs: () => request<Job[]>("/api/benchmark/jobs"),
  benchmarkJob: (id: string) => request<Job>(`/api/benchmark/jobs/${id}`),
};
