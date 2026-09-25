/**
 * Lineage & audit — "do I believe this number?"
 *
 * The audit strip is the point: the recomputed VRR is rebuilt from raw daily rows
 * through core.physics IN THIS REQUEST, not read back from the curated table. A match
 * to ~1e-16 is evidence; a restatement of the stored value would be decoration.
 *
 * Below it, the derivation chain and the per-completion rows show HOW: root inputs →
 * the pattern pressure used → the PVT method that produced the FVFs → every derived
 * term. That is the same chain Unity Catalog registers as table-level lineage.
 */
import { useEffect, useState } from "react";
import { api, type AuditResult, type Lineage } from "../api";
import { LineageGraph } from "../components/LineageGraph";
import {
  Badge, Card, DataTable, ErrorNote, Metric, Spinner, StatusIcon, fmt,
} from "../components/ui";

interface Props { patternId: string; period: string }

export function LineageView({ patternId, period }: Props) {
  const [audit, setAudit] = useState<AuditResult | null>(null);
  const [lin, setLin] = useState<Lineage | null>(null);
  const [err, setErr] = useState<unknown>(null);

  useEffect(() => {
    if (!patternId || !period) return;
    setAudit(null); setLin(null);
    api.audit(patternId, period).then(setAudit).catch(setErr);
    api.lineage(patternId, period).then(setLin).catch(setErr);
  }, [patternId, period]);

  if (err) return <ErrorNote error={err} />;
  if (!lin || !audit) return <Spinner label="recomputing from raw rows…" />;

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-title font-semibold">
          How the {fmt.month(period)} VRR was computed
        </h1>
        <p className="mt-1 max-w-3xl text-label leading-relaxed text-content-muted">
          Recomputed independently in this request from{" "}
          <code className="font-mono">{audit.provenance?.recomputed_from?.join(", ")}</code>{" "}
          via <code className="font-mono">{audit.provenance?.code}</code> — not read back
          from the curated table.
        </p>
      </header>

      {audit.ok && (
        <>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <Metric label="Stored VRR" value={audit.stored.vrr.toFixed(6)}
                    foot={`run_id ${audit.stored.run_id ?? "—"}`} />
            <Metric label="Recomputed from raw" value={audit.recomputed.vrr.toFixed(6)}
                    foot={`${audit.n_raw_rows} daily rows`} />
            <Metric label="Difference" value={audit.difference.toExponential(2)}
                    tone={audit.matches ? "good" : "bad"}
                    foot={<span className="inline-flex items-center gap-1">
                      <StatusIcon kind={audit.matches ? "ok" : "warn"}
                                  className={audit.matches ? "text-signal" : "text-suspect"} />
                      {audit.matches ? "verified" : "mismatch"}
                    </span>} />
          </div>
          <p className="text-label text-content-muted">
            PVT lookup methods in this period:{" "}
            {audit.pvt_methods.map((m) => (
              <span key={m} className="mr-1">
                <Badge tone={m === "exact" || m === "interpolated" ? "green" : "amber"}>{m}</Badge>
              </span>
            ))}
            {audit.low_confidence_inputs && (
              <span className="text-suspect"> <StatusIcon kind="warn" /> low-confidence — no valve change may be
                proposed on this period</span>
            )}
          </p>
        </>
      )}

      <Card
        title="Derivation graph"
        sub="Four raw tables → core.physics → one row per completion → five reservoir terms → two sides → one number. Hover a term to trace what fed it; its formula lights up bottom-left. The ×n on the allocation edge is that period's volume-weighted mean contribution factor — FACTOR opens every formula below."
      >
        <LineageGraph lin={lin} audit={audit} />
      </Card>

      <details className="group">
        <summary className="cursor-pointer list-none rounded-lg border border-surface-border bg-surface-card
                            px-4 py-2.5 text-label text-content-secondary shadow-card hover:bg-surface-raised">
          <span className="group-open:hidden">Show</span>
          <span className="hidden group-open:inline">Hide</span>{" "}
          the rows behind the graph — per-completion inputs and the roll-up
        </summary>
        <div className="mt-3 space-y-4">
      <Card
        title="Per-completion contributions"
        sub="One row per completion for this month: root inputs, the pattern pressure used, the PVT method that produced the FVFs, and every derived term."
      >
        <DataTable rows={lin.completions} max={480} />
      </Card>

      <Card title="Roll-up" sub="What those rows sum to — and the VRR they imply.">
        <DataTable rows={[{
          ...lin.term_totals,
          prod_res_bbl: lin.recomputed_from_terms.prod_res_bbl,
          inj_res_bbl: lin.recomputed_from_terms.inj_res_bbl,
          vrr: lin.recomputed_from_terms.vrr,
        }]} />
        <p className="mt-2 text-label text-content-muted">
          Unity Catalog OSS registers these tables as the catalog-of-record, so this chain
          is also visible as table-level lineage (<code className="font-mono">make register</code>).
        </p>
      </Card>
        </div>
      </details>
    </div>
  );
}
