/**
 * The derivation of one monthly VRR, as a graph rather than as prose.
 *
 * Lineage IS a graph — four raw tables feed two physics functions, which feed one row
 * per completion, which sum into five reservoir terms, which fold into two sides, which
 * divide into one number. Writing that as a sentence with arrows in it ("raw_volumes →
 * pressure → pvt → …") asked the reader to hold the shape in their head; drawing it
 * hands the shape over directly, and the value on every node says what actually flowed.
 *
 * This component does not compute. Every figure is lifted from the `/lineage` and
 * `/audit` payloads, both of which were produced by `core.physics` — the same code the
 * recompute strip above the graph ran.
 *
 * The formulas sit small in the bottom-left corner rather than in a table of their own:
 * they are the *rule* behind the arrows, wanted at a glance and read maybe once. Hover a
 * term and its formula lights up, which is the only reason a corner panel beats a
 * tooltip here — you can compare a value and its rule at the same time.
 */
import { useState } from "react";
import type { AuditResult, Lineage } from "../api";
import { fmt } from "./ui";

// The graph is drawn 1:1 with CSS pixels at desktop width, so every fontSize below is
// also its rendered size — which is why they follow the app's type scale (11 = micro,
// 12 = label) instead of the 9-10px the first cut used. Below `min-w`, the wrapper
// scrolls rather than shrinking the text under the legibility floor.
const W = 1180, H = 508;

/** One column of the DAG. x is the left edge; every node is NODE_W wide. */
const NODE_W = 176, NODE_H = 48;
const COL = { raw: 8, physics: 246, contrib: 484, term: 700, side: 878, vrr: 1046 };

const C = {
  raw: { fill: "#222d3c", stroke: "#3b4a5f", text: "#c8d6e4" },
  physics: { fill: "#241d38", stroke: "#a98bdc", text: "#cbb8f0" },
  contrib: { fill: "#16283a", stroke: "#3d87b8", text: "#a8d2ef" },
  prod: { fill: "#10281c", stroke: "#4fc47f", text: "#8fe0b1" },
  inj: { fill: "#16283a", stroke: "#5aa9dd", text: "#a8d2ef" },
} as const;

/** Kept out of `C` so `C[kind]` stays a union of node palettes, not palettes-or-string. */
const EDGE = "#6b7c92";

interface Node {
  id: string; x: number; y: number; label: string; value: string;
  kind: keyof typeof C; formula?: string; w?: number;
}

export function LineageGraph({ lin, audit }: { lin: Lineage; audit: AuditResult }) {
  const [hot, setHot] = useState<string | null>(null);

  const t = lin.term_totals;
  const r = lin.recomputed_from_terms;
  const f = lin.formulas;
  const pvt = audit.pvt_methods?.join(", ") || "—";
  const a = lin.allocation ?? { n: 0, min: null, max: null, weighted_mean: null };

  // Four raw tables in, one number out. Positions are laid out by hand rather than by a
  // layout engine because the shape is fixed — this is the VRR derivation, not an
  // arbitrary DAG, and a hand layout reads better than anything dagre would produce.
  const nodes: Node[] = [
    { id: "vol", x: COL.raw, y: 26, kind: "raw",
      label: "production_volumes_daily", value: `${audit.n_raw_rows} daily rows` },
    { id: "alloc", x: COL.raw, y: 118, kind: "raw",
      label: "pattern_contribution_factor",
      // FACTOR opens every formula in the corner panel, so naming the table was not
      // enough — the graph has to say what the numbers actually were.
      value: a.n
        ? `${a.n} factors · ${a.min?.toFixed(2)}–${a.max?.toFixed(2)}`
        : "windowed by effect_date" },
    { id: "pres", x: COL.raw, y: 210, kind: "raw",
      label: "pattern_pressure", value: "holds to next reading" },
    { id: "pvtsrc", x: COL.raw, y: 302, kind: "raw",
      label: "completion_pvt_characteristics", value: "by pressure + test_date" },

    { id: "pvt", x: COL.physics, y: 254, kind: "physics",
      label: "physics.pvt_lookup", value: pvt },
    { id: "contribfn", x: COL.physics, y: 118, kind: "physics",
      label: "physics.completion_contribution", value: "FACTOR · VOL · FVF" },

    { id: "contrib", x: COL.contrib, y: 180, kind: "contrib",
      label: "completion_contrib", value: `${lin.completions.length} completions` },

    { id: "oil", x: COL.term, y: 22, w: 150, kind: "prod",
      label: "oil_res", value: fmt.bbl(t.oil_res), formula: f.res_oil_volume_bbl },
    { id: "water", x: COL.term, y: 82, w: 150, kind: "prod",
      label: "water_res", value: fmt.bbl(t.water_res), formula: f.res_water_volume_bbl },
    { id: "gas", x: COL.term, y: 142, w: 150, kind: "prod",
      label: "free_gas_res", value: fmt.bbl(t.free_gas_res), formula: f.res_free_gas_volume_bbl },
    { id: "winj", x: COL.term, y: 250, w: 150, kind: "inj",
      label: "water_inj_res", value: fmt.bbl(t.water_inj_res), formula: f.res_water_inj_volume_bbl },
    { id: "ginj", x: COL.term, y: 310, w: 150, kind: "inj",
      label: "gas_inj_res", value: fmt.bbl(t.gas_inj_res), formula: f.res_gas_inj_volume_bbl },

    { id: "prod", x: COL.side, y: 82, kind: "prod",
      label: "production (res bbl)", value: fmt.bbl(r.prod_res_bbl), w: 152 },
    { id: "injs", x: COL.side, y: 280, kind: "inj",
      label: "injection (res bbl)", value: fmt.bbl(r.inj_res_bbl), w: 152 },
  ];

  const byId = Object.fromEntries(nodes.map((n) => [n.id, n]));
  const edges: [string, string][] = [
    ["vol", "contribfn"], ["alloc", "contribfn"], ["pres", "pvt"], ["pvtsrc", "pvt"],
    ["pvt", "contribfn"], ["contribfn", "contrib"],
    ["contrib", "oil"], ["contrib", "water"], ["contrib", "gas"],
    ["contrib", "winj"], ["contrib", "ginj"],
    ["oil", "prod"], ["water", "prod"], ["gas", "prod"],
    ["winj", "injs"], ["ginj", "injs"],
  ];

  // A node is dimmed unless it is on the path the pointer is asking about.
  const upstream = (id: string, seen = new Set<string>()): Set<string> => {
    seen.add(id);
    for (const [a, b] of edges) if (b === id && !seen.has(a)) upstream(a, seen);
    return seen;
  };
  const lit = hot ? upstream(hot) : null;
  const on = (id: string) => !lit || lit.has(id);

  const vrrX = COL.vrr, vrrY = 173;

  return (
    <div className="overflow-x-auto">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full min-w-[860px]" role="img"
           aria-label="Derivation graph: raw tables through core.physics to the monthly VRR">
        <defs>
          <marker id="lin-arrow" viewBox="0 0 10 10" refX="9" refY="5"
                  markerWidth="5" markerHeight="5" orient="auto">
            <path d="M0,2 L9,5 L0,8 z" fill={EDGE} />
          </marker>
        </defs>

        {/* ---- column captions, so the layering is nameable ---- */}
        {([["raw", "RAW", COL.raw], ["compute", "core.physics", COL.physics],
           ["curated", "CURATED", COL.contrib], ["terms", "RESERVOIR TERMS", COL.term],
           ["sides", "SIDES", COL.side], ["out", "RESULT", COL.vrr]] as const)
          .map(([k, label, x]) => (
            <text key={k} x={x} y={12} className="fill-content-muted"
                  style={{ fontSize: 11, letterSpacing: 0.7 }}>{label}</text>
          ))}

        {edges.map(([a, b]) => {
          const s = byId[a], d = byId[b];
          // Just "×0.65": the gap between the allocation table and the physics box is
          // 62px, and anything longer is clipped by the node it points at. The words go
          // in the tooltip and the card subtitle instead.
          const tag = a === "alloc" && lin.allocation?.weighted_mean != null
            ? `×${lin.allocation.weighted_mean.toFixed(2)}` : null;
          const x1 = s.x + (s.w ?? NODE_W), y1 = s.y + NODE_H / 2;
          const x2 = d.x, y2 = d.y + NODE_H / 2;
          const mid = (x1 + x2) / 2;
          return (
            <g key={`${a}-${b}`} opacity={on(a) && on(b) ? 1 : 0.16}>
              <path d={`M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}`}
                    fill="none" stroke={EDGE} strokeWidth={1.2} strokeOpacity={0.75}
                    markerEnd="url(#lin-arrow)" />
              {tag && (
                <text x={(x1 + x2) / 2} y={y1 - 6} textAnchor="middle"
                      className="fill-content-muted tabular-nums" style={{ fontSize: 11 }}>
                  <title>Volume-weighted mean contribution factor for this period</title>
                  {tag}
                </text>
              )}
            </g>
          );
        })}

        {/* sides → VRR, drawn separately because the division is the point */}
        {(["prod", "injs"] as const).map((id) => {
          const s = byId[id];
          const x1 = s.x + (s.w ?? NODE_W), y1 = s.y + NODE_H / 2;
          return (
            <path key={id} d={`M${x1},${y1} C${x1 + 40},${y1} ${vrrX - 40},${vrrY + 30} ${vrrX},${vrrY + 30}`}
                  fill="none" stroke={EDGE} strokeWidth={1.4}
                  strokeOpacity={on(id) ? 0.75 : 0.12} markerEnd="url(#lin-arrow)" />
          );
        })}

        {nodes.map((n) => (
          <Box key={n.id} n={n} dim={!on(n.id)}
               onHover={() => setHot(n.formula ? n.id : null)} onLeave={() => setHot(null)} />
        ))}

        {/* ---- the result ---- */}
        <g opacity={!lit || lit.has("prod") || lit.has("injs") ? 1 : 0.35}>
          <rect x={vrrX} y={vrrY} width={120} height={64} rx={8}
                fill={audit.matches ? "#10281c" : "#2b1418"}
                stroke={audit.matches ? "#4fc47f" : "#f2777a"} strokeWidth={1.8} />
          <text x={vrrX + 60} y={vrrY + 21} textAnchor="middle" className="fill-content-muted"
                style={{ fontSize: 11 }}>VRR</text>
          <text x={vrrX + 60} y={vrrY + 47} textAnchor="middle"
                className="font-semibold tabular-nums"
                fill={audit.matches ? "#8fe0b1" : "#f2a3a5"} style={{ fontSize: 22 }}>
            {r.vrr.toFixed(3)}
          </text>
          <text x={vrrX + 60} y={vrrY + 82} textAnchor="middle" className="fill-content-muted"
                style={{ fontSize: 11 }}>
            stored {audit.stored.vrr.toFixed(3)}
          </text>
          <text x={vrrX + 60} y={vrrY + 97} textAnchor="middle"
                fill={audit.matches ? "#4fc47f" : "#f2777a"} style={{ fontSize: 11 }}>
            Δ {audit.difference.toExponential(1)}
          </text>
        </g>

        {/* ---- formulas: small, in the corner, lighting up with the hovered term ---- */}
        <g transform={`translate(8, 390)`}>
          <text x={0} y={0} className="fill-content-muted" style={{ fontSize: 11, letterSpacing: 0.7 }}>
            FORMULAS · core.physics
          </text>
          {Object.entries(f).map(([term, formula], i) => {
            const active = hot && byId[hot]?.formula === formula;
            return (
              <text key={term} x={0} y={18 + i * 16} className="font-mono"
                    fill={active ? "#a8d2ef" : "#6b7c92"}
                    style={{ fontSize: 11, fontWeight: active ? 600 : 400 }}>
                {formula}
              </text>
            );
          })}
        </g>
      </svg>
    </div>
  );
}

/** Split an over-long table name onto two lines at an underscore. `completion_pvt_
 *  characteristics` is 30 characters and no font size that fits it in one line is above
 *  the legibility floor, so the box grows a second line instead. */
function wrap(label: string, boxW: number): string[] {
  const perChar = 6.4;                              // measured for the mono face at 11px
  if (label.length * perChar <= boxW - 16) return [label];
  const cut = label.lastIndexOf("_", Math.ceil(label.length / 2) + 4);
  return cut > 0 ? [label.slice(0, cut + 1), label.slice(cut + 1)] : [label];
}

function Box({ n, dim, onHover, onLeave }: {
  n: Node; dim: boolean; onHover: () => void; onLeave: () => void;
}) {
  const c = C[n.kind];
  const w = n.w ?? NODE_W;
  return (
    <g opacity={dim ? 0.35 : 1} onMouseEnter={onHover} onMouseLeave={onLeave}>
      <title>{n.formula ? `${n.label} = ${n.formula}` : n.label}</title>
      <rect x={n.x} y={n.y} width={w} height={NODE_H} rx={6}
            fill={c.fill} stroke={c.stroke} strokeWidth={1.2} />
      {wrap(n.label, w).map((line, i) => (
        <text key={i} x={n.x + 8} y={n.y + 16 + i * 12} fill={c.text} className="font-mono"
              style={{ fontSize: 11 }}>
          {line}
        </text>
      ))}
      <text x={n.x + 8} y={n.y + NODE_H - 9} className="fill-content-muted tabular-nums"
            style={{ fontSize: 11 }}>
        {n.value}
      </text>
    </g>
  );
}
