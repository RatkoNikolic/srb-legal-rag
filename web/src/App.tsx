import { useState } from "react";
import Chat from "./components/Chat";
import Runs from "./components/Runs";
import Dashboard from "./components/Dashboard";
import Benchmark from "./components/Benchmark";
import Corpus from "./components/Corpus";

const TABS = ["Chat", "Runs", "Benchmark", "Corpus", "Dashboard"] as const;
type Tab = (typeof TABS)[number];

export default function App() {
  const [tab, setTab] = useState<Tab>("Chat");
  return (
    <div className="mx-auto flex h-screen max-w-6xl flex-col px-4">
      <header className="flex items-center justify-between border-b border-slate-200 py-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">SRB Legal RAG</h1>
          <p className="text-xs text-slate-500">
            Serbian legal assistant — retrieval arms, side by side
          </p>
        </div>
        <nav className="flex gap-1 rounded-lg bg-slate-100 p-1">
          {TABS.map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`rounded-md px-4 py-1.5 text-sm font-medium transition ${
                tab === t ? "bg-white shadow-sm" : "text-slate-500 hover:text-slate-800"
              }`}
            >
              {t}
            </button>
          ))}
        </nav>
      </header>
      <main className="min-h-0 flex-1 py-4">
        {tab === "Chat" && <Chat />}
        {tab === "Runs" && <Runs />}
        {tab === "Benchmark" && <Benchmark />}
        {tab === "Corpus" && <Corpus />}
        {tab === "Dashboard" && <Dashboard />}
      </main>
    </div>
  );
}
