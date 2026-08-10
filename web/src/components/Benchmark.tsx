import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api, type Ablation, type BatchItem, type GoldenItem } from "../api";
import { armLabel, armShortLabel } from "../arms";
import AblationControls from "./AblationControls";

const TIERS = ["easy", "medium", "hard"] as const;
const ARM_COLORS: Record<string, string> = {
  naive: "#94a3b8",
  advanced: "#6366f1",
  graph: "#10b981",
  "context-stuffing": "#f59e0b",
  agentic: "#ec4899",
};
const SUBVIEWS = ["Questions", "Run", "Results"] as const;
type Subview = (typeof SUBVIEWS)[number];

export default function Benchmark() {
  const [view, setView] = useState<Subview>("Questions");
  // The selected golden set scopes the whole tab: which questions you edit,
  // which set a run measures against, and which batches the metrics show.
  const { data: sets = [] } = useQuery({ queryKey: ["goldenSets"], queryFn: api.goldenSets });
  // Derive the effective set: user override, else the flagged default, else first.
  const [setOverride, setSetOverride] = useState<string>("");
  const set = setOverride || (sets.find((s) => s.is_default) ?? sets[0])?.name || "";
  // The running job's id lives HERE, not in RunView — switching to Questions/
  // Results unmounts RunView, and losing the id would orphan a batch that is
  // still running server-side (progress unrecoverable from the UI).
  const [jobId, setJobId] = useState<string | null>(null);

  return (
    <div className="flex h-full min-h-0 flex-col gap-3">
      <div className="flex flex-wrap items-center gap-3">
        <nav className="flex gap-1 self-start rounded-lg bg-slate-100 p-1">
          {SUBVIEWS.map((s) => (
            <button
              key={s}
              onClick={() => setView(s)}
              className={`rounded-md px-3 py-1 text-sm font-medium transition ${
                view === s ? "bg-white shadow-sm" : "text-slate-500 hover:text-slate-800"
              }`}
            >
              {s}
            </button>
          ))}
        </nav>
        <label className="ml-auto flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-slate-400">
          Golden set
          <select
            value={set}
            onChange={(e) => setSetOverride(e.target.value)}
            className="rounded-lg border border-slate-300 bg-white px-2 py-1.5 text-sm font-normal normal-case tracking-normal text-slate-800"
          >
            {sets.map((s) => (
              <option key={s.name} value={s.name}>
                {s.name} ({s.items}){s.is_default ? " · default" : ""}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {view === "Questions" && <Questions set={set} />}
        {view === "Run" && <RunView set={set} jobId={jobId} setJobId={setJobId} />}
        {view === "Results" && <Results set={set} />}
      </div>
    </div>
  );
}

// ── Questions: view / edit / add per tier ──────────────────────────────────
function Questions({ set }: { set: string }) {
  const qc = useQueryClient();
  const { data: golden = [] } = useQuery({
    queryKey: ["golden", set],
    queryFn: () => api.golden(set),
    enabled: !!set,
  });
  const [editing, setEditing] = useState<GoldenItem | null>(null);

  const del = useMutation({
    mutationFn: (id: string) => api.goldenDelete(id, set),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["golden"] });
      qc.invalidateQueries({ queryKey: ["goldenSets"] });
    },
  });

  return (
    <div className="space-y-4">
      {TIERS.map((tier) => {
        const items = golden.filter((g) => g.category === tier);
        return (
          <div key={tier}>
            <div className="mb-1.5 flex items-center gap-2">
              <h3 className="text-sm font-semibold capitalize">{tier}</h3>
              <span className="text-xs text-slate-400">{items.length} questions</span>
              <button
                onClick={() =>
                  setEditing({
                    item_id: "",
                    category: tier,
                    query: { text: "", filters: { law_slug: "" } },
                    gold_answer: "",
                    gold_citations: [],
                  })
                }
                className="ml-auto rounded-md border border-slate-200 px-2 py-0.5 text-xs text-slate-600 hover:border-slate-400"
              >
                + Add {tier}
              </button>
            </div>
            <ul className="space-y-1.5">
              {items.map((g) => (
                <li key={g.item_id} className="rounded-md border border-slate-200 bg-white px-3 py-2 text-sm">
                  <div className="flex items-start gap-2">
                    <span className="mt-0.5 rounded bg-slate-100 px-1.5 text-xs tabular-nums">
                      {g.item_id}
                    </span>
                    <div className="min-w-0 flex-1">
                      <p className="font-medium">{g.query.text}</p>
                      <p className="mt-0.5 line-clamp-2 text-xs text-slate-500">{g.gold_answer}</p>
                      <div className="mt-1 flex flex-wrap gap-1">
                        {g.gold_citations.map((c) => (
                          <span key={c} className="rounded bg-slate-50 px-1.5 py-0.5 text-[10px] text-slate-500">
                            {c}
                          </span>
                        ))}
                      </div>
                    </div>
                    <div className="flex shrink-0 gap-1">
                      <button onClick={() => setEditing(g)} className="text-xs text-slate-400 hover:text-slate-700">
                        edit
                      </button>
                      <button
                        onClick={() => del.mutate(g.item_id)}
                        className="text-xs text-slate-400 hover:text-rose-600"
                      >
                        del
                      </button>
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          </div>
        );
      })}
      {editing && <Editor item={editing} set={set} onClose={() => setEditing(null)} />}
    </div>
  );
}

function Editor({ item, set, onClose }: { item: GoldenItem; set: string; onClose: () => void }) {
  const qc = useQueryClient();
  const [text, setText] = useState(item.query.text);
  const [answer, setAnswer] = useState(item.gold_answer ?? "");
  const [lawSlug, setLawSlug] = useState(item.query.filters?.law_slug ?? "");
  const [cites, setCites] = useState(item.gold_citations.join("\n"));
  const [err, setErr] = useState<string | null>(null);

  const save = useMutation({
    // Partial payload by design — the backend MERGES into the stored item, so
    // annotations the editor doesn't surface (capability, temporal_mode,
    // query.as_of, reachability labels) survive an edit. Empty answer → null
    // keeps abstain items (a first-class TaskItem concept) authorable; an
    // empty law-slug field must not fabricate a filters override.
    mutationFn: () =>
      api.goldenUpsert(
        {
          item_id: item.item_id,
          category: item.category,
          query: {
            text: text.trim(),
            ...(lawSlug.trim() ? { filters: { law_slug: lawSlug.trim() } } : {}),
          },
          gold_answer: answer.trim() || null,
          gold_citations: cites.split("\n").map((c) => c.trim()).filter(Boolean),
        },
        set,
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["golden"] });
      qc.invalidateQueries({ queryKey: ["goldenSets"] });
      onClose();
    },
    onError: (e) => setErr(String(e)),
  });

  return (
    <div className="fixed inset-0 z-10 flex items-center justify-center bg-black/30 p-4" onClick={onClose}>
      <div
        className="w-full max-w-2xl space-y-3 rounded-xl bg-white p-5 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="font-semibold">
          {item.item_id ? `Edit ${item.item_id}` : `New ${item.category} question`}
        </h3>
        <Field label="Question (Serbian Cyrillic)">
          <textarea value={text} onChange={(e) => setText(e.target.value)} rows={2} className={inputCls} />
        </Field>
        <Field label="Gold answer">
          <textarea value={answer} onChange={(e) => setAnswer(e.target.value)} rows={3} className={inputCls} />
        </Field>
        <Field label="Primary law slug (e.g. zakon-o-radu)">
          <input value={lawSlug} onChange={(e) => setLawSlug(e.target.value)} className={inputCls} />
        </Field>
        <Field label="Gold citations — one per line, format: source_id::član N">
          <textarea
            value={cites}
            onChange={(e) => setCites(e.target.value)}
            rows={3}
            className={`${inputCls} font-mono text-xs`}
          />
        </Field>
        {err && <p className="rounded bg-rose-50 px-2 py-1 text-xs text-rose-700">{err}</p>}
        <div className="flex justify-end gap-2">
          <button onClick={onClose} className="rounded-lg px-3 py-1.5 text-sm text-slate-500">
            Cancel
          </button>
          <button
            onClick={() => save.mutate()}
            disabled={save.isPending}
            className="rounded-lg bg-slate-900 px-4 py-1.5 text-sm font-medium text-white disabled:opacity-40"
          >
            {save.isPending ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Run: configure + launch a batch, watch progress ────────────────────────
function RunView({
  set,
  jobId,
  setJobId,
}: {
  set: string;
  jobId: string | null;
  setJobId: (id: string | null) => void;
}) {
  const qc = useQueryClient();
  const { data: arms = [] } = useQuery({ queryKey: ["arms"], queryFn: api.arms });
  const [armsOverride, setArmsOverride] = useState<string[] | null>(null);
  const pickedArms = armsOverride ?? arms; // default: all arms, no effect needed
  const [pickedTiers, setPickedTiers] = useState<string[]>([...TIERS]);
  const [label, setLabel] = useState("");
  const [limit, setLimit] = useState("");
  const [ablation, setAblation] = useState<Ablation>({});

  const { data: job } = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => api.benchmarkJob(jobId as string),
    enabled: !!jobId,
    refetchInterval: (q) => (q.state.data?.status === "running" ? 1500 : false),
  });
  // A finished batch must show up in Results without a remount — those queries
  // have no refetch interval and window-focus refetch is globally off.
  const jobDone = job?.status === "done";
  useEffect(() => {
    if (jobDone) {
      qc.invalidateQueries({ queryKey: ["batches"] });
      qc.invalidateQueries({ queryKey: ["batchStats"] });
      qc.invalidateQueries({ queryKey: ["batchItems"] });
    }
  }, [jobDone, qc]);

  const run = useMutation({
    mutationFn: () =>
      api.benchmarkRun({
        arms: pickedArms,
        categories: pickedTiers.length === TIERS.length ? null : pickedTiers,
        limit: limit ? Number(limit) : null,
        batch_id: label.trim() || null,
        golden: set || null,
        ablation: Object.keys(ablation).length ? ablation : null,
      }),
    onSuccess: (j) => setJobId(j.job_id),
  });

  const toggle = (arr: string[], set: (v: string[]) => void, v: string) =>
    set(arr.includes(v) ? arr.filter((x) => x !== v) : [...arr, v]);

  return (
    <div className="max-w-xl space-y-4">
      <div className="rounded-lg border border-slate-200 bg-white p-4">
        <h3 className="mb-1 text-sm font-semibold">Configure run</h3>
        <p className="mb-3 text-xs text-slate-400">
          Measuring against golden set <span className="font-medium text-slate-600">{set || "…"}</span>
          {" "}— change it with the picker above.
        </p>
        <Field label="Arms">
          <div className="flex flex-wrap gap-2">
            {arms.map((a) => (
              <Chip key={a} on={pickedArms.includes(a)} onClick={() => toggle(pickedArms, setArmsOverride, a)}>
                {armLabel(a)}
              </Chip>
            ))}
          </div>
        </Field>
        <Field label="Tiers">
          <div className="flex flex-wrap gap-2">
            {TIERS.map((t) => (
              <Chip key={t} on={pickedTiers.includes(t)} onClick={() => toggle(pickedTiers, setPickedTiers, t)}>
                {t}
              </Chip>
            ))}
          </div>
        </Field>
        <Field label="Ablations (applied to every arm in the run)">
          <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
            <AblationControls value={ablation} onChange={setAblation} arms={pickedArms} />
          </div>
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Batch label (optional)">
            <input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="auto" className={inputCls} />
          </Field>
          <Field label="Limit — total questions, applied after the tier filter (optional)">
            <input value={limit} onChange={(e) => setLimit(e.target.value)} placeholder="all" className={inputCls} />
          </Field>
        </div>
        <button
          onClick={() => run.mutate()}
          disabled={run.isPending || pickedArms.length === 0 || pickedTiers.length === 0}
          className="mt-2 rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
        >
          Run benchmark
        </button>
        <p className="mt-2 text-xs text-slate-400">
          Real model calls — the full 60×5 matrix costs ≈ $15–25 and takes ~45–60 min. Runs in the
          background; watch progress below or in the Results tab.
        </p>
      </div>

      {job && (
        <div className="rounded-lg border border-slate-200 bg-white p-4">
          <div className="mb-1 flex items-center justify-between text-sm">
            <span className="font-medium">
              {job.batch_id}
              {job.ablation && job.ablation !== "default" ? (
                <span className="ml-2 rounded bg-rose-50 px-1.5 py-0.5 text-xs text-rose-700">
                  {job.ablation}
                </span>
              ) : null}
            </span>
            <span
              className={`rounded-full px-2 py-0.5 text-xs ${
                job.status === "done"
                  ? "bg-emerald-100 text-emerald-700"
                  : job.status === "error"
                    ? "bg-rose-100 text-rose-700"
                    : "bg-sky-100 text-sky-700"
              }`}
            >
              {job.status}
            </span>
          </div>
          <div className="h-2 w-full overflow-hidden rounded bg-slate-100">
            <div
              className="h-full bg-indigo-500 transition-all"
              style={{ width: `${job.total ? (job.done / job.total) * 100 : 0}%` }}
            />
          </div>
          <p className="mt-1 text-xs text-slate-500">
            {job.done}/{job.total} {job.last ? `· ${job.last}` : ""} {job.error ? `· ${job.error}` : ""}
          </p>
        </div>
      )}
    </div>
  );
}

// ── Results: per-arm summary + per-question comparison ─────────────────────
function Results({ set }: { set: string }) {
  const { data: batches = [] } = useQuery({ queryKey: ["batches"], queryFn: api.batches });
  const [batchOverride, setBatchOverride] = useState<string>("");
  // Show only batches run against the selected set; fall back to all batches
  // when none match (e.g. legacy runs recorded before the golden column, or the
  // interactive bucket) so nothing becomes unreachable.
  const matching = batches.filter((b) => b.golden === set);
  const shown = matching.length ? matching : batches;
  const shownIds = new Set(shown.map((b) => b.batch_id));
  const batch =
    (batchOverride && shownIds.has(batchOverride) ? batchOverride : "") ||
    shown.find((b) => b.batch_id.startsWith("bench"))?.batch_id ||
    shown[0]?.batch_id ||
    "";

  const { data: stats } = useQuery({
    queryKey: ["batchStats", batch],
    queryFn: () => api.batchStats(batch),
    enabled: !!batch,
  });
  const { data: items = [] } = useQuery({
    queryKey: ["batchItems", batch],
    queryFn: () => api.batchItems(batch),
    enabled: !!batch,
  });

  const arms = stats ? Object.keys(stats.per_arm) : [];
  const accData = arms.map((a) => ({
    arm: armShortLabel(a),
    accuracy: stats!.per_arm[a].accuracy ?? 0,
    "prov.F1": stats!.per_arm[a].provenance_f1 ?? 0,
  }));

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <label className="text-xs font-medium uppercase tracking-wide text-slate-400">Batch</label>
        <select value={batch} onChange={(e) => setBatchOverride(e.target.value)} className="rounded-lg border border-slate-300 bg-white px-2 py-1.5 text-sm">
          {shown.map((b) => (
            <option key={b.batch_id} value={b.batch_id}>
              {b.batch_id} — {b.runs} runs ({b.scored} scored)
              {b.golden ? ` · ${b.golden}` : ""}
              {b.ablation && b.ablation !== "default" ? ` · ${b.ablation}` : ""}
            </option>
          ))}
        </select>
        {!matching.length && (
          <span className="text-xs text-slate-400">
            no batches for set <span className="font-medium">{set}</span> yet — showing all
          </span>
        )}
        {(() => {
          const abl = shown.find((b) => b.batch_id === batch)?.ablation;
          return abl && abl !== "default" ? (
            <span
              className="rounded-full bg-rose-50 px-2.5 py-0.5 text-xs font-medium text-rose-700"
              title="This batch ran with one or more retrieval capabilities ablated off"
            >
              ablated: {abl}
            </span>
          ) : null;
        })()}
      </div>

      {stats && (
        <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white p-4">
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Per-arm</h3>
          <table className="w-full text-left text-sm">
            <thead className="text-xs uppercase tracking-wide text-slate-400">
              <tr>
                <th className="py-1 pr-3">arm</th>
                <th className="py-1 pr-3 text-right">n</th>
                <th className="py-1 pr-3 text-right">accuracy</th>
                <th className="py-1 pr-3 text-right">precision</th>
                <th className="py-1 pr-3 text-right">recall</th>
                <th className="py-1 pr-3 text-right">cit.F1</th>
                <th className="py-1 pr-3 text-right">abstain</th>
                <th className="py-1 pr-3 text-right">avg ms</th>
                <th className="py-1 text-right">avg $/q</th>
              </tr>
            </thead>
            <tbody>
              {arms.map((a) => {
                const s = stats.per_arm[a];
                return (
                  <tr key={a} className="border-t border-slate-100">
                    <td className="py-1 pr-3 font-medium">{armLabel(a)}</td>
                    <td className="py-1 pr-3 text-right tabular-nums">{s.runs}</td>
                    <td className="py-1 pr-3 text-right tabular-nums">{pct(s.accuracy)}</td>
                    <td className="py-1 pr-3 text-right tabular-nums">{pct(s.citation_precision)}</td>
                    <td className="py-1 pr-3 text-right tabular-nums">{pct(s.citation_recall)}</td>
                    <td className="py-1 pr-3 text-right tabular-nums">{pct(s.provenance_f1)}</td>
                    <td className="py-1 pr-3 text-right tabular-nums">{pct(s.abstain_rate)}</td>
                    <td className="py-1 pr-3 text-right tabular-nums">{s.avg_latency_ms}</td>
                    <td className="py-1 text-right tabular-nums">${(s.avg_cost_usd ?? 0).toFixed(4)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {stats && (
        <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white p-4">
          <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
            Provenance & safety (per arm)
          </h3>
          <p className="mb-2 text-[11px] text-slate-400">
            <b>ret.recall</b> = gold units the retriever surfaced; <b>ret−cit gap</b> = surfaced but
            not cited; <b>misgrounded</b> / <b>temp-wrong-cited</b> = cites inapplicable / out-of-force
            authority; <b>halluc.</b> = incorrect or misgrounded answer; <b>over-abst.</b> / <b>false-ans.</b>
            = refused answerable / answered unanswerable. Lower is better except ret.recall.
          </p>
          <table className="w-full text-left text-sm">
            <thead className="text-xs uppercase tracking-wide text-slate-400">
              <tr>
                <th className="py-1 pr-3">arm</th>
                <th className="py-1 pr-3 text-right">ret.recall</th>
                <th className="py-1 pr-3 text-right">ret−cit gap</th>
                <th className="py-1 pr-3 text-right">misgrounded</th>
                <th className="py-1 pr-3 text-right">temp-wrong-cited</th>
                <th className="py-1 pr-3 text-right">halluc.</th>
                <th className="py-1 pr-3 text-right">over-abst.</th>
                <th className="py-1 text-right">false-ans.</th>
              </tr>
            </thead>
            <tbody>
              {arms.map((a) => {
                const s = stats.per_arm[a];
                return (
                  <tr key={a} className="border-t border-slate-100">
                    <td className="py-1 pr-3 font-medium">{armLabel(a)}</td>
                    <td className="py-1 pr-3 text-right tabular-nums">{pct(s.retrieval_recall)}</td>
                    <td className="py-1 pr-3 text-right tabular-nums text-indigo-600">
                      {signedPct(s.retrieval_citation_gap)}
                    </td>
                    <td className="py-1 pr-3 text-right tabular-nums">{pct(s.misgrounded_rate)}</td>
                    <td className="py-1 pr-3 text-right tabular-nums">
                      {pct(s.temporally_wrong_and_cited_rate)}
                    </td>
                    <td className="py-1 pr-3 text-right tabular-nums">{pct(s.hallucination_rate)}</td>
                    <td className="py-1 pr-3 text-right tabular-nums">{pct(s.over_abstention_rate)}</td>
                    <td className="py-1 text-right tabular-nums">
                      {pct(s.false_answer_rate_unanswerable)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {stats && (
        <div className="rounded-lg border border-slate-200 bg-white p-4">
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
            Accuracy & citation-F1 by arm
          </h3>
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={accData}>
              <CartesianGrid strokeDasharray="3 3" vertical={false} />
              <XAxis dataKey="arm" tickLine={false} interval={0} />
              <YAxis tickLine={false} domain={[0, 1]} width={40} />
              <Tooltip />
              <Legend />
              <Bar dataKey="accuracy" fill="#6366f1" radius={[3, 3, 0, 0]} />
              <Bar dataKey="prov.F1" fill="#10b981" radius={[3, 3, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}

      {items.length > 0 && <PerQuestion items={items} arms={arms} />}
    </div>
  );
}

// per-question heatmap + comparison chart
function PerQuestion({ items, arms }: { items: BatchItem[]; arms: string[] }) {
  const [sel, setSel] = useState<string | null>(null);
  const chartData = items.map((it) => {
    const row: Record<string, number | string> = { item: it.item_id };
    for (const a of arms) row[a] = it.arms[a]?.correctness ?? 0;
    return row;
  });
  const selected = items.find((it) => it.item_id === sel);

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-slate-200 bg-white p-4">
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
          Per-question correctness — each method compared (click a row)
        </h3>
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="text-slate-400">
              <tr>
                <th className="py-1 pr-2">item</th>
                <th className="py-1 pr-2">tier</th>
                <th className="max-w-[320px] py-1 pr-2">question</th>
                {arms.map((a) => (
                  <th key={a} className="px-1 py-1 text-center">{armLabel(a)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {items.map((it) => (
                <tr
                  key={it.item_id}
                  onClick={() => setSel(it.item_id === sel ? null : it.item_id)}
                  className={`cursor-pointer border-t border-slate-100 hover:bg-slate-50 ${
                    sel === it.item_id ? "bg-slate-100" : ""
                  }`}
                >
                  <td className="py-1 pr-2 font-medium tabular-nums">{it.item_id}</td>
                  <td className="py-1 pr-2 text-slate-400">{it.tier}</td>
                  <td className="max-w-[320px] truncate py-1 pr-2">{it.question}</td>
                  {arms.map((a) => {
                    const c = it.arms[a];
                    return (
                      <td key={a} className="px-1 py-1 text-center">
                        <span
                          className="inline-block h-4 w-7 rounded"
                          style={{ background: cellColor(c?.correctness, c?.abstained) }}
                          title={cellTitle(a, c)}
                        />
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="mt-2 flex items-center gap-3 text-[10px] text-slate-400">
          <Legend2 color="#10b981" label="correct" />
          <Legend2 color="#f59e0b" label="partial" />
          <Legend2 color="#f43f5e" label="incorrect" />
          <Legend2 color="#cbd5e1" label="abstained / n/a" />
        </div>
      </div>

      {selected && (
        <div className="rounded-lg border border-slate-200 bg-white p-4 text-sm">
          <p className="font-medium">{selected.item_id} · {selected.question}</p>
          <div className="mt-2 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {arms.map((a) => {
              const c = selected.arms[a];
              return (
                <div key={a} className="rounded-md border border-slate-150 bg-slate-50 px-2.5 py-1.5 text-xs">
                  <div className="flex items-center justify-between">
                    <span className="font-medium" style={{ color: ARM_COLORS[a] }}>{armLabel(a)}</span>
                    <span>{c ? `${Math.round((c.correctness ?? 0) * 100)}%` : "—"}</span>
                  </div>
                  <div className="mt-0.5 text-slate-500">
                    P {pct(c?.citation_precision)} · R {pct(c?.citation_recall)} · F1 {pct(c?.provenance_f1)}
                    {c?.abstained && " · abstained"}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      <div className="rounded-lg border border-slate-200 bg-white p-4">
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
          Overview — correctness per question, by method
        </h3>
        <div className="overflow-x-auto">
          <div style={{ width: Math.max(640, items.length * 46) }}>
            <ResponsiveContainer width="100%" height={240}>
              <BarChart data={chartData} barCategoryGap="20%">
                <CartesianGrid strokeDasharray="3 3" vertical={false} />
                <XAxis dataKey="item" tickLine={false} interval={0} angle={-90} height={50} textAnchor="end" fontSize={9} />
                <YAxis tickLine={false} domain={[0, 1]} width={30} />
                <Tooltip />
                <Legend />
                {arms.map((a) => (
                  <Bar key={a} dataKey={a} name={armLabel(a)} radius={[2, 2, 0, 0]}>
                    {chartData.map((_, i) => (
                      <Cell key={i} fill={ARM_COLORS[a] ?? "#64748b"} />
                    ))}
                  </Bar>
                ))}
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      </div>
    </div>
  );
}

// ── small helpers ──────────────────────────────────────────────────────────
const inputCls =
  "w-full rounded-lg border border-slate-300 px-2.5 py-1.5 text-sm focus:border-slate-500 focus:outline-none";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="mb-2 block">
      <span className="mb-1 block text-[11px] font-medium uppercase tracking-wide text-slate-400">{label}</span>
      {children}
    </label>
  );
}

function Chip({ on, onClick, children }: { on: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      onClick={onClick}
      className={`rounded-full px-3 py-1 text-xs font-medium ${
        on ? "bg-slate-900 text-white" : "border border-slate-300 text-slate-600"
      }`}
    >
      {children}
    </button>
  );
}

function Legend2({ color, label }: { color: string; label: string }) {
  return (
    <span className="flex items-center gap-1">
      <span className="h-2.5 w-2.5 rounded" style={{ background: color }} />
      {label}
    </span>
  );
}

const pct = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`;

const signedPct = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${Math.round(v * 100)}%`;

function cellColor(correctness: number | null | undefined, abstained?: boolean): string {
  if (abstained || correctness === null || correctness === undefined) return "#cbd5e1";
  if (correctness >= 1) return "#10b981";
  if (correctness >= 0.5) return "#f59e0b";
  return "#f43f5e";
}

function cellTitle(arm: string, c?: { correctness: number | null; abstained: boolean }): string {
  if (!c) return `${arm}: n/a`;
  return `${arm}: ${c.abstained ? "abstained" : `${Math.round((c.correctness ?? 0) * 100)}%`}`;
}
