// Canonical human-facing names for the retrieval arms. The slugs (naive,
// advanced, graph, context-stuffing, agentic) stay machine-stable in the API,
// DB and charts; these labels are what users read.
export const ARM_LABELS: Record<string, string> = {
  naive: "Naive RAG",
  advanced: "Advanced RAG",
  graph: "Advanced + Graph",
  "context-stuffing": "Context-Stuffing",
  agentic: "Agentic",
};

export function armLabel(slug: string): string {
  return ARM_LABELS[slug] ?? slug;
}

// Compact names for cramped contexts (chart axis ticks) where the full
// "Context-Stuffing" overflows and collides. The full labels stay in cards,
// the per-arm grid and the chat picker.
export const ARM_SHORT_LABELS: Record<string, string> = {
  naive: "Naive",
  advanced: "Advanced",
  graph: "Adv+Graph",
  "context-stuffing": "Stuffing",
  agentic: "Agentic",
};

export function armShortLabel(slug: string): string {
  return ARM_SHORT_LABELS[slug] ?? armLabel(slug);
}
