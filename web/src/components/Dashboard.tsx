import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api } from "../api";
import { armLabel, armShortLabel } from "../arms";

// Deployable arms in capability-ladder order; context-stuffing (the oracle
// reference condition) sorts last — a reference condition, not a ranked
// competitor (mirrors the backend's evals.CEILING_ARMS / _rank_arms).
const ARM_ORDER = ["naive", "advanced", "graph", "agentic", "context-stuffing"];

export default function Dashboard() {
  const [batch, setBatch] = useState<string>(""); // "" = all runs
  const { data: batches = [] } = useQuery({ queryKey: ["batches"], queryFn: api.batches });
  const { data: stats } = useQuery({
    queryKey: ["stats", batch],
    queryFn: () => api.stats(batch || undefined),
    refetchInterval: 5000,
  });
  const { data: corpus = [] } = useQuery({ queryKey: ["corpus"], queryFn: api.corpus });

  if (!stats) return null;

  const arms = ARM_ORDER.filter((a) => stats.per_arm[a]).concat(
    Object.keys(stats.per_arm).filter((a) => !ARM_ORDER.includes(a)),
  );
  const latencyData = arms.map((a) => ({
    arm: armShortLabel(a),
    p50: stats.per_arm[a].p50_total_ms,
    p95: stats.per_arm[a].p95_total_ms,
  }));
  const costData = arms.map((a) => ({
    arm: armShortLabel(a),
    "avg $/query": stats.per_arm[a].avg_cost_usd,
  }));

  return (
    <div className="space-y-4 overflow-y-auto pb-6">
      <div className="flex items-center gap-2">
        <label className="text-xs font-medium uppercase tracking-wide text-slate-400">Scope</label>
        <select
          value={batch}
          onChange={(e) => setBatch(e.target.value)}
          className="rounded-lg border border-slate-300 bg-white px-2 py-1.5 text-sm"
        >
          <option value="">All runs</option>
          {batches.map((b) => (
            <option key={b.batch_id} value={b.batch_id}>
              {b.batch_id} ({b.runs}){b.golden ? ` · ${b.golden}` : ""}
            </option>
          ))}
        </select>
        <span className="text-xs text-slate-400">
          graded accuracy/precision/recall live in the Benchmark → Results tab
        </span>
      </div>
      <div className="grid grid-cols-4 gap-3">
        <Card label="total runs" value={String(stats.total_runs)} />
        <Card label="total cost" value={`$${stats.total_cost_usd.toFixed(3)}`} />
        <Card
          label="corpus"
          value={`${corpus.length} laws / ${corpus.reduce((s, l) => s + l.chunks, 0)} članova`}
        />
        <Card
          label="repealed članova"
          value={String(corpus.reduce((s, l) => s + (l.repealed_clanovi ?? 0), 0))}
        />
      </div>

      <div className="grid grid-cols-3 gap-3">
        {arms.map((a) => {
          const s = stats.per_arm[a];
          return (
            <div key={a} className="rounded-lg border border-slate-200 bg-white p-4">
              <h3 className="mb-2 font-semibold">{armLabel(a)}</h3>
              <dl className="grid grid-cols-2 gap-y-1 text-sm">
                <dt className="text-slate-500">runs</dt>
                <dd className="text-right tabular-nums">{s.runs}</dd>
                <dt className="text-slate-500">abstain rate</dt>
                <dd className="text-right tabular-nums">{(s.abstain_rate * 100).toFixed(0)}%</dd>
                <dt className="text-slate-500">avg latency</dt>
                <dd className="text-right tabular-nums">{s.avg_total_ms} ms</dd>
                <dt className="text-slate-500">avg $/query</dt>
                <dd className="text-right tabular-nums">${s.avg_cost_usd.toFixed(5)}</dd>
                <dt className="text-slate-500">tokens in/out</dt>
                <dd className="text-right tabular-nums">
                  {s.prompt_tokens.toLocaleString()}/{s.output_tokens.toLocaleString()}
                </dd>
              </dl>
            </div>
          );
        })}
      </div>

      <div className="grid grid-cols-2 gap-3">
        <ChartCard title="Latency by arm (ms)">
          <BarChart data={latencyData}>
            <CartesianGrid strokeDasharray="3 3" vertical={false} />
            <XAxis dataKey="arm" tickLine={false} interval={0} />
            <YAxis tickLine={false} width={50} />
            <Tooltip />
            <Legend />
            <Bar dataKey="p50" fill="#6366f1" radius={[3, 3, 0, 0]} />
            <Bar dataKey="p95" fill="#c7d2fe" radius={[3, 3, 0, 0]} />
          </BarChart>
        </ChartCard>
        <ChartCard title="Cost by arm ($/query)">
          <BarChart data={costData}>
            <CartesianGrid strokeDasharray="3 3" vertical={false} />
            <XAxis dataKey="arm" tickLine={false} interval={0} />
            <YAxis tickLine={false} width={70} tickFormatter={(v: number) => v.toFixed(4)} />
            <Tooltip />
            <Bar dataKey="avg $/query" fill="#f59e0b" radius={[3, 3, 0, 0]} />
          </BarChart>
        </ChartCard>
      </div>

    </div>
  );
}

function Card({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4">
      <div className="text-xl font-semibold tabular-nums">{value}</div>
      <div className="text-xs uppercase tracking-wide text-slate-400">{label}</div>
    </div>
  );
}

function ChartCard({ title, children }: { title: string; children: React.ReactElement }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4">
      <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">{title}</h3>
      <ResponsiveContainer width="100%" height={240}>
        {children}
      </ResponsiveContainer>
    </div>
  );
}
