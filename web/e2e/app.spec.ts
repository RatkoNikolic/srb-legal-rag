import { test, expect } from "@playwright/test";

// E2E against the live stack (vite :5173 → fastapi :8000 → Gemini).
// The chat test makes a real model call — generous timeouts are deliberate.

test("shell renders with all tabs", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "SRB Legal RAG" })).toBeVisible();
  for (const tab of ["Chat", "Runs", "Benchmark", "Corpus", "Dashboard"]) {
    await expect(page.getByRole("button", { name: tab })).toBeVisible();
  }
});

test("chat: Latin-script question through the naive arm gets a cited answer", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByLabel("Arm").selectOption("naive");
  await page
    .getByRole("textbox", { name: /отпремнина/ })
    .fill("Koliko dana godišnjeg odmora najmanje pripada zaposlenom?");
  await page.getByRole("button", { name: "Ask" }).click();

  // user bubble appears immediately, input clears (regression: empty-question race)
  await expect(page.getByText("Koliko dana godišnjeg odmora")).toBeVisible();
  await expect(page.getByRole("textbox", { name: /отпремнина/ })).toHaveValue("");

  // assistant bubble with citation chips (real LLM round-trip)
  const chip = page.locator("a", { hasText: "član" }).first();
  await expect(chip).toBeVisible({ timeout: 90_000 });
});

test("chat: a multi-turn conversation resolves a context-dependent follow-up", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByLabel("Arm").selectOption("advanced");
  const box = page.getByRole("textbox", { name: /отпремнина/ });

  // turn 1 — establishes the subject (annual leave; gold is in-statute, ZOR čl. 69)
  await box.fill("Колико најмање радних дана годишњег одмора следује запосленом?");
  await page.getByRole("button", { name: "Ask" }).click();
  await expect(page.locator("a", { hasText: "član" }).first()).toBeVisible({ timeout: 90_000 });

  // the conversation header + New chat control appear once there are turns
  await expect(page.getByText(/Conversation ·/)).toBeVisible();

  // turn 2 — a follow-up that only resolves against turn 1 (subject elided)
  await box.fill("А да ли може да се користи у деловима?");
  await page.getByRole("button", { name: "Ask" }).click();

  // the follow-up bubble must get its own citation chips (a real, in-context answer)
  const chips = page.locator("a", { hasText: "član" });
  await expect(async () => expect(await chips.count()).toBeGreaterThan(1)).toPass({
    timeout: 90_000,
  });

  // New chat clears the transcript
  await page.getByRole("button", { name: "New chat" }).click();
  await expect(page.getByText(/Conversation ·/)).toHaveCount(0);
});

test("chat: the agentic arm is offered as a retrieval option", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByLabel("Arm").locator("option", { hasText: "Agentic" })).toHaveCount(1);
});

test("benchmark tab: questions load across the three tiers", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Benchmark" }).click();
  // Questions sub-view is default: a heading + count per tier
  await expect(page.getByRole("heading", { name: "easy" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "hard" })).toBeVisible();
  await expect(page.getByText(/20 questions/).first()).toBeVisible();
  // Run sub-view exposes the launch control
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByRole("button", { name: "Run benchmark" })).toBeVisible();
});

test("runs: table populates and drill-down opens", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Runs" }).click();
  const firstRow = page.locator("tbody tr").first();
  await expect(firstRow).toBeVisible();
  await firstRow.click();
  await expect(page.getByText("Stage timings")).toBeVisible();
  await expect(page.getByText("Retrieved chunks")).toBeVisible();
});

test("dashboard: aggregate cards and charts render", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Dashboard" }).click();
  await expect(page.getByText("total runs")).toBeVisible();
  await expect(page.getByText("Latency by arm (ms)")).toBeVisible();
});

test("corpus tab: summary cards, the cluster map and the all-laws table render", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Corpus" }).click();
  // single all-laws table with the labor law
  await expect(page.getByText("ЗАКОН о раду").first()).toBeVisible();
  await expect(page.getByText(/All laws/).first()).toBeVisible();
  // summary cards + the cluster map (the cross-reference graph viz + the two bar
  // charts were removed — the graph view is not part of the shipped UI)
  await expect(page.getByText(/repealed članova/i).first()).toBeVisible();
  await expect(page.getByText(/Cluster map/i).first()).toBeVisible();
});
