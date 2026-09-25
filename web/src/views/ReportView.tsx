/**
 * Report — "what happened to this pattern, and what should we do?"
 *
 * Four things in the order an engineer asks them: is the number in band, is the INPUT
 * trustworthy (the audit banner), what moved it (exact LMDI attribution), and what
 * change — if any — the physics supports. The draft button lives here rather than in
 * the chat because the analysis above it *is* the evidence for the recommendation.
 */
import { useEffect, useState } from "react";
import {
  Bar, BarChart, Cell, CartesianGrid, Line, ComposedChart, ReferenceArea, ReferenceLine,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import {
  api, type AnalysisCase, type Decompose, type PatternContext, type TrendRow,
} from "../api";
import { Banner, Card, DataTable, ErrorNote, Metric, Spinner, StatusIcon, CHART_AXIS, CHART_TOOLTIP, fmt } from "../components/ui";
import { PatternDiagram } from "../components/PatternDiagram";

interface Props {
  patternId: string; period: string; trend: TrendRow[];
}

export function ReportView({ patternId, period, trend }: Props) {
  const [ctx, setCtx] = useState<PatternContext | null>(null);
  const [dec, setDec] = useState<Decompose | null>(null);
  const [analysis, setAnalysis] = useState<AnalysisCase | null>(null);
  const [audit, setAudit] = useState<{ verdict: string; summary: string } | null>(null);
  const [submitted, setSubmitted] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);

  useEffect(() => {
    if (!patternId) return;
    api.context(patternId).then(setCtx).catch(setErr);
  }, [patternId]);

  useEffect(() => {
    if (!patternId || !period) return;
    setDec(null); setAnalysis(null); setAudit(null); setSubmitted(null);

    const prior = trend.filter((r) => r.vrr_date < period).at(-1);
    if (prior) api.decompose(patternId, prior.vrr_date, period).then(setDec).catch(() => setDec(null));

    api.analysis(patternId, period).then(setAnalysis).catch(setErr);
    api.inputAudit(patternId)
      .then((a) => setAudit(a.audits?.find((x) => x.vrr_date === period) ?? null))
      .catch(() => {});
  }, [patternId, period, trend]);

  if (err) return <ErrorNote error={err} />;
  if (!ctx || !period) return <Spinner label="loading the period…" />;

  const sel = trend.find((r) => r.vrr_date === period);
  const prev = trend.filter((r) => r.vrr_date < period).at(-1);
  if (!sel) return <Spinner />;

  const band: [number, number] = [
    ctx.memory?.typical_low ?? 0.9, ctx.memory?.typical_high ?? 1.1,
  ];
  const inBand = sel.vrr >= band[0] && sel.vrr <= band[1];

  const values = trend.map((r) => r.vrr);
  const lo = Math.min(...values, band[0], ctx.target_vrr);
  const hi = Math.max(...values, band[1], ctx.target_vrr);
  const pad = Math.max((hi - lo) * 0.08, 0.02);
  const yDomain: [number, number] = [lo - pad, hi + pad];

  async function submit() {
    setBusy(true);
    try {
      const res = await api.submit(patternId, period);
      setSubmitted(`Queued ${res.action_id} — next approver: ${res.next_approver}.`);
    } catch (e) {
      setErr(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-title font-semibold">
          {ctx.pattern_name} — VRR {fmt.vrr(sel.vrr)} on {fmt.month(period)}
        </h1>
        <p className="mt-0.5 text-label text-content-muted">
          id_pattern <code className="font-mono">{ctx.pattern_id}</code>
          {ctx.asset && <> · asset {ctx.asset}</>}
        </p>
      </header>

      <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
        <Metric label="VRR" value={fmt.vrr(sel.vrr)}
                tone={inBand ? "good" : "bad"}
                foot={prev ? `${fmt.signed(sel.vrr - prev.vrr, 3)} MoM` : undefined} />
        <Metric label="Target" value={ctx.target_vrr.toFixed(2)}
                foot={`band ${band[0].toFixed(2)}–${band[1].toFixed(2)}`} />
        <Metric label="Injection (res bbl)" value={fmt.bbl(sel.inj_res_bbl)} />
        <Metric label="Production (res bbl)" value={fmt.bbl(sel.prod_res_bbl)} />
      </div>

      {audit ? (
        <Banner
          tone={audit.verdict === "REAL_SIGNAL" ? "good" : "warn"}
          title={<span className="inline-flex items-center gap-1.5">
            <StatusIcon kind={audit.verdict === "REAL_SIGNAL" ? "ok" : "blocked"} />
            Input audit: {audit.verdict}
          </span>}
        >
          {audit.summary}
          {audit.verdict !== "REAL_SIGNAL" && (
            <> — guardrail: no valve change may be proposed on this period; it routes to
              the data steward instead (core/audit.py).</>
          )}
        </Banner>
      ) : sel.any_extrapolated ? (
        <Banner tone="warn" title={<span className="inline-flex items-center gap-1.5">
          <StatusIcon kind="warn" /> Extrapolated PVT in this period
        </span>}>
          Inputs are suspect. Run <code className="font-mono">make audit</code> to record
          a verdict.
        </Banner>
      ) : null}

      {/* Before any chart: what this pattern physically IS. Wells, roles, who is shared
          with the pattern next door — the shape everything below is measured over. */}
      <PatternDiagram patternId={patternId} period={period} />

      <Card
        title="VRR history"
        sub="Green band = the pattern's normal band (vrr_agent.pattern_memory); dashed line = target; amber dots = periods built on low-confidence PVT; red line = the period under review."
      >
        <ResponsiveContainer width="100%" height={300}>
          <ComposedChart data={trend} margin={{ left: -10, right: 10 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#232e3d" />
            <XAxis dataKey="vrr_date" tick={CHART_AXIS}
                   tickFormatter={(d: string) => fmt.month(d)} minTickGap={28} />
            {/* The domain must always contain the band and the target, not just the
                data. On a pattern sitting at 1.38 an auto-scaled axis drops the 0.90–1.10
                band off-screen entirely — and "how far off target is it" is the only
                question this chart exists to answer. */}
            <YAxis domain={yDomain} tick={CHART_AXIS} width={56}
                   tickFormatter={(v: number) => v.toFixed(2)} />
            <Tooltip
              contentStyle={CHART_TOOLTIP}
              labelFormatter={(d) => fmt.month(String(d))}
              formatter={(v: number, n: string) =>
                [n === "vrr" ? v.toFixed(3) : Math.round(v).toLocaleString(), n]}
            />
            <ReferenceArea y1={band[0]} y2={band[1]} fill="#4fc47f" fillOpacity={0.10} />
            <ReferenceLine y={ctx.target_vrr} stroke="#4fc47f" strokeDasharray="6 4" />
            <ReferenceLine x={period} stroke="#f2777a" strokeDasharray="3 3" />
            <Line type="monotone" dataKey="vrr" stroke="#5aa9dd" strokeWidth={2}
                  dot={(props: { cx?: number; cy?: number; payload?: TrendRow; index?: number }) =>
                    props.payload?.any_extrapolated
                      ? <circle key={props.index} cx={props.cx} cy={props.cy} r={4} fill="#e0a83a" />
                      : <circle key={props.index} cx={props.cx} cy={props.cy} r={2} fill="#5aa9dd" />} />
          </ComposedChart>
        </ResponsiveContainer>
      </Card>

      <Card
        title="ΔVRR attribution"
        sub="Exact log-mean (LMDI) split — the contributions sum to ΔVRR, to machine precision."
      >
        {!prev ? (
          <p className="text-body text-content-muted">No prior period to attribute against.</p>
        ) : !dec ? (
          <Spinner label="decomposing…" />
        ) : !dec.ok ? (
          <p className="text-body text-content-muted">{dec.reason ?? "no attribution"}</p>
        ) : (
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 xl:grid-cols-5">
            <div className="col-span-2 text-body">
              <p className="font-medium">
                {fmt.month(prev.vrr_date)} → {fmt.month(period)}
              </p>
              <p className="mt-1 tabular-nums text-content-secondary">
                VRR {fmt.vrr(dec.vrr_a)} → {fmt.vrr(dec.vrr_b)}{" "}
                (<strong>{fmt.signed(dec.d_vrr, 3)}</strong>)
              </p>
              <p className="mt-1 text-label text-content-muted">
                Injection side {fmt.signed(dec.side_contributions.injection)} · production
                side {fmt.signed(dec.side_contributions.production)}
              </p>
              <ul className="mt-3 space-y-1 text-label tabular-nums">
                {dec.drivers.map((d) => (
                  <li key={d.term} className="flex justify-between gap-3">
                    <span className="text-content-secondary">{d.label}</span>
                    <span className={d.contribution >= 0 ? "text-offtarget" : "text-signal"}>
                      {fmt.signed(d.contribution)} ({fmt.pct(d.share)})
                    </span>
                  </li>
                ))}
              </ul>
            </div>
            <div className="col-span-3">
              <ResponsiveContainer width="100%" height={200}>
                <BarChart data={dec.drivers} layout="vertical" margin={{ left: 30, right: 12 }}>
                  <XAxis type="number" tick={CHART_AXIS}
                         tickFormatter={(v: number) => v.toFixed(3)} />
                  <YAxis type="category" dataKey="label" width={110} tick={CHART_AXIS} />
                  <Tooltip formatter={(v: number) => fmt.signed(v)} contentStyle={CHART_TOOLTIP} />
                  <Bar dataKey="contribution">
                    {dec.drivers.map((d) => (
                      <Cell key={d.term} fill={d.contribution >= 0 ? "#d62728" : "#2ca02c"} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>
        )}
      </Card>

      <Card
        title="Draft a valve change for approval"
        sub="The analysis above is the evidence for this recommendation — magnitude is physics-computed and clamped by vrr_agent.safety_limits."
      >
        {!analysis ? (
          <Spinner label="running verify → attribute → classify → propose…" />
        ) : !analysis.ok ? (
          <p className="text-body text-content-muted">{analysis.reason}</p>
        ) : (
          <>
            <pre className="whitespace-pre-wrap rounded bg-surface-raised p-3 text-label leading-relaxed text-content-primary">
              {analysis.narrative}
            </pre>
            {analysis.draft ? (
              <div className="mt-3 space-y-2">
                <button
                  onClick={submit}
                  disabled={busy || !!submitted}
                  className="rounded bg-brand-500 px-3 py-1.5 text-body font-medium text-surface-base disabled:opacity-40"
                >
                  {busy ? "submitting…" : "📤 Submit to approval queue (draft → analyst)"}
                </button>
                {analysis.draft.action_type === "investigate_inputs" && (
                  <p className="text-label text-suspect">
                    This draft is an <em>investigate inputs</em> item (suspect PVT), not a
                    valve change — it still routes through the same approval chain.
                  </p>
                )}
                {submitted && <Banner tone="good" title={submitted} />}
              </div>
            ) : (
              <p className="mt-3 text-body text-content-muted">
                No anomaly fired for this period — nothing to draft.
              </p>
            )}
          </>
        )}
      </Card>

      <Card title="Monthly rows" sub="vrr_curated.pattern_vrr, grain = monthly">
        <DataTable rows={trend as unknown as Record<string, unknown>[]} />
      </Card>
    </div>
  );
}
