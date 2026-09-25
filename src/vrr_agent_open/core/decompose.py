"""ΔVRR attribution — exact additive decomposition (pure, no I/O).

VRR = INJ_RES / PROD_RES, and both sides are sums of per-term reservoir volumes
(oil_res, water_res, free_gas_res / water_inj_res, gas_inj_res). When VRR moves
between two periods the analyst's first question is *which term moved it* — this
module answers that deterministically, so the LLM only ever reads the ranking it
produced (see ``core.faithfulness``).

Two nested steps, both **exact** (contributions sum to ΔVRR, no residual):

1. side split (LMDI, log-mean):
       ΔVRR              = L(V0,V1) · ln(V1/V0)                     [log-mean identity]
       ln(V1/V0)         = ln(I1/I0) − ln(P1/P0)
   ⇒   ΔVRR_injection    = L(V0,V1) · ln(I1/I0)
       ΔVRR_production   = −L(V0,V1) · ln(P1/P0)

2. term split within each side:
   * all endpoints positive → LMDI weights  L(x0,x1)/L(X0,X1) · ln(x1/x0)
   * otherwise (a term is zero at one end, or ``free_gas_res`` is negative — both
     legal here and both outside the log domain) → share-of-change weights
     (x1−x0)/(X1−X0), which are exact for a sum and safe at zero/negative.

The chosen weighting is reported per side as ``method`` so the audit trail says how
the split was made. Contributions are signed: positive = pushed VRR up.
"""
from __future__ import annotations

import math

PROD_TERMS = ("oil_res", "water_res", "free_gas_res")
INJ_TERMS = ("water_inj_res", "gas_inj_res")

# Human labels for narration + the faithfulness gate's vocabulary.
TERM_LABELS = {
    "oil_res": "oil production",
    "water_res": "water production",
    "free_gas_res": "free gas production",
    "water_inj_res": "water injection",
    "gas_inj_res": "gas injection",
}


def _num(v, d=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def log_mean(a: float, b: float) -> float:
    """Logarithmic mean L(a,b) = (b−a)/ln(b/a); L(a,a)=a. 0 if either side ≤ 0."""
    if a <= 0 or b <= 0:
        return 0.0
    if a == b:
        return a
    return (b - a) / math.log(b / a)


def _split_side(terms: tuple, a: dict, b: dict, total_a: float, total_b: float,
                side_total: float) -> tuple[dict, str]:
    """Distribute one side's ΔVRR across its terms. Returns (contributions, method)."""
    positive = all(_num(a.get(t)) > 0 and _num(b.get(t)) > 0 for t in terms)
    if positive and total_a > 0 and total_b > 0 and total_a != total_b:
        lt = log_mean(total_a, total_b)
        weights = {t: (log_mean(_num(a[t]), _num(b[t])) / lt)
                      * math.log(_num(b[t]) / _num(a[t])) for t in terms}
        ln_total = math.log(total_b / total_a)
        if ln_total != 0:                       # normalise so the split is exact
            scale = side_total / ln_total
            return {t: w * scale for t, w in weights.items()}, "lmdi"
    delta_total = total_b - total_a
    if delta_total == 0:
        return {t: 0.0 for t in terms}, "no_change"
    return ({t: side_total * (_num(b.get(t)) - _num(a.get(t))) / delta_total
             for t in terms}, "share_of_change")


def decompose_vrr(a: dict, b: dict) -> dict:
    """Attribute the VRR change from period ``a`` to period ``b``.

    ``a``/``b`` are term-total dicts (the five ``*_res`` sums for the period). Returns
    the two VRRs, the signed per-term contributions (summing to ΔVRR), the ranked
    drivers, and the split method — everything the narration is allowed to say.
    """
    prod_a = sum(_num(a.get(t)) for t in PROD_TERMS)
    prod_b = sum(_num(b.get(t)) for t in PROD_TERMS)
    inj_a = sum(_num(a.get(t)) for t in INJ_TERMS)
    inj_b = sum(_num(b.get(t)) for t in INJ_TERMS)
    if prod_a <= 0 or prod_b <= 0:
        return {"ok": False, "reason": "production reservoir volume is zero; VRR undefined"}

    vrr_a, vrr_b = inj_a / prod_a, inj_b / prod_b
    d_vrr = vrr_b - vrr_a
    lv = log_mean(vrr_a, vrr_b) or vrr_a        # VRR>0 in practice; guard anyway

    inj_side = lv * math.log(inj_b / inj_a) if inj_a > 0 and inj_b > 0 else (
        d_vrr if prod_a == prod_b else 0.0)
    prod_side = d_vrr - inj_side                # exact by construction

    inj_c, inj_method = _split_side(INJ_TERMS, a, b, inj_a, inj_b, inj_side)
    prod_c, prod_method = _split_side(PROD_TERMS, a, b, prod_a, prod_b, prod_side)

    contrib = {**inj_c, **prod_c}
    deltas = {t: _num(b.get(t)) - _num(a.get(t)) for t in (*INJ_TERMS, *PROD_TERMS)}
    ranked = sorted(contrib.items(), key=lambda kv: abs(kv[1]), reverse=True)
    denom = sum(abs(v) for v in contrib.values()) or 1.0
    # `delta` is the term's OWN volume change; `contribution` is its effect on VRR.
    # They differ in sign on the production side — the gate needs both.
    drivers = [{"term": t, "label": TERM_LABELS[t], "contribution": v,
                "delta": deltas[t], "share": abs(v) / denom,
                "direction": "increased VRR" if v > 0 else "decreased VRR"}
               for t, v in ranked]
    return {
        "ok": True, "vrr_a": vrr_a, "vrr_b": vrr_b, "d_vrr": d_vrr,
        "prod_res_a": prod_a, "prod_res_b": prod_b,
        "inj_res_a": inj_a, "inj_res_b": inj_b,
        "side_contributions": {"injection": inj_side, "production": prod_side},
        "contributions": contrib, "term_deltas": deltas, "drivers": drivers,
        "dominant_driver": drivers[0]["term"] if drivers else None,
        "method": {"injection": inj_method, "production": prod_method,
                   "side_split": "lmdi_log_mean"},
        "check": {"sum_of_contributions": sum(contrib.values()), "d_vrr": d_vrr},
    }
