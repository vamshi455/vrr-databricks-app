/**
 * The workbench shell — header, a narrow filter rail, the active view, and the chatbot.
 *
 * Layout follows the analyst's workflow rather than the data model: Portfolio ("where do
 * I look?") → Report ("what happened here?") → Lineage ("do I believe the number?") →
 * Approvals ("who signs it off?").
 *
 * Two structural decisions worth keeping:
 *
 * - **Pattern and period live here**, not per view. Every view and the chatbot read the
 *   same selection, because a chart and an answer describing different months is the
 *   worst failure this app could have.
 * - **The chatbot floats.** It used to be a docked column eating a third of the width
 *   whether or not you were using it; now it overlays and the content is always full
 *   width.
 */
import { useEffect, useMemo, useState } from "react";
import { api, onUnauthorized, session, type Health, type Identity, type Pattern,
         type TrendRow } from "./api";
import { ChatBot } from "./components/ChatBot";
import { Header } from "./components/Header";
import { Login } from "./components/Login";
import { Banner, Spinner } from "./components/ui";
import { ApprovalView } from "./views/ApprovalView";
import { ArchitectureView } from "./views/ArchitectureView";
import { KnowledgeView } from "./views/KnowledgeView";
import { LineageView } from "./views/LineageView";
import { PortfolioView } from "./views/PortfolioView";
import { ReportView } from "./views/ReportView";

const VIEWS = [
  { id: "portfolio", label: "Portfolio", hint: "where to look first" },
  { id: "report", label: "Report", hint: "what moved, and what to do" },
  { id: "lineage", label: "Lineage & audit", hint: "do I believe this number" },
  { id: "approval", label: "Approvals", hint: "who signs it off" },
  { id: "knowledge", label: "Knowledge", hint: "what the agent may read" },
  // Last in the rail because it is the only view that is about the software rather than
  // the field — read once to understand the machine, not every morning.
  { id: "architecture", label: "Architecture", hint: "what runs when you ask" },
] as const;
type ViewId = (typeof VIEWS)[number]["id"];

export default function App() {
  const [view, setView] = useState<ViewId>("portfolio");
  const [patterns, setPatterns] = useState<Pattern[] | null>(null);
  const [patternId, setPatternId] = useState("");
  const [trend, setTrend] = useState<TrendRow[]>([]);
  const [period, setPeriod] = useState("");
  const [me, setMe] = useState<Identity | null>(null);
  const [loginOpen, setLoginOpen] = useState(false);
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.patterns()
      .then((p) => { setPatterns(p); if (p.length) setPatternId(p[0].pattern_id); })
      .catch((e) => setError(String(e.message ?? e)));
  }, []);

  // Health is polled, not fetched once: the trace badge has to notice MLflow coming back
  // (or going away) while you are working, since "always traced" is the claim being made.
  useEffect(() => {
    const tick = () => api.health().then(setHealth).catch(() => setHealth(null));
    tick();
    const id = setInterval(tick, 30_000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    if (!patternId) return;
    api.trend(patternId).then(({ rows }) => {
      setTrend(rows);
      setPeriod(rows.length ? rows[rows.length - 1].vrr_date : "");
    });
  }, [patternId]);

  useEffect(() => {
    if (session.token) {
      api.me().then((w) => setMe({ username: w.username, role: w.role }))
              .catch(() => session.clear());
    }
    onUnauthorized.handler = () => { setMe(null); setLoginOpen(true); };
    return () => { onUnauthorized.handler = null; };
  }, []);

  const selected = trend.find((r) => r.vrr_date === period);
  const vsTarget: "high" | "low" = (selected?.vrr ?? 1) >= 1 ? "high" : "low";
  const sorted = useMemo(
    () => [...(patterns ?? [])].sort((a, b) =>
      (a.pattern_name ?? "").localeCompare(b.pattern_name ?? "")), [patterns]);
  const patternName = sorted.find((p) => p.pattern_id === patternId)?.pattern_name ?? "";

  if (error) {
    return (
      <div className="mx-auto max-w-2xl p-8">
        <Banner tone="bad" title="Cannot reach the API">
          {error}. Start it with <code className="font-mono">make app</code>, and if the
          database is empty run <code className="font-mono">make seed</code>.
        </Banner>
      </div>
    );
  }
  if (!patterns) return <Spinner label="loading patterns…" />;
  if (!patterns.length) {
    return (
      <div className="mx-auto max-w-2xl p-8">
        <Banner tone="warn" title="No VRR data yet">
          Run <code className="font-mono">make seed</code> to generate the synthetic field.
        </Banner>
      </div>
    );
  }

  const shared = { patternId, period, trend };

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-surface-raised text-content-primary">
      <Header
        me={me}
        health={health}
        patternName={patternName}
        period={period}
        onSignIn={() => setLoginOpen(true)}
        onSignOut={() => { api.logout(); setMe(null); }}
      />

      <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
        {/* ------------------------------------------------------- filter rail */}
        <aside className="flex shrink-0 flex-col border-b border-surface-border bg-surface-card px-3 py-3
                          lg:w-56 lg:overflow-y-auto lg:border-b-0 lg:border-r lg:py-4">
          <nav className="flex gap-1 overflow-x-auto lg:block lg:space-y-0.5">
            {VIEWS.map((v) => (
              <button
                key={v.id}
                onClick={() => setView(v.id)}
                title={v.hint}
                className={`whitespace-nowrap rounded-md px-2.5 py-1.5 text-left text-body transition lg:block lg:w-full ${
                  view === v.id
                    ? "bg-brand-500 font-medium text-surface-base"
                    : "text-content-secondary hover:bg-surface-raised"
                }`}
              >
                {v.label}
                {/* A document sitting unapproved answers nothing, and no other screen
                    would say so — the badge is the only place that fact surfaces. */}
                {v.id === "knowledge" && (health?.knowledge.pending_review ?? 0) > 0 && (
                  <span className="ml-1.5 rounded-full bg-suspect-soft px-1.5 text-micro font-medium text-suspect">
                    {health?.knowledge.pending_review}
                  </span>
                )}
              </button>
            ))}
          </nav>

          <div className="mt-3 grid grid-cols-2 gap-3 border-t border-surface-divider pt-3 lg:mt-5 lg:grid-cols-1 lg:space-y-3 lg:pt-4">
            <Field label="Pattern">
              <select value={patternId} onChange={(e) => setPatternId(e.target.value)}
                      className={selectClass}>
                {sorted.map((p) => (
                  <option key={p.pattern_id} value={p.pattern_id}>{p.pattern_name}</option>
                ))}
              </select>
            </Field>

            <Field label="Period">
              <select value={period} onChange={(e) => setPeriod(e.target.value)}
                      className={selectClass}>
                {[...trend].reverse().map((r) => (
                  <option key={r.vrr_date} value={r.vrr_date}>
                    {new Date(r.vrr_date + "T00:00:00").toLocaleDateString(undefined,
                      { month: "short", year: "numeric" })}
                  </option>
                ))}
              </select>
            </Field>
          </div>

          <div className="mt-auto hidden space-y-1.5 border-t border-surface-divider pt-4 text-micro text-content-muted lg:block">
            <p>
              Postgres <code className="font-mono">{health?.postgres.host ?? "—"}</code>
            </p>
            <p>
              Knowledge {health?.knowledge.docs ?? 0} doc(s) ·{" "}
              {health?.knowledge.chunks ?? 0} chunks
            </p>
            <p className="leading-snug">
              Numbers come from deterministic tools; the model only phrases them, behind
              the faithfulness gate.
            </p>
          </div>
        </aside>

        {/* ------------------------------------------------------------- view */}
        <main className="min-w-0 flex-1 overflow-y-auto px-4 py-4 sm:px-6 sm:py-5">
          {view === "portfolio" && <PortfolioView onPick={setPatternId} />}
          {view === "report" && <ReportView {...shared} />}
          {view === "lineage" && <LineageView {...shared} />}
          {view === "approval" && <ApprovalView role={me?.role ?? ""} />}
          {view === "architecture" && <ArchitectureView />}
          {view === "knowledge" && (
            <KnowledgeView
              role={me?.role ?? ""}
              signedIn={!!me}
              onNeedSignIn={() => setLoginOpen(true)}
              // Approving embeds, so the sidebar's doc/chunk counts are stale the moment
              // it returns. Refresh health rather than wait out the 30s poll.
              onChanged={() => api.health().then(setHealth).catch(() => {})}
            />
          )}
        </main>
      </div>

      <ChatBot
        patternId={patternId}
        patternName={patternName}
        period={period}
        user={me?.username ?? ""}
        signedIn={!!me}
        onNeedSignIn={() => setLoginOpen(true)}
        vsTarget={vsTarget}
        llmUp={!!health?.llm.available}
      />

      {loginOpen && (
        <Login
          onSignedIn={(who) => { setMe(who); setLoginOpen(false); }}
          onCancel={() => setLoginOpen(false)}
        />
      )}
    </div>
  );
}

const selectClass =
  "w-full rounded-md border border-surface-border bg-surface-card px-2 py-1 text-label " +
  "text-content-primary focus:border-brand-500";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-micro font-medium uppercase tracking-wide text-content-muted">
        {label}
      </span>
      {children}
    </label>
  );
}
