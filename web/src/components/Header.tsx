/**
 * The app bar: brand left, context centre, status and identity right.
 *
 * Identity belongs top-right because that is where every application puts it, and
 * because it is *status*, not a control — you cannot change your role here, only see it.
 * The old sidebar dropdown implied the opposite.
 *
 * The trace badge sits beside it for a specific reason: this workbench went untraced for
 * a whole session once, silently, because the indicator was a grey line buried in the
 * sidebar. Now it is red and next to your name until MLflow comes back.
 */
import { useState } from "react";
import type { Health, Identity } from "../api";
import { SCALES, type ScaleId, readScale, saveScale } from "../ui-scale";

export function Header({ me, health, patternName, period, onSignIn, onSignOut }: {
  me: Identity | null;
  health: Health | null;
  patternName: string;
  period: string;
  onSignIn: () => void;
  onSignOut: () => void;
}) {
  const traced = health?.tracing.enabled;
  const llm = health?.llm;

  return (
    <header className="flex h-14 shrink-0 items-center gap-2 border-b border-surface-border bg-surface-card
                       px-3 sm:gap-4 sm:px-4">
      {/* ------------------------------------------------------------- brand */}
      <div className="flex min-w-0 items-center gap-2.5">
        <MeridianMark />
        <div className="min-w-0 leading-none">
          <div className="truncate text-sub font-semibold tracking-tight text-brand-900">
            Meridian Petroleum
          </div>
          <div className="mt-0.5 hidden text-micro text-content-muted sm:block">
            VRR Reasoning &amp; Lineage
          </div>
        </div>
      </div>

      {/* ----------------------------------------------------------- context */}
      <div className="ml-2 hidden items-center gap-2 border-l border-surface-border pl-4 lg:flex">
        <span className="text-label text-content-muted">Reviewing</span>
        <span className="text-label font-medium text-content-primary">{patternName || "—"}</span>
        <span className="text-surface-border">·</span>
        <span className="text-label text-content-secondary">
          {period
            ? new Date(period + "T00:00:00").toLocaleDateString(undefined,
                { month: "short", year: "numeric" })
            : "—"}
        </span>
      </div>

      <div className="flex-1" />

      {/* ------------------------------------------------------------ status */}
      <a
        href={health?.tracing.uri}
        target="_blank"
        rel="noreferrer"
        title={traced
          ? "Every question is recorded as a span tree in MLflow"
          : "MLflow is unreachable — answers still work, but they are NOT being recorded"}
        className={`flex shrink-0 items-center gap-1.5 rounded-full px-2.5 py-1 text-micro font-medium ${
          traced
            ? "bg-signal-soft text-signal hover:bg-signal-soft"
            : "bg-offtarget-soft text-offtarget hover:bg-offtarget-soft"
        }`}
      >
        <span className={`h-1.5 w-1.5 rounded-full ${traced ? "bg-signal" : "bg-offtarget"}`} />
        {traced ? "Traced" : "NOT TRACED"}
      </a>

      <span
        title={llm?.available
          ? `${llm.model} via ${llm.provider} — phrasing only; every number stays tool-computed`
          : "No model running — answers are fully computed"}
        className="hidden shrink-0 items-center gap-1.5 rounded-full bg-surface-raised px-2.5 py-1
                   text-micro font-medium text-content-secondary sm:flex"
      >
        <span className={`h-1.5 w-1.5 rounded-full ${
          llm?.available ? "bg-signal" : "bg-content-muted"}`} />
        {llm?.available ? llm.model : "no model"}
      </span>

      <ScalePicker />

      {/* ---------------------------------------------------------- identity */}
      <div className="ml-1 shrink-0 border-l border-surface-border pl-2 sm:pl-4">
        {me ? (
          <div className="flex items-center gap-2.5">
            <div className="hidden text-right leading-tight sm:block">
              <div className="text-label font-medium text-content-primary">{me.username}</div>
              <div className="text-micro text-content-muted">
                role <span className="font-medium text-brand-600">{me.role}</span>
                <span className="text-content-muted"> · from your token</span>
              </div>
            </div>
            <Avatar name={me.username} />
            <button
              onClick={onSignOut}
              className="text-micro text-content-muted underline underline-offset-2 hover:text-content-primary"
            >
              sign out
            </button>
          </div>
        ) : (
          <button
            onClick={onSignIn}
            className="rounded-md bg-brand-500 px-3 py-1.5 text-label font-medium text-surface-base hover:bg-brand-600"
          >
            Sign in
          </button>
        )}
      </div>
    </header>
  );
}

/**
 * How big the app is, for this reader.
 *
 * A native `<select>` rather than a custom menu: it gets keyboard operation, the global
 * `:focus-visible` ring and the platform's own touch handling for free, and this is a
 * setting you change once — not a control worth a bespoke widget.
 *
 * It exists because the default is 68%, which puts body text near 8px. That is what this
 * workbench is read at, but it is well under the 11px floor the July accessibility pass
 * set, so the way back has to be one click away rather than a rebuild.
 */
function ScalePicker() {
  const [scale, setScale] = useState<ScaleId>(() => readScale());
  return (
    <label className="hidden shrink-0 items-center gap-1.5 md:flex"
           title="Scales the whole interface — type, padding and spacing together">
      <span className="text-micro text-content-muted">size</span>
      <select
        value={scale}
        onChange={(e) => { const v = e.target.value as ScaleId; setScale(v); saveScale(v); }}
        className="rounded-md border border-surface-border bg-surface-raised px-1.5 py-1
                   text-micro text-content-secondary"
      >
        {(Object.keys(SCALES) as ScaleId[]).map((id) => (
          <option key={id} value={id}>{SCALES[id].label}</option>
        ))}
      </select>
    </label>
  );
}

/** Two arcs meeting a vertical — a meridian. Inline SVG so there is no asset to fetch. */
function MeridianMark() {
  return (
    <svg width="30" height="30" viewBox="0 0 32 32" aria-label="Meridian Petroleum">
      <circle cx="16" cy="16" r="14" fill="#1b4664" />
      <ellipse cx="16" cy="16" rx="6.5" ry="14" fill="none" stroke="#7ba7c4" strokeWidth="1.4" />
      <line x1="16" y1="2" x2="16" y2="30" stroke="#b7791f" strokeWidth="1.6" />
      <line x1="2.6" y1="16" x2="29.4" y2="16" stroke="#7ba7c4" strokeWidth="1.2" opacity="0.7" />
    </svg>
  );
}

function Avatar({ name }: { name: string }) {
  const initials = name.split(/[.@]/).slice(0, 2).map((p) => p[0]?.toUpperCase()).join("");
  return (
    <span className="flex h-8 w-8 items-center justify-center rounded-full bg-brand-100 text-label font-semibold text-brand-700">
      {initials || "?"}
    </span>
  );
}
