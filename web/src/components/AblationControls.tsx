import { useQuery } from "@tanstack/react-query";
import { api, type Ablation } from "../api";

// Generic capability-toggle controls rendered from /api/ablations — so the UI
// surface can't drift from the backend AblationConfig. Controlled component:
// `value` holds only the overrides (toggles that differ from production default).
// `arms` (optional) scopes the visible toggles to those that affect the picked
// arm(s) — naive/context-stuffing have none and render a hint.
export default function AblationControls({
  value,
  onChange,
  arms,
}: {
  value: Ablation;
  onChange: (v: Ablation) => void;
  arms?: string[];
}) {
  const { data } = useQuery({ queryKey: ["ablations"], queryFn: api.ablations });
  if (!data) return null;
  const { spec, defaults } = data;
  const toggles =
    arms && arms.length ? spec.filter((t) => t.arms.some((a) => arms.includes(a))) : spec;
  if (!toggles.length)
    return (
      <p className="text-xs text-slate-400">
        No retrieval ablations for this arm — it's the {arms?.includes("naive") ? "flat baseline" : "fixed reference"}.
      </p>
    );

  const groups = [...new Set(toggles.map((t) => t.group))];
  const isOn = (key: string) => value[key] ?? defaults[key];
  const flip = (key: string) => {
    const next = { ...value };
    const newVal = !isOn(key);
    if (newVal === defaults[key]) delete next[key]; // back to default → drop the override
    else next[key] = newVal;
    onChange(next);
  };

  return (
    <div className="space-y-2">
      {groups.map((g) => (
        <div key={g}>
          <p className="mb-1 text-[10px] font-medium uppercase tracking-wide text-slate-400">{g}</p>
          <div className="flex flex-wrap gap-1.5">
            {toggles
              .filter((t) => t.group === g)
              .map((t) => {
                const on = isOn(t.key);
                return (
                  <button
                    key={t.key}
                    type="button"
                    title={`${t.description}\n(${t.arms.join(", ")})`}
                    onClick={() => flip(t.key)}
                    className={`rounded-full border px-2.5 py-1 text-xs font-medium transition ${
                      on
                        ? "border-slate-300 bg-slate-900 text-white"
                        : "border-rose-200 bg-rose-50 text-rose-700 line-through"
                    }`}
                  >
                    {t.label}
                  </button>
                );
              })}
          </div>
        </div>
      ))}
      <p className="text-[10px] text-slate-400">
        Filled = on (production default) · struck = ablated off. Each isolates one capability.
      </p>
    </div>
  );
}
