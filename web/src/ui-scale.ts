/**
 * How big the whole application is, in one number.
 *
 * The six type tokens and Tailwind's spacing scale are all rem, so the root font-size
 * scales type AND padding AND gaps AND card widths in step — the same thing browser zoom
 * does. Changing the tokens alone would have shrunk the text and left the whitespace,
 * turning a dense workbench into a sparse one.
 *
 * **68% is the default** because that is where this workbench is read: the operator was
 * running the browser at 68% zoom, so baking it in gives the same picture at 100%.
 *
 * The honest cost: at 68% the `micro` token renders around 7.5px, against the 11px floor
 * the accessibility pass set in July. That floor was not arbitrary — below it glyphs fail
 * regardless of contrast. So this is a *setting* with a way back, not a constant: the
 * choice is per reader, persisted, and applied before first paint so there is no flash of
 * full-size layout on every load.
 */

const KEY = "vrr.ui-scale";

export type ScaleId = "compact" | "default" | "large";

/** Percentages of the browser's root size (16px unless the reader changed it). */
export const SCALES: Record<ScaleId, { pct: number; label: string; hint: string }> = {
  compact: { pct: 68, label: "Compact", hint: "most on screen — body text ~8px" },
  default: { pct: 85, label: "Default", hint: "body text ~10px" },
  large: { pct: 100, label: "Large", hint: "the audited scale — body text 12px" },
};

export const DEFAULT_SCALE: ScaleId = "compact";

export function readScale(): ScaleId {
  const v = localStorage.getItem(KEY);
  return v && v in SCALES ? (v as ScaleId) : DEFAULT_SCALE;
}

export function applyScale(id: ScaleId): void {
  document.documentElement.style.setProperty("--ui-scale", `${SCALES[id].pct}%`);
}

export function saveScale(id: ScaleId): void {
  localStorage.setItem(KEY, id);
  applyScale(id);
}

/** Called from `main.tsx` BEFORE React renders, so the first paint is already correct. */
export function initScale(): ScaleId {
  const id = readScale();
  applyScale(id);
  return id;
}
