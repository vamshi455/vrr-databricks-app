/**
 * Portfolio — "where do I look first?"
 *
 * Every pattern's latest VRR against its target, ranked by absolute drift. The colour
 * carries the one distinction that changes what you do next: amber means the number is
 * built on low-confidence PVT, so the *input* is suspect and no valve change may be
 * proposed on it — the guardrail from core/audit.py, made visible before you click in.
 */
import { useEffect, useState } from "react";
import {
  Bar, BarChart, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { api, type Overview } from "../api";
import {
  Badge, Card, DataTable, ErrorNote, Metric, Spinner, CHART_AXIS, CHART_TOOLTIP,
} from "../components/ui";

export function PortfolioView({ onPick }: { onPick: (id: string) => void }) {
  const [asset, setAsset] = useState<string>("all assets");
  const [data, setData] = useState<Overview | null>(null);
  const [audits, setAudits] = useState<Record<string, number>>({});
  const [dq, setDq] = useState<Awaited<ReturnType<typeof api.dataQuality>> | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const [showDq, setShowDq] = useState(false);

  useEffect(() => {
    setData(null);
    api.overview(asset === "all assets" ? undefined : asset).then(setData).catch(setErr);
  }, [asset]);

  useEffect(() => {
    api.inputAudit().then((a) => setAudits(a.by_verdict ?? {})).catch(() => {});
    api.dataQuality().then(setDq).catch(() => {});
  }, []);

  if (err) return <ErrorNote error={err} />;
  if (!data) return <Spinner label="scanning the portfolio…" />;

  const assets = [...new Set(data.patterns.map((p) => p.asset).filter(Boolean))] as string[];
  const top = data.patterns.slice(0, 20).map((p) => ({ ...p, name: p.pattern_name }));
  const suspect = data.patterns.filter((p) => p.any_extrapolated).length;

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-title font-semibold">Portfolio — where to look first</h1>
        <p className="mt-1 max-w-3xl text-label leading-relaxed text-content-muted">
          Every pattern's latest VRR against its target, ranked by absolute drift
          (<code className="font-mono">VRR_OVERVIEW</code> over{" "}
          <code className="font-mono">vrr_curated.pattern_vrr</code>). Amber = built on
          low-confidence PVT, so the number itself is suspect and cannot carry a valve
          recommendation.
        </p>
      </header>

      <div className="flex items-center gap-3">
        <select
          className="rounded border border-surface-border bg-surface-card px-2 py-1.5 text-body"
          value={asset}
          onChange={(e) => setAsset(e.target.value)}
        >
          {["all assets", ...assets].map((a) => <option key={a}>{a}</option>)}
        </select>
        {Object.keys(audits).length > 0 && (
          <p className="text-label text-content-muted">
            Input-audit verdicts:{" "}
            {Object.entries(audits).map(([k, v]) => (
              <span key={k} className="mr-2">
                <Badge tone={k === "REAL_SIGNAL" ? "green" : "amber"}>{k}</Badge> {v}
              </span>
            ))}
            — only REAL_SIGNAL periods may carry a recommendation.
          </p>
        )}
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <Metric label="Patterns" value={data.n_patterns} />
        <Metric label="Off target" value={data.off_target.length}
                tone={data.off_target.length ? "bad" : "good"} />
        <Metric label="Suspect inputs" value={suspect} tone={suspect ? "warn" : "good"} />
      </div>

      <Card title="Drift from target" sub="Top 20 by |VRR − target|. Click a bar to open that pattern.">
        <ResponsiveContainer width="100%" height={Math.max(260, top.length * 26)}>
          <BarChart data={top} layout="vertical" margin={{ left: 90, right: 16 }}>
            <XAxis type="number" tick={CHART_AXIS} />
            {/* interval={0} or Recharts drops every other label — and an unlabelled bar
                in a "where do I look first" chart is a bar you cannot act on. */}
            <YAxis type="category" dataKey="name" width={90} tick={CHART_AXIS}
                   interval={0} />
            <Tooltip
              formatter={(v: number) => v.toFixed(4)}
              labelFormatter={(l) => `${l}`}
              contentStyle={CHART_TOOLTIP}
            />
            <Bar dataKey="drift" onClick={(d: { id_pattern?: string }) => d.id_pattern && onPick(d.id_pattern)}>
              {top.map((p) => (
                <Cell key={p.id_pattern}
                      fill={p.any_extrapolated ? "#ff7f0e" : "#1f77b4"}
                      cursor="pointer" />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </Card>

      <Card title="All patterns" sub="The same rows as the chart, with the numbers behind them.">
        <DataTable
          rows={data.patterns.map((p) => ({
            pattern: p.pattern_name, asset: p.asset ?? "—", date: p.vrr_date,
            vrr: p.vrr, target: p.target_vrr, drift: p.drift,
            verdict: p.verdict ?? "—", completions: p.n_completions,
            suspect_pvt: p.any_extrapolated, rho: p.response_factor ?? null,
          }))}
        />
      </Card>

      <Card
        title={
          <button className="text-left" onClick={() => setShowDq((s) => !s)}>
            {showDq ? "▾" : "▸"} Ingestion data quality (DATA_QUALITY)
          </button>
        }
      >
        {!showDq ? (
          <p className="text-label text-content-muted">
            {dq?.ok ? `All ${dq.checks_run.length} checks clean.`
                    : `${dq?.n_findings ?? "—"} finding(s).`} Click to expand.
          </p>
        ) : dq?.ok ? (
          <p className="text-body text-signal">
            All {dq.checks_run.length} checks clean — allocation sums ≤ 1, no orphan
            volumes, every pattern has pressure, every allocated completion has PVT.
          </p>
        ) : (
          <div className="space-y-3">
            {Object.entries(dq?.findings ?? {}).map(([name, rows]) => (
              <div key={name}>
                <p className="mb-1 text-label font-medium text-suspect">
                  {name} — {rows.length} row(s)
                </p>
                <DataTable rows={rows} max={200} />
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
