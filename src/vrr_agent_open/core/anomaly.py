"""Deterministic VRR anomaly detection + action-queue draft assembly (design §5.4B).

Same trust model as physics.py/recommend.py: everything here is COMPUTED — the
rules, the magnitudes, and the narrative all cite their inputs. Pure Python (no
I/O) so it unit-tests off-cluster; ``09_anomaly_to_queue.py`` supplies the data
(pattern_vrr_monthly history, pattern_memory, safety_limits, injector state).

Detection rules (design §5.4B "out-of-band VRR, sustained MoM drift,
extrapolated-PVT flags"):
  out_of_band       latest VRR outside the pattern's normal band (pattern_memory
                    typical_low/high when learned, else the global TARGET_BAND)
  sustained_drift   >= DRIFT_MIN_PERIODS consecutive same-sign MoM moves with
                    cumulative |dVRR| >= DRIFT_MIN_TOTAL
  extrapolated_pvt  latest row built on extrapolated/closest PVT — an INPUT
                    problem, so per guardrail §6 ("never recommend on suspect
                    inputs") the draft is investigate-inputs, NOT a valve change.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..config import TARGET_BAND

DRIFT_MIN_PERIODS = 3     # consecutive same-sign MoM moves to call it a drift
DRIFT_MIN_TOTAL = 0.10    # cumulative |dVRR| across those moves


@dataclass(frozen=True)
class Anomaly:
    kind: str            # out_of_band | sustained_drift | extrapolated_pvt
    severity: str        # high | medium | low
    detail: str          # human-readable, cites the numbers
    vrr_date: str        # period the anomaly is anchored to (latest)
    vrr: Optional[float]
    actionable: bool     # False => investigate-inputs draft, no valve change


def _num(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def detect_anomalies(
    history: list[dict], *, target_vrr: float,
    band: Optional[tuple[float, float]] = None,
    drift_min_periods: int = DRIFT_MIN_PERIODS,
    drift_min_total: float = DRIFT_MIN_TOTAL,
) -> list[Anomaly]:
    """Run the deterministic rules over one pattern's period-ordered VRR history.

    ``history``: rows of pattern_vrr_monthly (oldest→newest) with at least
    vrr_date, vrr, any_extrapolated. ``band`` is (low, high) from pattern_memory
    when the pattern has a learned normal band, else the global TARGET_BAND.
    """
    rows = [r for r in history if _num(r.get("vrr")) is not None]
    if not rows:
        return []
    rows = sorted(rows, key=lambda r: str(r.get("vrr_date")))
    latest = rows[-1]
    vrr, vdate = _num(latest.get("vrr")), str(latest.get("vrr_date"))
    lo, hi = band or TARGET_BAND
    out: list[Anomaly] = []

    # 1) input audit FIRST (guardrail §6) — suspect inputs veto valve actions.
    suspect = bool(latest.get("any_extrapolated"))
    if suspect:
        out.append(Anomaly(
            kind="extrapolated_pvt", severity="low",
            detail=(f"Latest period {vdate} is built on extrapolated/closest PVT "
                    "lookups; VRR value is suspect — audit inputs before acting."),
            vrr_date=vdate, vrr=vrr, actionable=False))

    # 2) out-of-band VRR vs the pattern's normal band.
    if vrr < lo or vrr > hi:
        side = "over-replicating" if vrr > hi else "under-replicating"
        out.append(Anomaly(
            kind="out_of_band", severity="high",
            detail=(f"{side} — VRR {vrr:.2f} on {vdate} is outside the normal band "
                    f"[{lo:.2f}, {hi:.2f}] (target {target_vrr:.2f})."),
            vrr_date=vdate, vrr=vrr, actionable=not suspect))

    # 3) sustained MoM drift: consecutive same-sign moves ending at the latest row.
    deltas = [_num(b.get("vrr")) - _num(a.get("vrr")) for a, b in zip(rows, rows[1:])]
    run, total = 0, 0.0
    for d in reversed(deltas):
        if d == 0 or (run and (d > 0) != (total > 0)):
            break
        run += 1
        total += d
    if run >= drift_min_periods and abs(total) >= drift_min_total:
        direction = "upward" if total > 0 else "downward"
        out.append(Anomaly(
            kind="sustained_drift", severity="medium",
            detail=(f"Sustained {direction} drift — {run} consecutive periods, "
                    f"cumulative dVRR {total:+.2f}, ending {vdate} at VRR {vrr:.2f}."),
            vrr_date=vdate, vrr=vrr, actionable=not suspect))
    return out


def build_draft(
    *, pattern_id: str, pattern_name: str, anomaly: Anomaly,
    recommendation: Optional[dict] = None, driver: Optional[str] = None,
    precedent: Optional[dict] = None, response_factor: float = 1.0,
    n_adjustments: int = 0,
) -> dict:
    """Assemble one action_queue draft row: anomaly + computed recommendation +
    precedent + a deterministic narrative that CITES all three (design §5.4C.5 —
    named wells, drate, expected post-VRR, driver, precedent, confidence)."""
    rec = recommendation or {}
    actionable = anomaly.actionable and rec.get("ok") and rec.get("direction") not in (None, "none")
    changes = rec.get("injector_changes") or []

    lines = [f"[{anomaly.severity.upper()}] {pattern_name} ({pattern_id}): {anomaly.detail}"]
    if driver:
        lines.append(f"Dominant driver (VRR_DECOMPOSE): {driver}.")
    if not anomaly.actionable:
        lines.append("Input audit failed — investigate source data; no valve change proposed.")
    elif actionable:
        for c in changes:
            lines.append(
                f"{c['completion_id']}: {c['current_surface']:.0f} → {c['new_surface']:.0f} "
                f"({c['change_pct']*100:+.1f}%{' — CLAMPED by safety limit' if c['bounded'] else ''})")
        lines.append(
            f"Expected post-VRR {rec['expected_post_vrr']:.2f} (target {rec['target_vrr']:.2f}, "
            f"rho={response_factor:.2f} from {n_adjustments} prior adjustments).")
        if rec.get("any_bounded"):
            lines.append("Safety-bounded: will not fully reach target — escalate if a larger change is needed.")
    else:
        lines.append("No injection change recommended (within tolerance or no adjustable injectors).")
    if precedent:
        lines.append(f"Precedent: {precedent['summary']}")
    confidence = ("low" if not anomaly.actionable else
                  "high" if (precedent and n_adjustments >= 3) else "medium")
    lines.append(f"Confidence: {confidence}. Draft only — analyst → RM → site approval required.")

    return {
        "pattern_id": pattern_id, "pattern_name": pattern_name,
        "vrr_date": anomaly.vrr_date, "anomaly_kind": anomaly.kind,
        "severity": anomaly.severity, "anomaly_detail": anomaly.detail,
        "driver": driver,
        "action_type": ("investigate_inputs" if not anomaly.actionable else
                        rec.get("direction", "none") if actionable else "none"),
        "recommendation": rec if actionable else None,
        "precedent": precedent, "confidence": confidence,
        "narrative": "\n".join(lines),
    }
