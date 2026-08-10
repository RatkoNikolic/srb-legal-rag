import { test, expect } from "@playwright/test";

// Backend E2E — drives the FastAPI server directly (no browser). Validates the
// API contract the UI depends on plus the behaviours the benchmark hinges on:
// the five arms, validation, a real cited round-trip, the agentic tool trace,
// and multi-turn conversation continuity (query condensation). Structural
// checks are cheap; the four asks at the end make real Gemini calls.

const API = `http://localhost:${process.env.E2E_API_PORT ?? "8000"}`;

test("arms listing includes all five arms", async ({ request }) => {
  const res = await request.get(`${API}/api/arms`);
  expect(res.ok()).toBeTruthy();
  expect(await res.json()).toEqual(["advanced", "agentic", "context-stuffing", "graph", "naive"]);
});

test("corpus endpoint exposes the full ten-cluster corpus", async ({ request }) => {
  const corpus = await (await request.get(`${API}/api/corpus`)).json();
  expect(corpus.length).toBeGreaterThanOrEqual(29);
  const clusters = new Set(corpus.map((l: { cluster: string }) => l.cluster));
  // 10 numbered thematic clusters + the procedural/constitutional backbone
  expect(clusters.size).toBeGreaterThanOrEqual(10);
});

test("validation: empty question → 422, unknown arm → 404", async ({ request }) => {
  expect((await request.post(`${API}/api/ask`, { data: { question: "  ", arm: "naive" } })).status())
    .toBe(422);
  expect(
    (await request.post(`${API}/api/ask`, { data: { question: "Питање?", arm: "quantum" } })).status(),
  ).toBe(404);
});

test("ask: naive arm returns a cited, non-abstained answer", async ({ request }) => {
  const res = await request.post(`${API}/api/ask`, {
    data: { question: "Колико дана годишњег одмора најмање има запослени?", arm: "naive" },
  });
  const run = await res.json();
  expect(run.abstained).toBeFalsy();
  expect(run.citations.length).toBeGreaterThan(0);
  expect(run.citations[0].url).toContain("eli");
});

test("ask: agentic arm answers and logs a tool trace", async ({ request }) => {
  // The ReAct loop is up to MAX_STEPS=10 *sequential* LLM round-trips; under
  // parallel-worker LLM traffic + provider backoff it can legitimately exceed
  // the global 120s budget (observed: 39s solo, >120s in a busy run).
  test.slow();
  const res = await request.post(`${API}/api/ask`, {
    data: { question: "Шта прописује члан 69 Закона о раду о годишњем одмору?", arm: "agentic" },
  });
  const run = await res.json();
  expect(Array.isArray(run.extras.tool_trace)).toBeTruthy();
  expect(run.extras.tool_trace.length).toBeGreaterThan(0);
  expect(run.extras.steps_used).toBeGreaterThan(0);
  expect(run.extras.tools_called).toContain("finish");
});

test("multi-turn: a follow-up is condensed and answered in context", async ({ request }) => {
  const conversation_id = "e2e-continuity";
  // Both golds live in the statute text (ZOR čl. 69 / čl. 73) — a question whose
  // literal answer is outside the corpus (a dinar amount set by decision) is a
  // legitimate abstention for a literal-minded generator and makes the test flaky.
  const Q1 = "Колико најмање радних дана годишњег одмора следује запосленом?";
  const first = await (
    await request.post(`${API}/api/ask`, {
      data: { question: Q1, arm: "advanced", conversation_id },
    })
  ).json();
  expect(first.abstained).toBeFalsy();

  const followup = await (
    await request.post(`${API}/api/ask`, {
      data: {
        question: "А да ли може да се користи у деловима?",
        arm: "advanced",
        conversation_id,
        history: [
          { role: "user", content: Q1 },
          { role: "assistant", content: first.answer_text },
        ],
      },
    })
  ).json();

  // the pronoun „је" was resolved into a standalone retrieval query…
  expect(typeof followup.extras.condensed_query).toBe("string");
  expect(followup.extras.condensed_query.toLowerCase()).not.toBe("а да ли може да се користи у деловима?");
  // …and the follow-up got a real cited answer rather than abstaining
  expect(followup.abstained).toBeFalsy();
  expect(followup.citations.length).toBeGreaterThan(0);
});

test("benchmark golden exposes 60 questions across three tiers", async ({ request }) => {
  const golden = await (await request.get(`${API}/api/golden`)).json();
  expect(golden.length).toBe(60);
  expect([...new Set(golden.map((g: { category: string }) => g.category))].sort()).toEqual([
    "easy",
    "hard",
    "medium",
  ]);
  expect(golden[0].gold_citations.length).toBeGreaterThan(0);
});

test("golden sets are discoverable and scope the item listing", async ({ request }) => {
  const sets = await (await request.get(`${API}/api/golden/sets`)).json();
  const byName = Object.fromEntries(sets.map((s: { name: string }) => [s.name, s]));
  // the live default (60-item situational set) + the 30-item subset
  expect(byName["golden-situational-60"].is_default).toBeTruthy();
  expect(byName["golden-situational-60"].items).toBe(60);
  expect(byName["golden-situational-30"].items).toBe(30);
  // ?golden=<name> scopes the item listing to that set
  const sub = await (await request.get(`${API}/api/golden?golden=golden-situational-30`)).json();
  expect(sub.length).toBe(30);
  // a bad set name on a run is rejected
  expect(
    (await request.post(`${API}/api/benchmark/run`, { data: { arms: ["naive"], golden: ".." } })).status(),
  ).toBe(422);
});

test("abstention always quotes its reason", async ({ request }) => {
  const res = await request.post(`${API}/api/ask`, {
    data: { question: "Која је казна за кривично дело пореске утаје?", arm: "advanced" },
  });
  const run = await res.json();
  expect(run.abstained).toBeTruthy();
  expect(run.abstain_reason).toBeTruthy(); // the reason is always recorded
  // The visible answer is never blank: either the model's own abstention prose
  // or the backend's "Уздржавам се… Разлог: …" fallback (injected only when the
  // model returned empty text — so asserting the literal "Разлог" would flake).
  expect(run.answer_text.trim()).not.toBe("");
  expect(run.citations.length).toBe(0); // an abstention cites nothing
});

test("telemetry: runs list, drill-down and stats are consistent", async ({ request }) => {
  const runs = await (await request.get(`${API}/api/runs`)).json();
  expect(runs.length).toBeGreaterThan(0);
  const detail = await (await request.get(`${API}/api/runs/${runs[0].id}`)).json();
  expect(detail.retrieved).toBeDefined();
  expect(detail.timings.total_ms).toBeGreaterThan(0);
  const stats = await (await request.get(`${API}/api/stats`)).json();
  expect(stats.total_runs).toBeGreaterThanOrEqual(runs.length);
});
