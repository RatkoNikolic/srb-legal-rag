import { defineConfig } from "@playwright/test";

// E2E covers both ends of the live stack:
//  - api.spec.ts  → the FastAPI backend directly (request fixture, no browser)
//  - app.spec.ts  → the React UI driving the same backend through the browser
// Playwright boots both servers (uvicorn :8000 + vite :5173) and reuses any
// already running. The chat/agentic specs make real Gemini calls — hence the
// generous timeouts. E2E_API_PORT moves the backend off :8000 when another
// project holds it (parallel sessions).
const apiPort = process.env.E2E_API_PORT ?? "8000";
const apiUrl = `http://localhost:${apiPort}`;

export default defineConfig({
  testDir: "./e2e",
  timeout: 120_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: "http://localhost:5173",
    screenshot: "only-on-failure",
  },
  webServer: [
    {
      command: `.venv/bin/uvicorn lexsr.api:app --port ${apiPort}`,
      cwd: "..",
      url: `${apiUrl}/api/arms`,
      reuseExistingServer: true,
      timeout: 120_000,
    },
    {
      command: "npm run dev",
      url: "http://localhost:5173",
      reuseExistingServer: true,
      env: { VITE_API_PROXY: apiUrl },
    },
  ],
});
