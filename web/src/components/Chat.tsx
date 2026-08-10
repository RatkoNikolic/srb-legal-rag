import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api, type Ablation, type Run, type Turn } from "../api";
import { armLabel } from "../arms";
import AblationControls from "./AblationControls";

interface Message {
  role: "user" | "assistant";
  text: string;
  run?: Run;
}

// id generation is impure → kept out of render (effect/event handler only)
function makeConversationId(): string {
  return `c-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

// Tidy a stored law title for the delegation-chain label: drop the
// editorial-consolidation prefix and normalize the SHOUTING "ЗАКОН".
function cleanLawTitle(title: string): string {
  return title
    .replace(/^Редакцијски пречишћен текст\s*/i, "")
    .replace(/^ЗАКОН\s*/i, "Закон ")
    .trim();
}

// The capability-toggle config the backend recorded with this run (extras.ablation_label).
function ablationOf(run: Run): string {
  return (run.extras?.ablation_label as string) ?? "default";
}

// The conversation transcript IS the memory: prior turns are sent with each
// question so the backend can resolve follow-ups ("а ко је утврђује?").
function asHistory(messages: Message[]): Turn[] {
  return messages
    .filter((m) => m.text && !m.text.startsWith("⚠"))
    .map((m) => ({ role: m.role, content: m.text }));
}

export default function Chat() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [question, setQuestion] = useState("");
  const [arm, setArm] = useState("advanced");
  const [asOf, setAsOf] = useState("");
  const [ablation, setAblation] = useState<Ablation>({});
  const [showAblation, setShowAblation] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  // one id per conversation → groups this conversation's runs in telemetry
  const conversationIdRef = useRef<string | null>(null);
  useEffect(() => {
    conversationIdRef.current ??= makeConversationId();
  }, []);

  const { data: arms = ["naive", "advanced", "graph"] } = useQuery({
    queryKey: ["arms"],
    queryFn: api.arms,
  });

  const ask = useMutation({
    mutationFn: (q: string) =>
      api.ask(q, arm, asOf || null, asHistory(messages), conversationIdRef.current, ablation),
    // capture the conversation the question belongs to, so a response landing
    // after "New chat" can't leak into the fresh conversation
    onMutate: (q: string) => {
      setMessages((m) => [...m, { role: "user", text: q }]);
      setQuestion("");
      return { cid: conversationIdRef.current };
    },
    onSuccess: (run, _q, ctx) => {
      if (ctx?.cid !== conversationIdRef.current) return; // stale: conversation was reset
      setMessages((m) => [...m, { role: "assistant", text: run.answer_text, run }]);
      setTimeout(() => bottomRef.current?.scrollIntoView({ behavior: "smooth" }), 50);
    },
    onError: (err, _q, ctx) => {
      if (ctx?.cid !== conversationIdRef.current) return;
      setMessages((m) => [...m, { role: "assistant", text: `⚠ ${String(err)}` }]);
    },
  });

  function newConversation() {
    conversationIdRef.current = makeConversationId();
    setMessages([]);
    setQuestion("");
  }

  return (
    <div className="flex h-full flex-col">
      {messages.length > 0 && (
        <div className="flex items-center justify-between border-b border-slate-100 pb-2">
          <span className="text-xs text-slate-400">
            Conversation · {asHistory(messages).length} turns
          </span>
          <button
            onClick={newConversation}
            className="rounded-md border border-slate-200 px-2.5 py-1 text-xs text-slate-500 hover:border-slate-400"
          >
            New chat
          </button>
        </div>
      )}
      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto pb-4">
        {messages.length === 0 && (
          <div className="mt-16 text-center text-sm text-slate-400">
            Постави питање о радном праву, ПИО, доприносима, странцима…
            <br />
            Pick a retrieval arm and (optionally) an „as of" date — temporal correctness is the
            whole point.
          </div>
        )}
        {messages.map((m, i) =>
          m.role === "user" ? (
            <div key={i} className="flex justify-end">
              <div className="max-w-[80%] rounded-2xl rounded-br-sm bg-slate-900 px-4 py-2.5 text-sm text-white">
                {m.text}
              </div>
            </div>
          ) : (
            <AssistantBubble key={i} message={m} />
          ),
        )}
        {ask.isPending && (
          <div className="flex items-center gap-2 text-sm text-slate-400">
            <span className="h-2 w-2 animate-pulse rounded-full bg-slate-400" />
            {armLabel(arm)} is retrieving + generating…
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {showAblation && (
        <div className="border-t border-slate-200 pt-3">
          <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
            <p className="mb-2 text-xs text-slate-500">
              Ablate retrieval capabilities for <span className="font-medium">{armLabel(arm)}</span> —
              toggle a stage off to see what it buys. The exact config is recorded with the answer.
            </p>
            <AblationControls value={ablation} onChange={setAblation} arms={[arm]} />
          </div>
        </div>
      )}

      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (question.trim() && !ask.isPending) ask.mutate(question.trim());
        }}
        className="flex items-end gap-2 border-t border-slate-200 pt-3"
      >
        <div className="flex flex-col gap-1">
          <label
            htmlFor="arm-select"
            className="text-[10px] font-medium uppercase tracking-wide text-slate-400"
          >
            Arm
          </label>
          <select
            id="arm-select"
            value={arm}
            onChange={(e) => {
              setArm(e.target.value);
              // ablation overrides are arm-specific experiment state — carrying
              // them to another arm would silently mislabel its runs as ablated
              setAblation({});
            }}
            className="rounded-lg border border-slate-300 bg-white px-2 py-2 text-sm"
          >
            {arms.map((a) => (
              <option key={a} value={a}>
                {armLabel(a)}
              </option>
            ))}
          </select>
        </div>
        <div className="flex flex-col gap-1">
          <label
            htmlFor="asof-input"
            className="text-[10px] font-medium uppercase tracking-wide text-slate-400"
          >
            As of
          </label>
          <input
            id="asof-input"
            type="date"
            value={asOf}
            onChange={(e) => setAsOf(e.target.value)}
            className="rounded-lg border border-slate-300 bg-white px-2 py-[7px] text-sm"
          />
        </div>
        <div className="flex flex-col gap-1">
          <span className="text-[10px] font-medium uppercase tracking-wide text-slate-400">
            Ablate
          </span>
          <button
            type="button"
            onClick={() => setShowAblation((s) => !s)}
            className={`rounded-lg border px-2.5 py-2 text-sm ${
              Object.keys(ablation).length
                ? "border-rose-300 bg-rose-50 text-rose-700"
                : "border-slate-300 bg-white text-slate-500"
            }`}
            title="Toggle retrieval-capability ablations"
          >
            {Object.keys(ablation).length ? `${Object.keys(ablation).length} off` : "·"}{" "}
            {showAblation ? "▴" : "▾"}
          </button>
        </div>
        <textarea
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              if (question.trim() && !ask.isPending) ask.mutate(question.trim());
            }
          }}
          rows={2}
          placeholder="нпр. Колико износи отпремнина при одласку у пензију?"
          className="flex-1 resize-none rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none"
        />
        <button
          type="submit"
          disabled={ask.isPending || !question.trim()}
          className="rounded-lg bg-slate-900 px-5 py-2.5 text-sm font-medium text-white disabled:opacity-40"
        >
          Ask
        </button>
      </form>
    </div>
  );
}

function AssistantBubble({ message }: { message: Message }) {
  const [showSources, setShowSources] = useState(false);
  const run = message.run;
  return (
    <div className="flex justify-start">
      <div className="max-w-[85%] rounded-2xl rounded-bl-sm border border-slate-200 bg-white px-4 py-3 text-sm shadow-sm">
        {run?.abstained && (
          <div className="mb-2 inline-block rounded-full bg-amber-100 px-2.5 py-0.5 text-xs font-medium text-amber-800">
            abstained{run.abstain_reason ? ` — ${run.abstain_reason}` : ""}
          </div>
        )}
        <p className="whitespace-pre-wrap">{message.text}</p>
        {run && (
          <div className="mt-3 border-t border-slate-100 pt-2">
            <div className="flex flex-wrap items-center gap-1.5">
              {run.citations.map((c, i) => {
                const chipTitle = `на снази од ${c.valid_from ?? "?"} → ${c.valid_to ?? "на снази"}${c.version ? ` · изм. ${c.version}` : ""}${c.parent_title ? `\nдонет на основу: ${cleanLawTitle(c.parent_title)}` : ""}`;
                const body = (
                  <>
                    {c.source_title.replace(/^ЗАКОН\s*/i, "Закон ")} · {c.fragment}
                    {c.parent_title ? (
                      <span className="text-slate-400">
                        {" "}
                        · на основу: {cleanLawTitle(c.parent_title)}
                      </span>
                    ) : null}
                    {c.valid_to ? " ⚠" : ""}
                  </>
                );
                const chipClass =
                  "rounded-full border border-slate-200 bg-slate-50 px-2.5 py-0.5 text-xs text-slate-700";
                // no ELI url (e.g. a stub act) → a plain chip, not a dead link
                return c.url ? (
                  <a
                    key={i}
                    href={c.url}
                    target="_blank"
                    rel="noreferrer"
                    className={`${chipClass} hover:border-slate-400`}
                    title={chipTitle}
                  >
                    {body}
                  </a>
                ) : (
                  <span key={i} className={chipClass} title={chipTitle}>
                    {body}
                  </span>
                );
              })}
              <button
                onClick={() => setShowSources((s) => !s)}
                className="ml-auto text-xs text-slate-400 hover:text-slate-700"
              >
                {run.arm}
                {ablationOf(run) && ablationOf(run) !== "default" ? (
                  <span className="text-rose-500"> · {ablationOf(run)}</span>
                ) : null}{" "}
                · {Math.round(run.timings.total_ms)} ms · ${run.cost_usd.toFixed(4)}{" "}
                {showSources ? "▴" : "▾"}
              </button>
            </div>
            {showSources && (
              <ul className="mt-2 space-y-1.5">
                {run.retrieved.map((r) => (
                  <li key={r.chunk_id} className="rounded-md bg-slate-50 px-2.5 py-1.5 text-xs">
                    <span className="font-medium">{r.fragment}</span>{" "}
                    <span className="text-slate-500">{r.source_title}</span>
                    <span className="ml-1 rounded bg-slate-200 px-1 text-[10px]">{r.stage}</span>
                    {r.delegates_to_bylaw && (
                      <span className="ml-1 rounded bg-indigo-100 px-1 text-[10px] text-indigo-700">
                        делегира → подзаконски акт
                      </span>
                    )}
                    {r.structural_path && (
                      <span className="ml-1 text-[10px] text-slate-400">· {r.structural_path}</span>
                    )}
                    {r.repealed || r.valid_to ? (
                      <span className="ml-1 rounded bg-red-100 px-1 text-[10px] text-red-700">
                        престао да важи {r.valid_to}
                      </span>
                    ) : (
                      r.valid_from && (
                        <span className="ml-1 rounded bg-emerald-50 px-1 text-[10px] text-emerald-700">
                          на снази од {r.valid_from}
                          {r.applies_from && r.applies_from > r.valid_from
                            ? ` · у примени од ${r.applies_from}`
                            : ""}
                        </span>
                      )
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
