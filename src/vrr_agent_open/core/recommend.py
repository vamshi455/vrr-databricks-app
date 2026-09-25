"""Deterministic VRR valve-adjustment recommendation engine (design §5.4C-D).

The magnitude of a recommended injection change is COMPUTED from physics, not guessed
by the LLM — same trust principle as physics.py/tools.py. The LLM only narrates the
result. Pure Python (no I/O) so it unit-tests off-cluster; the anomaly/approval jobs
supply the data (pattern_vrr, completion_contrib injectors, pattern_memory, safety_limits).

Steer VRR toward target by adjusting injection (VRR = INJ_RES / PROD_RES):
  1. physics target        INJ_RES_target = target_VRR * PROD_RES
     required change        d_inj_res = INJ_RES_target - INJ_RES_current
  2. precedent calibration  d_reco = d_inj_res / rho     (rho = learned per-pattern gain)
  3. to surface rate        d_surface_i = d_reco_i / (FACTOR_i * Bw_inj_i)
  4. safety bound           clamp each injector's %% change to max_inj_rate_change_pct
  5. expected post-VRR      current_vrr + rho * applied_inj_res / PROD_RES
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

DEFAULT_MAX_CHANGE_PCT = 0.15   # 15% per action unless safety_limits says otherwise
ON_TARGET_TOL = 0.02            # within ±0.02 VRR = no action


@dataclass(frozen=True)
class InjectorState:
    """Current state of one injector completion (from completion_contrib)."""
    completion_id: str
    factor: float
    bw_inj: float
    water_inj_surface: float    # current surface injection rate (e.g. bwpd)
    inj_res: float              # current reservoir-bbl injection contribution


@dataclass(frozen=True)
class InjectorChange:
    completion_id: str
    current_surface: float
    delta_surface: float
    new_surface: float
    change_pct: float
    bounded: bool               # clamped by the safety limit


def _num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def recommend_injection_change(
    *, prod_res: float, inj_res: float, target_vrr: float,
    injectors: list[InjectorState], response_factor: float = 1.0,
    max_change_pct: float = DEFAULT_MAX_CHANGE_PCT,
) -> dict:
    """Recommend a bounded, precedent-calibrated injection change to steer VRR->target."""
    prod_res, inj_res = _num(prod_res), _num(inj_res)
    if prod_res <= 0:
        return {"ok": False, "reason": "production reservoir volume is zero; VRR undefined"}
    if not injectors:
        return {"ok": False, "reason": "no injector completions to adjust"}

    current_vrr = inj_res / prod_res
    rho = _num(response_factor, 1.0)
    if rho <= 0:
        rho = 1.0

    if abs(current_vrr - target_vrr) <= ON_TARGET_TOL:
        return {"ok": True, "direction": "none", "current_vrr": current_vrr,
                "target_vrr": target_vrr, "response_factor": rho,
                "injector_changes": [], "expected_post_vrr": current_vrr,
                "note": "VRR is within tolerance of target; no adjustment recommended."}

    inj_res_target = target_vrr * prod_res
    d_inj_res_physics = inj_res_target - inj_res          # <0 = cut, >0 = increase
    d_inj_res_reco = d_inj_res_physics / rho              # precedent calibration
    total_inj_res = sum(_num(i.inj_res) for i in injectors) or inj_res or 1.0

    changes, applied_inj_res = [], 0.0
    any_bounded = False
    for inj in injectors:
        share = _num(inj.inj_res) / total_inj_res if total_inj_res else 1.0 / len(injectors)
        d_inj_res_i = d_inj_res_reco * share
        denom = _num(inj.factor) * _num(inj.bw_inj)
        if denom == 0 or _num(inj.water_inj_surface) == 0:
            continue                                       # can't scale this injector
        d_surface_i = d_inj_res_i / denom
        pct = d_surface_i / _num(inj.water_inj_surface)
        clamped = max(-max_change_pct, min(max_change_pct, pct))
        bounded = clamped != pct
        any_bounded = any_bounded or bounded
        applied_surface = clamped * _num(inj.water_inj_surface)
        applied_inj_res += applied_surface * denom         # back to reservoir bbl
        changes.append(InjectorChange(
            completion_id=inj.completion_id, current_surface=_num(inj.water_inj_surface),
            delta_surface=applied_surface, new_surface=_num(inj.water_inj_surface) + applied_surface,
            change_pct=clamped, bounded=bounded))

    expected_post_vrr = current_vrr + rho * applied_inj_res / prod_res
    direction = ("reduce_injection" if d_inj_res_reco < 0
                 else "increase_injection" if d_inj_res_reco > 0 else "none")
    return {
        "ok": True, "current_vrr": current_vrr, "target_vrr": target_vrr,
        "direction": direction, "response_factor": rho,
        "d_inj_res_physics": d_inj_res_physics, "d_inj_res_recommended": d_inj_res_reco,
        "injector_changes": [c.__dict__ for c in changes],
        "any_bounded": any_bounded, "exceeds_safety": any_bounded,
        "expected_post_vrr": expected_post_vrr,
        "note": ("Recommendation clamped by safety limits — expected VRR will not fully reach "
                 "target; escalate for a larger change." if any_bounded else
                 "Recommended change is within safety limits."),
    }


def update_response_factor(rho: float, predicted_dvrr: float, actual_dvrr: float,
                           alpha: float = 0.3) -> float:
    """Learning: EMA-update the per-pattern response factor from a realized outcome.

    rho = actual VRR move / predicted VRR move. rho>1 => pattern over-responds (temper
    future recs); rho<1 => under-responds (strengthen). Guards divide-by-zero and clamps
    to a sane range so a single odd outcome can't destabilize future recommendations.
    """
    predicted_dvrr = _num(predicted_dvrr)
    if predicted_dvrr == 0:
        return _num(rho, 1.0)
    ratio = _num(actual_dvrr) / predicted_dvrr
    ratio = max(0.2, min(5.0, ratio))
    new = (1 - alpha) * _num(rho, 1.0) + alpha * ratio
    return max(0.3, min(3.0, new))


def find_precedent(history: list[dict], driver: Optional[str] = None,
                   direction: Optional[str] = None) -> Optional[dict]:
    """Case-based memory: the most recent EXECUTED adjustment matching the same driver
    (and optionally direction), to cite as precedent ("last time X -> VRR went to Y")."""
    cand = [h for h in history if (h.get("outcome") == "executed")
            and (driver is None or h.get("driver") == driver)
            and (direction is None or h.get("change_type") == direction)]
    if not cand:
        return None
    best = max(cand, key=lambda h: str(h.get("ts") or ""))
    return {
        "vrr_date": str(best.get("vrr_date")), "driver": best.get("driver"),
        "change_type": best.get("change_type"), "d_surface_pct": best.get("d_surface_pct"),
        "pre_vrr": best.get("pre_vrr"), "actual_post_vrr": best.get("actual_post_vrr"),
        "summary": (f"Last time ({best.get('vrr_date')}) driver '{best.get('driver')}' triggered a "
                    f"{best.get('change_type')} of {_num(best.get('d_surface_pct'))*100:.0f}%% → "
                    f"VRR {_num(best.get('pre_vrr')):.2f} → {_num(best.get('actual_post_vrr')):.2f}."),
    }
