/**
 * The pattern, drawn the way an engineer draws it on a whiteboard: injector in the
 * middle, the producers it sweeps toward around the outside, a line between them whose
 * weight is how much of that producer belongs to this pattern.
 *
 * Two rules govern this file.
 *
 * 1. **It decides nothing.** Every x/y, every size, and the name of the shape come from
 *    `core/pattern_layout.py` through `/api/patterns/{id}/layout`. This component turns
 *    that into SVG and nothing else — same reason no other view assembles a number.
 * 2. **It never pretends to be a map.** This database has contribution factors, not
 *    coordinates, so distance from the injector means *allocation*, and the figure says
 *    so on its face. A convincing cross-section built from invented well paths would be
 *    the most trusted thing on screen and the only thing with no provenance behind it.
 *
 * Beside it sits the same period stated for someone who has never heard of VRR: of every
 * 100 reservoir barrels taken out, how many went back in.
 */
import { useEffect, useState } from "react";
import { api, type LayoutNode, type PatternLayout } from "../api";
import { Badge, Card, Spinner, fmt } from "./ui";

const C = {
  injector: "#5aa9dd",   // brand — water going down
  producer: "#4fc47f",   // signal — oil coming up
  idle: "#8b9db0",
  suspect: "#e0a83a",
  sweep: "#5aa9dd",
} as const;

/** Rendered pixels per viewBox unit. Fixed so the caption at 8.5 units always lands at
 *  ~11px — the app's smallest step — whatever extent the pattern's own viewBox has. */
const PX_PER_UNIT = 1.34;

/** Node radius in viewBox units. `size` is already area-corrected upstream. */
const radius = (n: LayoutNode) => 8 + 12 * n.size;

/** Radius including the dashed shared-well halo — what captions and sweep lines must
 *  clear. Without this the halo is drawn straight through the well's own caption. */
const outer = (n: LayoutNode) => radius(n) + (n.shared ? 6.5 : 0);

export function PatternDiagram({ patternId, period }: { patternId: string; period: string }) {
  const [data, setData] = useState<PatternLayout | null>(null);
  const [err, setErr] = useState(false);
  const [hover, setHover] = useState<string | null>(null);

  useEffect(() => {
    if (!patternId || !period) return;
    setData(null); setErr(false);
    api.layout(patternId, period).then(setData).catch(() => setErr(true));
  }, [patternId, period]);

  if (err) return null;                       // the report is still useful without a picture
  if (!data) return <Card title="Pattern layout"><Spinner label="drawing the pattern…" /></Card>;
  if (!data.found || !data.nodes?.length) {
    return (
      <Card title="Pattern layout" sub="No completions contributed in this period.">
        <p className="text-body text-content-muted">Nothing to draw for {fmt.month(period)}.</p>
      </Card>
    );
  }

  const nodes = data.nodes;
  const byId = Object.fromEntries(nodes.map((n) => [n.completion_id, n]));
  const producers = nodes.filter((n) => n.role === "producer");
  const injectors = nodes.filter((n) => n.role === "injector");
  // The sweep lines start at the injection group, not at any one injector. Modelled as a
  // stand-in node so the same trim() serves both ends of the line.
  const hubR: LayoutNode = {
    ...(injectors[0] ?? nodes[0]),
    x: data.hub?.x ?? 0, y: data.hub?.y ?? 0, shared: false,
    // Clear the whole ring of injectors plus their markers, else the line starts inside
    // one of them.
    size: ((data.hub?.radius ?? 0) + (injectors.length ? radius(injectors[0]) : 0) - 8) / 12,
  };

  return (
    <Card
      title={<span className="flex items-center gap-2">
        {data.geometry_label}
        <Badge tone="slate">{data.n_injectors} inj · {data.n_producers} prod</Badge>
        {data.shared?.length ? <Badge tone="amber">{data.shared.length} shared</Badge> : null}
        {data.low_confidence?.length
          ? <Badge tone="amber">{data.low_confidence.length} low-confidence PVT</Badge> : null}
      </span>}
      sub={data.caption}
    >
      <div className="grid gap-5 lg:grid-cols-[1.35fr_1fr]">
        <div>
          {/* Capped rather than fluid: an SVG that fills a 1500px column scales its own
              text with it, and the well labels end up three times the size of every
              other label in the app. The type scale wins over the picture. */}
          <svg viewBox={viewBox(nodes)} className="mx-auto w-full"
               style={{ maxWidth: viewBoxNums(nodes)[2] * PX_PER_UNIT }} role="img"
               aria-label={`${data.geometry_label} schematic for ${data.pattern_name}`}>
            <defs>
              {/* The swept region: strongest at the injector, fading out past the
                  producers. Suggestive shading, not a saturation map — it has no
                  contour lines for exactly that reason. */}
              <radialGradient id="sweep-area">
                <stop offset="0%" stopColor={C.injector} stopOpacity="0.20" />
                <stop offset="55%" stopColor={C.injector} stopOpacity="0.07" />
                <stop offset="100%" stopColor={C.injector} stopOpacity="0" />
              </radialGradient>
              {/* markerUnits="userSpaceOnUse" is load-bearing: the default scales the
                  marker by the line's stroke-width, and here stroke-width IS the
                  contribution factor — so a strong producer grew an arrowhead five
                  times the size of a weak one. */}
              <marker id="sweep-tip" viewBox="0 0 10 10" refX="8" refY="5"
                      markerUnits="userSpaceOnUse" markerWidth="8" markerHeight="8"
                      orient="auto-start-reverse">
                <path d="M0,1 L9,5 L0,9 z" fill={C.sweep} />
              </marker>
            </defs>

            <circle cx="0" cy="0" r="118" fill="url(#sweep-area)" />

            {/* The pattern outline — the square of a five-spot, the hexagon of a
                seven-spot. Dashed, because it is the allocation boundary, not a lease
                line and not a fault. */}
            {producers.length >= 3 && (
              <polygon
                points={ringOrder(producers).map((n) => `${n.x},${n.y}`).join(" ")}
                fill="none" stroke={C.sweep} strokeOpacity="0.55" strokeWidth="1"
                strokeDasharray="4 4"
              />
            )}

            {data.links?.map((l) => {
              const b = byId[l.to];
              if (!b) return null;
              const injectorHovered = hover !== null && byId[hover]?.role === "injector";
              const lit = hover === null || hover === l.to || injectorHovered;
              // Stop the line at the hub edge and again at the producer's. Drawn
              // centre-to-centre the arrowhead lands underneath the producer and the
              // sweep loses the one thing it is there to show — which way water is going.
              const [x1, y1, x2, y2] = trim(hubR, b);
              return (
                <line
                  key={l.to} x1={x1} y1={y1} x2={x2} y2={y2}
                  stroke={C.sweep} strokeWidth={1 + 3.4 * l.factor}
                  strokeOpacity={lit ? 0.9 : 0.15}
                  strokeLinecap="round" markerEnd="url(#sweep-tip)" className="sweep-flow"
                />
              );
            })}

            {nodes.map((n) => (
              <Well key={n.completion_id} n={n}
                    dim={hover !== null && hover !== n.completion_id}
                    onHover={setHover} />
            ))}
          </svg>

          <Legend />
        </div>

        <FluidBalance data={data} />
      </div>

      <p className="mt-4 border-t border-surface-divider pt-3 text-label leading-relaxed text-content-muted">
        <strong className="font-medium text-content-secondary">Schematic, not a map.</strong>{" "}
        Wells are placed by contribution factor — a producer drawn closer to the injector
        gives this pattern a larger share of its volumes. The database holds no well
        coordinates, surveys or perforation depths, so no distance here is in feet.
        Roles are what each completion actually did in {fmt.month(period)}, not how it was
        designed, so a converted well appears under the role it played.
      </p>
    </Card>
  );
}

function Well({ n, dim, onHover }: {
  n: LayoutNode; dim: boolean; onHover: (id: string | null) => void;
}) {
  const r = radius(n), o = outer(n);
  const fill = n.role === "injector" ? C.injector : n.role === "producer" ? C.producer : C.idle;
  return (
    <g opacity={dim ? 0.35 : 1}
       onMouseEnter={() => onHover(n.completion_id)} onMouseLeave={() => onHover(null)}
       style={{ cursor: "default" }}>
      <title>
        {`${n.completion_name} — ${n.role}\n`}
        {`contribution factor ${n.factor.toFixed(2)}\n`}
        {`${fmt.pct(n.share)} of the pattern's ${n.role === "injector" ? "injection" : "production"}`}
        {` (${fmt.bbl(n.res_bbl)} res bbl)`}
        {n.shared ? `\nshared across ${n.n_patterns} patterns` : ""}
        {n.low_confidence ? `\nPVT: ${n.pvt_methods} — low confidence` : ""}
      </title>

      {/* A completion split across patterns wears a dashed halo. It is the usual reason
          two engineers disagree about a VRR and it is invisible in every other view. */}
      {n.shared && (
        <circle cx={n.x} cy={n.y} r={r + 5.5} fill="none" stroke={C.suspect}
                strokeWidth="1.2" strokeDasharray="3 3" />
      )}
      <circle cx={n.x} cy={n.y} r={r} fill={fill} fillOpacity={n.role === "idle" ? 0.25 : 1}
              stroke={n.low_confidence ? C.suspect : "#18212d"}
              strokeWidth={n.low_confidence ? 2.5 : 2} />

      {/* Down into the ground for injection, up out of it for production — the one
          glyph that needs no legend. */}
      <path
        d={n.role === "injector" ? arrow(n.x, n.y, r, 1)
          : n.role === "producer" ? arrow(n.x, n.y, r, -1) : ""}
        stroke="#0b1219" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"
      />
      {n.role === "idle" && (
        <line x1={n.x - 4} y1={n.y} x2={n.x + 4} y2={n.y} stroke="#0b1219" strokeWidth="2" />
      )}

      {/* Caption above the wells in the top half, below the ones underneath: every sweep
          line runs outward from the centre, so a label on the centre side of its well
          always has a line through it. */}
      <text x={n.x} y={above(n) ? n.y - o - 14 : n.y + o + 11} textAnchor="middle"
            className="fill-content-secondary font-medium" style={{ fontSize: 8.5, ...HALO }}>
        {n.completion_name}
      </text>
      <text x={n.x} y={above(n) ? n.y - o - 3 : n.y + o + 22} textAnchor="middle"
            className="fill-content-muted" style={{ fontSize: 8.2, ...HALO }}>
        {n.role === "idle" ? "idle" : `f ${n.factor.toFixed(2)} · ${fmt.pct(n.share)}`}
      </text>
    </g>
  );
}

/** A vertical arrow inside a well marker. `dir` +1 points down (injection). */
function arrow(x: number, y: number, r: number, dir: 1 | -1) {
  const h = Math.min(r * 0.52, 7);
  const tip = y + dir * h, tail = y - dir * h;
  return `M${x},${tail} L${x},${tip} M${x - 3.4},${tip - dir * 3.4} L${x},${tip} `
       + `L${x + 3.4},${tip - dir * 3.4}`;
}

/** A white knock-out behind label text. The injector sits at the centre with sweep
 *  lines radiating in every direction, so there is no side its caption can be moved to
 *  that a line does not cross — the label has to defend itself instead. */
// Knock-out in the CARD colour, not white — on a dark card a white halo turns every
// caption into a glowing smear.
const HALO = { stroke: "#18212d", strokeWidth: 1.7, paintOrder: "stroke" } as const;

/** Producers in the upper half wear their caption above; everything else below. */
const above = (n: LayoutNode) => n.y < -12;

/** A viewBox that just contains the wells and their captions.
 *
 *  Computed rather than fixed because the figure's extent genuinely varies — a two-well
 *  line drive and an eight-producer nine-spot with idle wells on the rim are not the
 *  same size, and a box drawn for the worst case leaves the common case floating in a
 *  field of white. */
function viewBox(nodes: LayoutNode[]): string {
  const [x, y, w, h] = viewBoxNums(nodes);
  return `${x} ${y} ${w} ${h}`;
}

function viewBoxNums(nodes: LayoutNode[]): [number, number, number, number] {
  const pad = 6, caption = 27;
  const xs = nodes.flatMap((n) => [n.x - outer(n), n.x + outer(n)]);
  const ys = nodes.flatMap((n) => [
    n.y - outer(n) - (above(n) ? caption : 0),
    n.y + outer(n) + (above(n) ? 0 : caption),
  ]);
  // Labels stick out sideways further than the marker does; half a name at ~4.6 units
  // per character is close enough and cheaper than measuring text.
  const nameHalf = Math.max(...nodes.map((n) => n.completion_name.length)) * 2.3;
  const minX = Math.min(...xs) - nameHalf - pad, maxX = Math.max(...xs) + nameHalf + pad;
  const minY = Math.min(...ys) - pad, maxY = Math.max(...ys) + pad;
  return [minX, minY, maxX - minX, maxY - minY];
}

/** Shorten a centre-to-centre segment to run edge-to-edge, leaving room for the tip. */
function trim(a: LayoutNode, b: LayoutNode): [number, number, number, number] {
  const dx = b.x - a.x, dy = b.y - a.y;
  const len = Math.hypot(dx, dy) || 1;
  const ux = dx / len, uy = dy / len;
  const from = outer(a) + 3, to = len - outer(b) - 6;
  return [a.x + ux * from, a.y + uy * from, a.x + ux * to, a.y + uy * to];
}

/** Producers in angular order, so the outline is a convex ring and not a bowtie. */
function ringOrder(nodes: LayoutNode[]) {
  return [...nodes].sort((a, b) => Math.atan2(a.y, a.x) - Math.atan2(b.y, b.x));
}

function Legend() {
  const items: [string, string, string][] = [
    [C.injector, "Injector", "water in"],
    [C.producer, "Producer", "oil + water out"],
    [C.suspect, "Amber ring", "shared or low-confidence PVT"],
  ];
  return (
    <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1">
      {items.map(([colour, label, note]) => (
        <span key={label} className="flex items-center gap-1.5 text-micro text-content-muted">
          <span className="h-2.5 w-2.5 rounded-full" style={{ background: colour }} />
          <span className="font-medium text-content-secondary">{label}</span> {note}
        </span>
      ))}
      <span className="text-micro text-content-muted">
        line weight = contribution factor · circle area = share of volume
      </span>
    </div>
  );
}

/**
 * The same month with no jargon in it. VRR is a ratio of two numbers that are already on
 * this page; the only thing added here is the sentence a non-engineer can repeat.
 */
function FluidBalance({ data }: { data: PatternLayout }) {
  const inj = data.inj_res_bbl ?? 0;
  const prod = data.prod_res_bbl ?? 0;
  const vrr = data.vrr ?? 0;
  const scale = Math.max(inj, prod) || 1;
  const putBack = Math.round(vrr * 100);
  const tone = vrr >= 0.95 && vrr <= 1.05 ? "signal" : vrr < 0.95 ? "offtarget" : "suspect";
  const toneHex = { signal: "#4fc47f", offtarget: "#f2777a", suspect: "#e0a83a" }[tone];

  return (
    <div className="self-start rounded-lg bg-surface-raised p-4">
      <div className="text-micro font-medium uppercase tracking-wide text-content-muted">
        The balance, in plain terms
      </div>
      <p className="mt-2 text-sub leading-snug text-content-primary">
        For every <strong>100 barrels</strong> of space emptied in the rock,{" "}
        <strong style={{ color: toneHex }}>{putBack} barrels</strong> were put back.
      </p>
      <p className="mt-1 text-label leading-relaxed text-content-muted">
        {vrr < 0.95
          ? "Less goes in than comes out, so pressure falls and the reservoir gives up oil more slowly."
          : vrr > 1.05
          ? "More goes in than comes out. Pressure builds, and water can arrive at the producers early."
          : "In balance — pressure is holding, which is what the flood is for."}
      </p>

      <div className="mt-4 space-y-3">
        <Bar label="Put back — injection" value={inj} scale={scale} colour={C.injector} />
        <Bar label="Taken out — production" value={prod} scale={scale} colour={C.producer} />
      </div>

      <p className="mt-4 text-label leading-relaxed text-content-muted">
        Both measured <em>down in the reservoir</em>, not at surface — a barrel of oil
        shrinks on the way up, so surface barrels would not compare. That conversion is
        the PVT step, and it is why the amber rings above matter.
      </p>
    </div>
  );
}

function Bar({ label, value, scale, colour }: {
  label: string; value: number; scale: number; colour: string;
}) {
  return (
    <div>
      <div className="flex items-baseline justify-between text-micro">
        <span className="text-content-secondary">{label}</span>
        <span className="tabular-nums font-medium text-content-primary">{fmt.bbl(value)} bbl</span>
      </div>
      <div className="mt-1 h-2.5 overflow-hidden rounded-full bg-surface-raised">
        <div className="h-full rounded-full transition-all"
             style={{ width: `${Math.max(2, (value / scale) * 100)}%`, background: colour }} />
      </div>
    </div>
  );
}
