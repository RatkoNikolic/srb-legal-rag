import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type EvalScore, type LlmIoStage, type ToolStep } from "../api";
import { armLabel } from "../arms";

const STAGE_COLORS: Record<string, string> = {
  embed_query_ms: "bg-sky-400",
  vector_ms: "bg-indigo-400",
  fts_ms: "bg-violet-400",
  rerank_ms: "bg-fuchsia-400",
  graph_ms: "bg-emerald-400",
  agent_ms: "bg-teal-400",
  condense_ms: "bg-cyan-400",
  llm_ms: "bg-amber-400",
};

export default function Runs() {
  const [selected, setSelected] = useState<string | null>(null);
  const [batch, setBatch] = useState<string>(""); // "" = all batches
  const { data: batches = [] } = useQuery({
    queryKey: ["batches"],
    queryFn: api.batches,
    refetchInterval: 10000,
  });
  const { data: runs = [], isLoading } = useQuery({
    queryKey: ["runs", batch],
    queryFn: () => api.runs(500, batch || undefined),
    refetchInterval: 5000,
  });

  return (
    <div className="flex h-full min-h-0 flex-col gap-3">
      <div className="flex items-center gap-2">
        <label htmlFor="batch-filter" className="text-xs font-medium uppercase tracking-wide text-slate-400">
          Batch
        </label>
        <select
          id="batch-filter"
          value={batch}
          onChange={(e) => setBatch(e.target.value)}
          className="rounded-lg border border-slate-300 bg-white px-2 py-1.5 text-sm"
        >
          {/* runs.length is the *filtered* list — showing it here would label
              "All batches" with the currently selected batch's count */}
          <option value="">All batches</option>
          {batches.map((b) => (
            <option key={b.batch_id} value={b.batch_id}>
              {b.batch_id} — {b.runs} runs
            </option>
          ))}
        </select>
        <span className="text-xs text-slate-400">{runs.length} runs shown</span>
      </div>

      <div className="grid min-h-0 flex-1 grid-cols-[1fr_1.2fr] gap-4">
        <div className="min-h-0 overflow-y-auto rounded-lg border border-slate-200 bg-white">
          <table className="w-full text-left text-sm">
            <thead className="sticky top-0 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th className="px-3 py-2">When</th>
                <th className="px-3 py-2">Arm</th>
                <th className="px-3 py-2">Question</th>
                <th className="px-3 py-2 text-center">✓</th>
                <th className="px-3 py-2 text-right">ms</th>
                <th className="px-3 py-2 text-right">$</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr
                  key={r.id}
                  onClick={() => setSelected(r.id)}
                  className={`cursor-pointer border-t border-slate-100 hover:bg-slate-50 ${
                    selected === r.id ? "bg-slate-100" : ""
                  }`}
                >
                  <td className="whitespace-nowrap px-3 py-2 text-xs text-slate-500">
                    {r.ts.slice(5, 16).replace("T", " ")}
                  </td>
                  <td className="px-3 py-2">
                    <span className="rounded bg-slate-100 px-1.5 py-0.5 text-xs">{armLabel(r.arm)}</span>
                    {r.abstained && (
                      <span className="ml-1 rounded bg-amber-100 px-1.5 py-0.5 text-xs text-amber-800">
                        abst
                      </span>
                    )}
                  </td>
                  <td className="max-w-[220px] truncate px-3 py-2">{r.question}</td>
                  <td className="px-3 py-2 text-center">
                    {r.correct === null ? (
                      <span className="text-slate-300">·</span>
                    ) : r.correct ? (
                      <span className="text-emerald-600">✓</span>
                    ) : (
                      <span className="text-rose-500">✗</span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">{Math.round(r.total_ms)}</td>
                  <td className="px-3 py-2 text-right tabular-nums">{r.cost_usd.toFixed(4)}</td>
                </tr>
              ))}
              {!isLoading && runs.length === 0 && (
                <tr>
                  <td colSpan={6} className="px-3 py-8 text-center text-slate-400">
                    No runs yet — ask something in the Chat tab, or run the benchmark.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        <div className="min-h-0 overflow-y-auto">
          {selected ? (
            <RunDetail id={selected} />
          ) : (
            <div className="flex h-full items-center justify-center rounded-lg border border-dashed border-slate-300 text-sm text-slate-400">
              Select a run to inspect it
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function RunDetail({ id }: { id: string }) {
  const { data: run } = useQuery({ queryKey: ["run", id], queryFn: () => api.run(id) });
  if (!run) return null;

  const stages = Object.entries(run.timings).filter(([k, v]) => k !== "total_ms" && v > 0);
  const total = run.timings.total_ms || 1;

  return (
    <div className="space-y-4 rounded-lg border border-slate-200 bg-white p-4 text-sm">
      <div>
        <div className="mb-1 flex flex-wrap items-center gap-2 text-xs text-slate-500">
          <span className="rounded bg-slate-100 px-1.5 py-0.5">{armLabel(run.arm)}</span>
          {run.as_of && <span>as of {run.as_of}</span>}
          <span className="rounded bg-slate-50 px-1.5 py-0.5 text-slate-400">{run.batch_id}</span>
          <span>{run.model}</span>
          <span className="ml-auto">{run.ts}</span>
        </div>
        <p className="font-medium">{run.question}</p>
      </div>

      {run.eval && <EvalPanel ev={run.eval} />}

      <div>
        <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-slate-500">
          Stage timings — {Math.round(total)} ms total
        </h3>
        <div className="flex h-4 w-full overflow-hidden rounded bg-slate-100">
          {stages.map(([k, v]) => (
            <div
              key={k}
              className={STAGE_COLORS[k] ?? "bg-slate-400"}
              style={{ width: `${(v / total) * 100}%` }}
              title={`${k}: ${Math.round(v)} ms`}
            />
          ))}
        </div>
        <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-slate-500">
          {stages.map(([k, v]) => (
            <span key={k} className="flex items-center gap-1">
              <span className={`h-2 w-2 rounded-sm ${STAGE_COLORS[k] ?? "bg-slate-400"}`} />
              {k.replace("_ms", "")} {Math.round(v)}
            </span>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-3 gap-2 text-center">
        <Metric label="prompt tokens" value={run.prompt_tokens.toLocaleString()} />
        <Metric label="output tokens" value={run.output_tokens.toLocaleString()} />
        <Metric label="cost" value={`$${run.cost_usd.toFixed(5)}`} />
      </div>

      {typeof run.extras?.condensed_query === "string" && (
        <div className="rounded-md bg-sky-50 px-3 py-2 text-xs text-sky-800">
          <span className="font-semibold">Condensed query:</span>{" "}
          {run.extras.condensed_query as string}
        </div>
      )}

      <LlmRetries llmIo={run.extras?.llm_io as Record<string, LlmIoStage> | undefined} />

      {Array.isArray(run.extras?.tool_trace) && (
        <div>
          <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-slate-500">
            Agent trace ({(run.extras.tool_trace as ToolStep[]).length} steps)
          </h3>
          <ol className="space-y-1.5">
            {(run.extras.tool_trace as ToolStep[]).map((t, i) => (
              // key by index: retry/forced entries share a step number with the
              // step they annotate
              <li key={i} className="rounded-md border border-slate-150 bg-slate-50 px-2.5 py-1.5 text-xs">
                <div className="flex items-center gap-1.5">
                  <span className="rounded bg-slate-200 px-1.5 text-[10px] tabular-nums">{t.step}</span>
                  <span className="font-medium">{t.tool}</span>
                  {t.forced && (
                    <span className="rounded bg-amber-100 px-1.5 text-[10px] font-medium text-amber-700">
                      forced
                    </span>
                  )}
                  {t.seed && (
                    <span className="rounded bg-sky-100 px-1.5 text-[10px] font-medium text-sky-700">
                      seed
                    </span>
                  )}
                  {t.args && Object.keys(t.args).length > 0 && (
                    <span className="text-slate-500">
                      {Object.entries(t.args)
                        .map(([k, v]) => `${k}=${v}`)
                        .join(", ")}
                    </span>
                  )}
                  {typeof t.chunks_added === "number" && t.chunks_added > 0 && (
                    <span className="ml-auto text-emerald-600">+{t.chunks_added}</span>
                  )}
                </div>
                {t.error && (
                  <p className="mt-0.5 text-rose-600">
                    {t.error}
                    {t.finish_reason ? ` (finish_reason=${t.finish_reason})` : ""}
                  </p>
                )}
                {t.thought && <p className="mt-0.5 text-slate-500">{t.thought}</p>}
              </li>
            ))}
          </ol>
        </div>
      )}

      <div>
        <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-slate-500">
          Answer {run.abstained && <span className="text-amber-600">(abstained)</span>}
        </h3>
        <p className="whitespace-pre-wrap rounded-md bg-slate-50 p-3">{run.answer_text}</p>
      </div>

      <div>
        <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-slate-500">
          Retrieved chunks ({run.retrieved.length}) — hover to expand
        </h3>
        <ul className="space-y-1.5">
          {run.retrieved.map((r) => {
            const cited = run.citations.some(
              (c) => c.fragment === r.fragment && c.source_title === r.source_title,
            );
            return (
              <li
                key={r.chunk_id}
                className={`group rounded-md border px-2.5 py-1.5 ${
                  cited ? "border-emerald-300 bg-emerald-50" : "border-slate-150 bg-slate-50"
                }`}
              >
                <div className="flex items-center gap-1.5 text-xs">
                  <span className="font-medium">{r.fragment}</span>
                  <span className="text-slate-500">{r.source_title}</span>
                  <span className="rounded bg-slate-200 px-1 text-[10px]">{r.stage}</span>
                  <span className="ml-auto tabular-nums text-slate-400">{r.score.toFixed(3)}</span>
                  {cited && <span className="text-emerald-600">cited</span>}
                </div>
                {r.structural_path && (
                  <div className="mt-0.5 text-[10px] text-slate-400">{r.structural_path}</div>
                )}
                {r.repealed || r.valid_to ? (
                  <div className="mt-0.5 text-[11px] text-red-600">престао да важи {r.valid_to}</div>
                ) : (
                  r.valid_from && (
                    <div className="mt-0.5 text-[11px] text-emerald-700">
                      {r.published ? `објављен ${r.published} · ` : ""}на снази од {r.valid_from}
                      {r.applies_from && r.applies_from > r.valid_from
                        ? ` · у примени од ${r.applies_from}`
                        : ""}
                    </div>
                  )
                )}
                <p className="mt-1 whitespace-pre-wrap text-xs text-slate-600 line-clamp-2 group-hover:line-clamp-none">
                  {r.text}
                </p>
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}

function EvalPanel({ ev }: { ev: EvalScore }) {
  const pct = (v: number | null) => (v === null ? "—" : `${Math.round(v * 100)}%`);
  return (
    <div className="rounded-md border border-indigo-200 bg-indigo-50/60 p-3">
      <div className="mb-1.5 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-indigo-700">
        Graded
        <span
          className={`rounded-full px-2 py-0.5 text-[10px] ${
            ev.correct ? "bg-emerald-200 text-emerald-800" : "bg-rose-200 text-rose-800"
          }`}
        >
          {ev.correct ? "correct" : ev.correctness === 0.5 ? "partial" : "incorrect"}
        </span>
      </div>
      <div className="grid grid-cols-4 gap-2 text-center text-xs">
        <EvalCell label="accuracy" value={pct(ev.correctness)} />
        <EvalCell label="precision" value={pct(ev.citation_precision)} />
        <EvalCell label="recall" value={pct(ev.citation_recall)} />
        <EvalCell label="cit. F1" value={pct(ev.provenance_fidelity)} />
      </div>
      {ev.judge_rationale && (
        <p className="mt-2 text-xs italic text-slate-600">“{ev.judge_rationale}”</p>
      )}
    </div>
  );
}

function EvalCell({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded bg-white/70 py-1.5">
      <div className="text-sm font-semibold tabular-nums">{value}</div>
      <div className="text-[10px] uppercase tracking-wide text-slate-400">{label}</div>
    </div>
  );
}

// One-line resilience summary: shown only when some LLM stage needed retries
// (or gave up) — quiet runs stay quiet.
function LlmRetries({ llmIo }: { llmIo?: Record<string, LlmIoStage> }) {
  if (!llmIo) return null;
  const noisy = Object.entries(llmIo).filter(
    ([, s]) => (s.failures?.length ?? 0) > 0 || (s.giveups ?? 0) > 0,
  );
  if (noisy.length === 0) return null;
  const parts = noisy.map(([stage, s]) => {
    const fails = s.failures?.length ?? 0;
    const giveups = s.giveups ?? 0;
    return `${stage}: ${fails} failed attempt${fails === 1 ? "" : "s"}${
      giveups ? `, ${giveups} give-up${giveups === 1 ? "" : "s"}` : ""
    }`;
  });
  return (
    <div className="rounded-md bg-amber-50 px-3 py-2 text-xs text-amber-800">
      <span className="font-semibold">LLM retries:</span> {parts.join(" · ")}
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md bg-slate-50 py-2">
      <div className="text-base font-semibold tabular-nums">{value}</div>
      <div className="text-[10px] uppercase tracking-wide text-slate-400">{label}</div>
    </div>
  );
}
