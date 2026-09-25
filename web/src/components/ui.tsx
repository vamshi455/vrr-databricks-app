/**
 * Small shared primitives. Deliberately few: this is a workbench for one engineer
 * reading numbers, not a design system.
 *
 * The one piece of real logic here is `provenanceLine` — where an answer came from, in
 * analyst English. It is the single most-read line in the app, so it lives in one place
 * with its own test rather than being re-derived per view.
 */
import type { ChatMeta } from "../api";
import type { ReactNode } from "react";

export function Card({ title, sub, children, className = "" }: {
  title?: ReactNode; sub?: ReactNode; children: ReactNode; className?: string;
}) {
  return (
    <section className={`rounded-lg border border-surface-border bg-surface-card shadow-card ${className}`}>
      {(title || sub) && (
        <header className="border-b border-surface-divider px-4 py-3">
          {title && <h2 className="text-sub font-semibold text-content-primary">{title}</h2>}
          {sub && <p className="mt-0.5 text-label leading-relaxed text-content-muted">{sub}</p>}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

export function Metric({ label, value, foot, tone = "plain" }: {
  label: string; value: ReactNode; foot?: ReactNode;
  tone?: "plain" | "good" | "warn" | "bad";
}) {
  const toneClass = {
    plain: "text-content-primary", good: "text-signal", warn: "text-suspect", bad: "text-offtarget",
  }[tone];
  return (
    <div className="rounded-lg border border-surface-border bg-surface-card px-4 py-3 shadow-card">
      <div className="text-micro font-medium uppercase tracking-wide text-content-muted">{label}</div>
      <div className={`mt-1 text-display font-semibold tabular-nums ${toneClass}`}>{value}</div>
      {foot && <div className="mt-0.5 text-micro text-content-muted">{foot}</div>}
    </div>
  );
}

export function Banner({ tone, title, children }: {
  tone: "good" | "warn" | "bad" | "info"; title: ReactNode; children?: ReactNode;
}) {
  const styles = {
    good: "border-signal/40 bg-signal-soft text-signal",
    warn: "border-suspect/40 bg-suspect-soft text-suspect",
    bad: "border-offtarget/40 bg-offtarget-soft text-offtarget",
    info: "border-surface-border bg-surface-raised text-content-secondary",
  }[tone];
  return (
    <div className={`rounded-lg border px-4 py-3 text-body ${styles}`}>
      <div className="font-medium">{title}</div>
      {children && <div className="mt-1 text-label leading-relaxed opacity-90">{children}</div>}
    </div>
  );
}

export function Badge({ children, tone = "slate" }: {
  children: ReactNode; tone?: "slate" | "green" | "amber" | "red";
}) {
  const styles = {
    slate: "bg-surface-raised text-content-secondary", green: "bg-signal-soft text-signal",
    amber: "bg-suspect-soft text-suspect", red: "bg-offtarget-soft text-offtarget",
  }[tone];
  return (
    <span className={`inline-flex rounded px-1.5 py-0.5 text-micro font-medium ${styles}`}>
      {children}
    </span>
  );
}

/**
 * Status glyphs, as SVG rather than as emoji.
 *
 * These were ✅ / ⚠️ / 🛑 / ⚪. Three problems with that: a screen reader announces
 * "white heavy check mark" in the middle of a sentence about provenance, the glyphs
 * render at a different size and baseline in every OS font, and they cannot take the
 * semantic colour of the thing they sit next to. Inline SVG on `currentColor` fixes all
 * three and stays a single character wide in the type scale.
 */
export function StatusIcon({ kind, className = "" }: {
  kind: "ok" | "warn" | "blocked" | "idle"; className?: string;
}) {
  const d = {
    ok: "M10 18a8 8 0 100-16 8 8 0 000 16zm3.857-9.809a.75.75 0 00-1.214-.882l-3.483 4.79"
      + "-1.88-1.88a.75.75 0 10-1.06 1.061l2.5 2.5a.75.75 0 001.137-.089l4-5.5z",
    warn: "M8.485 2.495c.673-1.167 2.357-1.167 3.03 0l6.28 10.875c.673 1.167-.17 2.625"
      + "-1.516 2.625H3.72c-1.347 0-2.189-1.458-1.515-2.625L8.485 2.495zM10 5a.75.75 0"
      + " 01.75.75v3.5a.75.75 0 01-1.5 0v-3.5A.75.75 0 0110 5zm0 9a1 1 0 100-2 1 1 0 000 2z",
    blocked: "M10 18a8 8 0 100-16 8 8 0 000 16zM8.28 7.22a.75.75 0 00-1.06 1.06L8.94 10"
      + "l-1.72 1.72a.75.75 0 101.06 1.06L10 11.06l1.72 1.72a.75.75 0 101.06-1.06L11.06 10"
      + "l1.72-1.72a.75.75 0 00-1.06-1.06L10 8.94 8.28 7.22z",
    idle: "M10 5.5a4.5 4.5 0 100 9 4.5 4.5 0 000-9z",
  }[kind];
  return (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"
         className={`inline-block h-[1.05em] w-[1.05em] shrink-0 align-[-0.15em] ${className}`}>
      <path d={d} />
    </svg>
  );
}

export function Spinner({ label = "loading…" }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 py-8 text-body text-content-muted">
      <span className="h-3 w-3 animate-spin rounded-full border-2 border-surface-border border-t-content-secondary" />
      {label}
    </div>
  );
}

export function ErrorNote({ error }: { error: unknown }) {
  const msg = error instanceof Error ? error.message : String(error);
  return (
    <Banner tone="bad" title="Request failed">
      {msg}. Is the API up (<code className="font-mono">make api</code>) and seeded
      (<code className="font-mono">make seed</code>)?
    </Banner>
  );
}

/** A scrollable data table. Keys of the first row become the columns. */
export function DataTable({ rows, max = 400 }: {
  rows: Record<string, unknown>[]; max?: number;
}) {
  if (!rows.length) return <p className="text-body text-content-muted">No rows.</p>;
  const cols = Object.keys(rows[0]);
  return (
    <div className="overflow-auto rounded border border-surface-border" style={{ maxHeight: max }}>
      <table className="w-full text-label">
        <thead className="sticky top-0 bg-surface-raised text-left text-content-secondary">
          <tr>
            {cols.map((c) => <th key={c} className="whitespace-nowrap px-2 py-1.5 font-medium">{c}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-t border-surface-divider hover:bg-surface-raised">
              {cols.map((c) => (
                <td key={c} className="whitespace-nowrap px-2 py-1 tabular-nums text-content-secondary">
                  {fmtCell(r[c])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function fmtCell(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "number") return Number.isInteger(v) ? v.toLocaleString() : v.toFixed(4);
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

// ------------------------------------------------------------ provenance ----
const INTENT_LABEL: Record<string, string> = {
  explain: "Computed from your tables",
  recommend: "Computed from your tables",
  audit: "Input audit",
  lineage: "Lineage",
  completions: "Completion list",
  portfolio: "Portfolio scan",
  data_quality: "Data-quality check",
  knowledge: "From your documents",
  general: "General knowledge — not your data",
  help: "How to use this workbench",
  status: "Runtime status — live probes",
};

/**
 * One caption saying who produced an answer and whether the gate cleared it.
 *
 * Ported from the Streamlit drawer, where the raw version (`intent: general · LLM ·
 * gate n/a (no field figures claimed)`) read like a debug log. The information is the
 * trust argument, so it stays visible — but in English.
 */
export function provenanceLine(
  intent: string | undefined, meta: ChatMeta = {},
): { text: string; tone: "ok" | "warn" | "idle" | "none" } {
  const source = INTENT_LABEL[intent ?? ""] ?? (intent ?? "answer").replace(/_/g, " ");
  const gate = meta.gate ?? "";
  const model = meta.model ?? "LLM";
  const tools = meta.tools_called?.length ? ` · tools: ${meta.tools_called.join(", ")}` : "";
  // The icon is returned rather than baked into the string so the caller can colour it
  // semantically; an emoji in the text could do neither.
  const line = (phrasing: string, tone: "ok" | "warn" | "idle" | "none") =>
    ({ text: `${source} · ${phrasing}${tools}`, tone });
  // Two very different reasons an answer has no LLM in it, and conflating them reads as
  // a broken model when nothing is wrong:
  //   gate "skipped (no local LLM running)"  → Ollama really is down
  //   no gate at all                         → this INTENT never uses a model. Lineage,
  //     portfolio, completions and data-quality answers are assembled from tool output
  //     by design; there is no prose for a model to write.
  if (!meta.llm && gate.startsWith("skipped"))
    return line("no model running — computed answer", "idle");
  if (!meta.llm) return line("deterministic answer — no model needed", "ok");
  if (gate.startsWith("REJECTED"))
    return line(`${model} phrasing rejected — computed wording shown`, "warn");
  if (gate.includes("abstained")) return line("abstained — nothing above the retrieval floor", "none");
  if (gate.includes("repair")) return line(`${model} phrasing · gate passed after one repair`, "ok");
  if (gate.startsWith("n/a")) return line(`${model} · no field figures to verify`, "none");
  return line(`${model} phrasing · gate passed`, "ok");
}

/** A gate violation as a sentence, not a dict repr. */
export function violationLine(v: { kind?: string; term?: string; detail?: string }): string {
  return v.detail ?? `${v.kind ?? "violation"} on ${v.term ?? "?"}`;
}

/**
 * Text sizes for SVG chart furniture, in px, matching the Tailwind scale by name —
 * `tick` is `micro` (11) and `tooltip` is `label` (12). Recharts takes numbers, not
 * classes, so without this the scale silently forks the moment someone types 10.
 */
export const chartType = { tick: 11, tooltip: 12 } as const;

/** Recharts renders its tooltip as an inline-styled white box, so the theme has to be
 *  handed to it explicitly — otherwise a white card pops up over the dark chart. */
export const CHART_TOOLTIP = {
  fontSize: chartType.tooltip,
  background: "#222d3c",
  border: "1px solid #2f3b4d",
  borderRadius: 6,
  color: "#e8eef5",
} as const;

/** Axis ticks and labels on the dark ground. */
export const CHART_AXIS = { fontSize: chartType.tick, fill: "#8b9db0" } as const;

export const fmt = {
  vrr: (v: number | null | undefined) => (v == null ? "—" : v.toFixed(3)),
  bbl: (v: number | null | undefined) => (v == null ? "—" : Math.round(v).toLocaleString()),
  signed: (v: number, dp = 4) => `${v >= 0 ? "+" : ""}${v.toFixed(dp)}`,
  pct: (v: number) => `${(v * 100).toFixed(1)}%`,
  month: (iso: string) =>
    new Date(iso + (iso.length === 10 ? "T00:00:00" : "")).toLocaleDateString(undefined, {
      month: "short", year: "numeric",
    }),
};
