import { useQuery } from "@tanstack/react-query";
import { api, type CorpusLaw, type GraphStats } from "../api";

// 10 thematic clusters + the shared backbone (registry.py CLUSTER_NAMES).
const CLUSTERS: Record<string, string> = {
  backbone: "Shared backbone",
  "1": "Employment & contributions",
  "2": "Family & inheritance",
  "3": "Real estate & housing",
  "4": "Consumer & transactions",
  "5": "Traffic & misdemeanors",
  "6": "Personal taxes & freelancing",
  "7": "Data & digital",
  "8": "Business & company (SME)",
  "9": "Disputes & enforcement",
  "10": "Health & social welfare",
};
// Maximally-distinct categorical palette (tab10-derived, darkened for white text):
// well-separated across hue + lightness so no two clusters read alike.
const CLUSTER_COLORS: Record<string, string> = {
  backbone: "#475569", // slate (neutral)
  "1": "#d62728", // red
  "2": "#2e9e2e", // green
  "3": "#1f77b4", // blue
  "4": "#e8590c", // orange
  "5": "#8e44ad", // purple
  "6": "#0e7490", // teal
  "7": "#c2185b", // magenta
  "8": "#8c564b", // brown
  "9": "#827717", // olive
  "10": "#b7791f", // gold
};
const CLUSTER_KEYS = Object.keys(CLUSTERS);

// Legacy/repealed exports sometimes parse to a bare "ЗАКОН"; fall back to the
// registry title (its name precedes the gazette list).
function lawName(law: { title: string; registry_title?: string; slug: string }): string {
  const t = (law.title || "").trim().replace(/^Редакцијски пречишћен текст\s*/i, "").trim();
  const bare = /^(закон|zakon)$/i.test(t) || t.length < 7;
  if (bare && law.registry_title) return law.registry_title.split(":")[0].trim();
  return t || law.slug;
}

export default function Corpus() {
  const { data: corpus = [] } = useQuery({ queryKey: ["corpus"], queryFn: api.corpus });
  const { data: summary } = useQuery({ queryKey: ["corpusSummary"], queryFn: api.corpusSummary });
  const { data: gstats } = useQuery({ queryKey: ["corpusStats"], queryFn: api.corpusStats });

  const totalClanova = corpus.reduce((s, l) => s + l.chunks, 0);
  const totalRepealed = corpus.reduce((s, l) => s + (l.repealed_clanovi ?? 0), 0);

  return (
    <div className="h-full space-y-4 overflow-y-auto pb-6">
      <div className="grid grid-cols-3 gap-3">
        <Card label="seed + backbone laws" value={String(corpus.length)} />
        <Card label="članova (in laws)" value={totalClanova.toLocaleString()} />
        <Card label="repealed članova" value={String(totalRepealed)} />
      </div>
      {summary && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Card label="materialized bylaws" value={summary.materialized_bylaws.toLocaleString()} />
          <Card label="CC decisions" value={String(summary.cc_decisions)} />
          <Card label="stub acts (graph-only)" value={summary.stubs.toLocaleString()} />
          <Card label="acts total (incl. periphery)" value={summary.acts_total.toLocaleString()} />
        </div>
      )}

      <ClusterMap laws={corpus} />

      {gstats && gstats.nodes > 0 && <GraphTopology s={gstats} />}

      <AllLawsTable laws={corpus} />
    </div>
  );
}

// The colour ↔ number ↔ name key for the ten thematic clusters + the shared
// backbone — the legend for every cluster-coloured chart/chip on this tab.
function ClusterMap({ laws }: { laws: CorpusLaw[] }) {
  const countOf = (c: string) => laws.filter((l) => (l.cluster ?? "") === c).length;
  return (
    <Panel title="Cluster map — the ten thematic clusters + the shared backbone">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
        {CLUSTER_KEYS.map((c) => (
          <div
            key={c}
            className="flex items-center gap-2 rounded-md border border-slate-200 bg-slate-50 px-2.5 py-1.5"
          >
            <span
              className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-[10px] font-semibold text-white"
              style={{ background: CLUSTER_COLORS[c] }}
            >
              {c === "backbone" ? "B" : c}
            </span>
            <div className="min-w-0">
              <div className="truncate text-xs font-medium" title={CLUSTERS[c]}>
                {CLUSTERS[c]}
              </div>
              <div className="text-[10px] text-slate-400">{countOf(c)} laws</div>
            </div>
          </div>
        ))}
      </div>
    </Panel>
  );
}

function AllLawsTable({ laws }: { laws: CorpusLaw[] }) {
  const sorted = [...laws].sort((a, b) =>
    (a.cluster ?? "").localeCompare(b.cluster ?? "") || lawName(a).localeCompare(lawName(b)),
  );
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4">
      <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
        All laws ({laws.length}) — gazette issues link to the source on PIS
      </h3>
      <table className="w-full text-left text-sm">
        <thead className="text-xs uppercase tracking-wide text-slate-400">
          <tr>
            <th className="py-1.5 pr-3">Cluster</th>
            <th className="w-[220px] py-1.5 pr-3">Law</th>
            <th className="py-1.5 pr-3">Gazette chain (links to PIS)</th>
            <th className="py-1.5 pr-3 text-right">članova</th>
            <th className="py-1.5 pr-3 text-right">repealed</th>
            <th
              className="whitespace-nowrap py-1.5 pr-3 text-right"
              title="Publication of the base gazette (objavljivanje)"
            >
              published
            </th>
            <th
              className="whitespace-nowrap py-1.5 pr-3 text-right"
              title="Entry into force (stupanje na snagu) — from the law's „ступа на снагу“ final provision, constitutional +8-day default otherwise"
            >
              in force from
            </th>
            <th
              className="whitespace-nowrap py-1.5 text-right"
              title="PIS versionApplicationDate (početak primene) — blank where PIS provides no distinct start-of-application date for this version"
            >
              applies from
            </th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((l) => (
            <tr key={l.slug} className="border-t border-slate-100 align-top">
              <td className="py-1.5 pr-3">
                <span
                  className="inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[11px] font-medium text-white"
                  style={{ background: CLUSTER_COLORS[l.cluster ?? ""] ?? "#64748b" }}
                  title={CLUSTERS[l.cluster ?? ""] ?? ""}
                >
                  {l.cluster || "?"}
                </span>
              </td>
              <td className="w-[220px] py-1.5 pr-3">
                {/* no ELI → plain text, not a dead "#" link */}
                <a
                  {...(l.eli ? { href: l.eli, target: "_blank", rel: "noreferrer" } : {})}
                  className={`block max-w-[210px] truncate font-medium ${l.eli ? "hover:underline" : "cursor-default"}`}
                  title={l.registry_title || lawName(l)}
                >
                  {lawName(l)}
                </a>
                <div className="max-w-[210px] truncate text-[10px] text-slate-400">{l.slug}</div>
              </td>
              <td className="max-w-[240px] py-1.5 pr-3 text-xs text-slate-500">
                {l.gazette_chain.map((g, i) => (
                  <span key={`${g}-${i}`}>
                    {i > 0 && " · "}
                    {l.eli ? (
                      <a
                        href={l.eli}
                        target="_blank"
                        rel="noreferrer"
                        className="hover:text-slate-800 hover:underline"
                        title={`Службени гласник ${g} — изворни текст на ПИС`}
                      >
                        {g}
                      </a>
                    ) : (
                      g
                    )}
                  </span>
                ))}
              </td>
              <td className="py-1.5 pr-3 text-right tabular-nums">{l.main_clanovi}</td>
              <td className="py-1.5 pr-3 text-right tabular-nums">{l.repealed_clanovi}</td>
              <td className="whitespace-nowrap py-1.5 pr-3 text-right tabular-nums">
                {l.law_published || "—"}
              </td>
              <td className="whitespace-nowrap py-1.5 pr-3 text-right tabular-nums">
                {l.law_valid_from || "—"}
              </td>
              <td className="whitespace-nowrap py-1.5 text-right tabular-nums">
                {l.applies_from || "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// Graph density — the diagnostic behind the GraphRAG sparsity finding
// (benchmark.md §6 / knowledge_base.md §9). Two layers, reported separately: the
// numbered cross-reference web (UPUĆUJE_NA) is intrinsically sparse — Serbian
// statutes rarely cite each other by article number — but the layer the graph
// arm actually traverses adds DEFINED_IN (an article → its law's definitions
// article *when it uses a defined term* — term-aware), so its real reach is
// higher than "isolated %" implies, without the old signal-free law-global edge.
function GraphTopology({ s }: { s: GraphStats }) {
  const reach = s.arm_reachable_pct;
  return (
    <Panel title="Graph topology — cross-reference density vs. arm expansion reach">
      <div className="grid grid-cols-3 gap-2 sm:grid-cols-6">
        <Stat label="članci (nodes)" value={s.nodes.toLocaleString()} />
        <Stat label="xref edges (UPUĆUJE_NA)" value={s.semantic_edges.toLocaleString()} />
        <Stat label="xref mean out-degree" value={s.mean_out_degree.toFixed(2)} />
        <Stat label="xref isolated" value={`${s.isolated_pct}%`} />
        <Stat label="cross-law edges" value={s.cross_law_edges.toLocaleString()} />
        {reach != null ? (
          <Stat label="arm-reachable" value={`${reach}%`} warn={reach < 40} />
        ) : (
          <Stat label="max out-degree" value={String(s.max_out_degree)} />
        )}
      </div>
      <p className="mt-2 text-[11px] text-slate-600">
        The numbered cross-reference web is intrinsically thin ({s.isolated_pct}% of članci have no
        outgoing UPUĆUJE_NA — Serbian statutes seldom cite each other by article number).
        {reach != null && (
          <>
            {" "}But the graph arm also expands over term-aware{" "}
            <span className="font-medium">DEFINED_IN</span>, so it can expand from{" "}
            <span className="font-medium">{reach}%</span> of članci
            (mean {s.expansion_mean_out_degree?.toFixed(2)}). GraphRAG's remaining edge is
            definition-/validity-aware traversal (incl. the law→bylaw and constitutional-court
            hops), not citation multi-hop (see benchmark.md §6).
          </>
        )}
      </p>
    </Panel>
  );
}

function Stat({ label, value, warn }: { label: string; value: string; warn?: boolean }) {
  return (
    <div className={`rounded-lg border p-2 ${warn ? "border-amber-300 bg-amber-50" : "border-slate-200"}`}>
      <div className="text-base font-semibold tabular-nums">{value}</div>
      <div className="text-[10px] uppercase tracking-wide text-slate-400">{label}</div>
    </div>
  );
}

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4">
      <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">{title}</h3>
      {children}
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
