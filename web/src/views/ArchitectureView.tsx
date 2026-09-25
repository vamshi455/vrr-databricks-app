/**
 * The system, drawn from the system — click any box.
 *
 * Every other view answers a question about the reservoir. This one answers the question
 * a new reader actually asks first: *what runs when I ask something, and why should I
 * believe the number that comes back?* Prose in a README cannot answer it honestly,
 * because prose does not know whether the model is up, whether tracing is on, or how many
 * documents are sitting unapproved right now. This does — every counter is measured when
 * the request lands.
 *
 * Three decisions worth keeping:
 *
 * - **Nothing here computes.** Positions, labels, and the formatted figure on each box
 *   all arrive from `core/architecture.py`, the same contract the pattern schematic and
 *   the lineage graph use. React draws; it does not decide.
 * - **A box with no measurement shows no number.** `value` is `null` when a probe
 *   failed, and a dash rendered in its place would read as "zero", which is a different
 *   claim from "I could not tell".
 * - **The boxes are operable without a mouse.** They are `<g role="button" tabIndex={0}>`
 *   with Enter/Space handling and a drawn focus ring — the SVG equivalent of a real
 *   button, since an HTML button cannot live inside an `<svg>`. The July audit found
 *   this app shipped with no visible focus anywhere; it is not repeating that here.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type ArchNode, type Architecture } from "../api";
import { Card, ErrorNote, Spinner } from "../components/ui";

/** How often the counters re-read. Paused while the tab is hidden — a background tab
 *  polling a database every five seconds is a cost with no reader. */
const POLL_MS = 5_000;

/** One accent per band, from the app palette. The approval lane overrides per node so a
 *  card's colour matches the swim-lane board it came from. */
const BAND_ACCENT: Record<string, string> = {
  ingest: "#5aa9dd",     // brand — the deterministic path
  agent: "#a98bdc",      // the turn
  knowledge: "#4fc47f",  // signal — the gated corpus
  approval: "#8b9db0",   // per-node below
  llmops: "#e0a83a",     // suspect — observation, not enforcement
};

const STAGE_ACCENT: Record<string, string> = {
  draft: "#8b9db0", analyst: "#5aa9dd", rm: "#a98bdc",
  site: "#e0a83a", executed: "#4fc47f",
};

const EDGE = "#6b7c92";

function accentFor(n: ArchNode): string {
  return STAGE_ACCENT[n.id] ?? BAND_ACCENT[n.band] ?? "#8b9db0";
}

export function ArchitectureView() {
  const [arch, setArch] = useState<Architecture | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [fetchedAt, setFetchedAt] = useState<number | null>(null);
  const [, forceTick] = useState(0);
  const live = useRef(true);

  const load = useCallback(() => {
    api.architecture()
      .then((a) => { setArch(a); setFetchedAt(Date.now()); setError(null); })
      .catch(setError);
  }, []);

  // Poll, but only while the tab is actually being looked at. `visibilitychange` also
  // triggers an immediate refresh on return, so coming back to the tab never shows a
  // counter that is minutes stale while claiming to be live.
  useEffect(() => {
    load();
    const id = setInterval(() => { if (!document.hidden) load(); }, POLL_MS);
    const onVis = () => {
      live.current = !document.hidden;
      if (!document.hidden) load();
    };
    document.addEventListener("visibilitychange", onVis);
    return () => { clearInterval(id); document.removeEventListener("visibilitychange", onVis); };
  }, [load]);

  // A separate one-second tick purely so "updated 3s ago" counts up between polls.
  useEffect(() => {
    const id = setInterval(() => forceTick((n) => n + 1), 1_000);
    return () => clearInterval(id);
  }, []);

  const byId = useMemo(
    () => Object.fromEntries((arch?.nodes ?? []).map((n) => [n.id, n])), [arch]);

  // Selecting a box dims everything it does not touch. Neighbours, not full ancestry:
  // this is a system map, and almost everything is transitively connected to everything.
  const related = useMemo(() => {
    if (!arch || !selected) return null;
    const set = new Set([selected]);
    for (const e of arch.edges) {
      if (e.from === selected) set.add(e.to);
      if (e.to === selected) set.add(e.from);
    }
    return set;
  }, [arch, selected]);

  if (error) return <ErrorNote error={error} />;
  if (!arch) return <Spinner label="reading the system…" />;

  const lit = (id: string) => !related || related.has(id);
  const node = selected ? byId[selected] : null;

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <div>
          <h1 className="text-title font-semibold">Architecture</h1>
          <p className="text-label text-content-secondary">
            What runs when you ask a question. Click any box.
          </p>
        </div>
        <p className="flex items-center gap-1.5 text-micro text-content-muted">
          <span className="inline-block size-1.5 rounded-full bg-signal" aria-hidden />
          live · updated {agoLabel(fetchedAt)}
        </p>
      </header>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_20rem]">
        {/* `min-w-0` is load-bearing. A grid item defaults to `min-width: auto`, so the
            900px minimum on the SVG below pushed this card to 924px inside a 353px
            track — the scroll container never got the chance to scroll, and the card's
            own heading was clipped at the viewport edge instead. Measured at 375px, not
            eyeballed: the page reported no horizontal scroll the whole time, because the
            overflow was happening one level down. */}
        <Card
          className="min-w-0"
          title="The system, from the system"
          sub="Every figure below was measured when this page last polled — nothing on this diagram is written down."
        >
          <div className="overflow-x-auto">
            <svg
              viewBox={`0 0 ${arch.canvas.w} ${arch.canvas.h}`}
              className="w-full min-w-[900px]"
              role="group"
              aria-label="System architecture: ingest, the turn, knowledge, approval chain, and LLM ops"
            >
              <defs>
                <marker id="arch-arrow" viewBox="0 0 10 10" refX="9" refY="5"
                        markerWidth="5" markerHeight="5" orient="auto">
                  <path d="M0,2 L9,5 L0,8 z" fill={EDGE} />
                </marker>
              </defs>

              {/* ---- bands ---- */}
              {arch.bands.map((b) => (
                <g key={b.id}>
                  <rect x={b.x} y={b.y} width={b.w} height={b.h} rx={10}
                        fill="#0d141d" stroke="#232e3d" strokeWidth={1} />
                  <text x={b.x + 16} y={b.y + 18} className="fill-content-muted"
                        style={{ fontSize: 11, letterSpacing: 0.8, fontWeight: 600 }}>
                    {b.title}
                  </text>
                  {/* A fixed column, not `title.length * 7.2`. That estimate ignored the
                      0.8 letter-spacing and ran the subtitle into the title on the two
                      longest bands — "KNOWLEDGEa document cannot be searched…". Measuring
                      SVG text properly needs a render pass; a column wide enough for the
                      longest title does the job with no measurement at all. */}
                  <text x={b.x + 148} y={b.y + 18}
                        className="fill-content-muted" style={{ fontSize: 11 }}>
                    {b.sub}
                  </text>
                </g>
              ))}

              {/* ---- edges ---- */}
              {arch.edges.map((e) => {
                const s = byId[e.from], d = byId[e.to];
                if (!s || !d) return null;
                const dim = !(lit(e.from) && lit(e.to));
                return (
                  <Edge key={`${e.from}-${e.to}`} s={s} d={d} label={e.label} dim={dim} />
                );
              })}

              {/* ---- boxes ---- */}
              {arch.nodes.map((n) => (
                <Box key={n.id} n={n} dim={!lit(n.id)} active={selected === n.id}
                     onSelect={() => setSelected(selected === n.id ? null : n.id)} />
              ))}
            </svg>
          </div>
        </Card>

        <DetailPanel node={node} onClose={() => setSelected(null)} />
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------- edges ---- */
/**
 * Same-row edges leave the right face and enter the left; anything crossing a band
 * leaves a horizontal face and enters vertically, dashed. The dash is the point: a solid
 * line is the normal flow of one stage, a dashed one is a claim about how two stages
 * touch, which is what a reader is most likely to doubt.
 */
function Edge({ s, d, label, dim }: {
  s: { x: number; y: number; w: number; h: number };
  d: { x: number; y: number; w: number; h: number };
  label: string; dim: boolean;
}) {
  const sameRow = Math.abs(s.y - d.y) < 4;
  let path: string, lx: number, ly: number;

  if (sameRow) {
    const x1 = s.x + s.w, y1 = s.y + s.h / 2;
    const x2 = d.x, y2 = d.y + d.h / 2;
    const mid = (x1 + x2) / 2;
    path = `M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}`;
    lx = mid; ly = y1 - 6;
  } else {
    const down = d.y > s.y;
    const x1 = s.x + s.w / 2, y1 = down ? s.y + s.h : s.y;
    const x2 = d.x + d.w / 2, y2 = down ? d.y : d.y + d.h;
    const mid = (y1 + y2) / 2;
    path = `M${x1},${y1} C${x1},${mid} ${x2},${mid} ${x2},${y2}`;
    // At the source end, NOT the vertical midpoint. An edge crossing two band gaps has
    // its midpoint inside some third band, where the caption is drawn over whatever box
    // happens to be there — "signed role" landed across the `analyst` card. The strip
    // just outside the source box is the band's own padding and is always clear.
    lx = x1; ly = down ? y1 + 13 : y1 - 7;
  }

  return (
    <g opacity={dim ? 0.12 : 1}>
      <path d={path} fill="none" stroke={EDGE} strokeWidth={1.2} strokeOpacity={0.75}
            strokeDasharray={sameRow ? undefined : "4 3"} markerEnd="url(#arch-arrow)" />
      {label && (
        <text x={lx} y={ly} textAnchor="middle" className="fill-content-muted"
              style={{ fontSize: 11 }}>
          {label}
        </text>
      )}
    </g>
  );
}

/* ------------------------------------------------------------------- boxes ---- */
function Box({ n, dim, active, onSelect }: {
  n: ArchNode; dim: boolean; active: boolean; onSelect: () => void;
}) {
  const [focused, setFocused] = useState(false);
  const accent = accentFor(n);

  return (
    <g
      role="button"
      tabIndex={0}
      aria-pressed={active}
      // The figure is part of the name: a screen reader user picking through the map
      // should hear "Knowledge index, 76 chunks indexed", not just a noun.
      aria-label={`${n.label}${n.value ? `, ${n.value}` : ""}`}
      onClick={onSelect}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSelect(); }
      }}
      onFocus={() => setFocused(true)}
      onBlur={() => setFocused(false)}
      opacity={dim ? 0.22 : 1}
      style={{ cursor: "pointer", outline: "none" }}
    >
      {/* Drawn rather than inherited: the app's :focus-visible rule targets HTML
          controls, and an SVG group would otherwise be reachable by Tab but invisible. */}
      {focused && (
        <rect x={n.x - 4} y={n.y - 4} width={n.w + 8} height={n.h + 8} rx={10}
              fill="none" stroke="#5aa9dd" strokeWidth={2} />
      )}
      <rect x={n.x} y={n.y} width={n.w} height={n.h} rx={7}
            fill={active ? "#222d3c" : "#18212d"}
            stroke={accent} strokeWidth={active ? 1.8 : 1} strokeOpacity={active ? 1 : 0.55} />
      {/* A left edge in the band's accent, so the lane a box belongs to survives the
          eye jumping around the map. */}
      <rect x={n.x} y={n.y} width={3} height={n.h} rx={1.5} fill={accent} />

      <text x={n.x + 12} y={n.y + 23} className="fill-content-primary"
            style={{ fontSize: 12, fontWeight: 600 }}>
        {n.label}
      </text>
      {n.value && (
        <text x={n.x + 12} y={n.y + 41} className="fill-content-muted tabular-nums"
              style={{ fontSize: 11 }}>
          {n.value}
        </text>
      )}
      {/* A dot means this box enforces something. Shape, not colour alone. */}
      {n.guardrail && (
        <circle cx={n.x + n.w - 11} cy={n.y + 12} r={3} fill={accent}>
          <title>Enforces a guardrail — click for detail</title>
        </circle>
      )}
    </g>
  );
}

/* ------------------------------------------------------------------ detail ---- */
function DetailPanel({ node, onClose }: { node: ArchNode | null; onClose: () => void }) {
  if (!node) {
    return (
      <Card title="Nothing selected" sub="Click a box to see what it is and where it lives">
        <p className="text-body text-content-secondary">
          A dot in a box's top-right corner means it enforces a rule — a role check, a
          human gate, a clamp — rather than just moving data along.
        </p>
      </Card>
    );
  }

  return (
    <Card title={node.label} sub={node.value ?? "no measurement available right now"}>
      <div className="space-y-3">
        <p className="text-body text-content-secondary">{node.what}</p>

        {node.guardrail && (
          <div className="rounded-md border border-surface-border bg-surface-raised px-2.5 py-2">
            <p className="text-micro font-medium uppercase tracking-wide text-content-muted">
              Guardrail
            </p>
            <p className="mt-1 text-label text-content-primary">{node.guardrail}</p>
          </div>
        )}

        {node.files.length > 0 && (
          <div>
            <p className="text-micro font-medium uppercase tracking-wide text-content-muted">
              Where it lives
            </p>
            <ul className="mt-1 space-y-0.5">
              {node.files.map((f) => (
                <li key={f} className="font-mono text-label text-content-secondary">{f}</li>
              ))}
            </ul>
          </div>
        )}

        <button onClick={onClose}
                className="rounded-md border border-surface-border px-2.5 py-1 text-label
                           text-content-secondary hover:bg-surface-raised">
          Clear selection
        </button>
      </div>
    </Card>
  );
}

/** "3s ago" / "2m ago". Null until the first successful poll — never "0s ago" for a
 *  fetch that has not happened. */
function agoLabel(at: number | null): string {
  if (at === null) return "never";
  const secs = Math.max(0, Math.round((Date.now() - at) / 1000));
  return secs < 60 ? `${secs}s ago` : `${Math.round(secs / 60)}m ago`;
}
