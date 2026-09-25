"""Unit tests for the deterministic anomaly detector + draft assembly — off-cluster."""

from vrr_agent_open.core import anomaly as A
from vrr_agent_open.core import recommend as R


def _hist(vrrs, extrapolated_last=False, start_month=1):
    rows = []
    for i, v in enumerate(vrrs):
        rows.append({"vrr_date": f"2026-{start_month + i:02d}-01", "vrr": v,
                     "any_extrapolated": extrapolated_last and i == len(vrrs) - 1})
    return rows


def test_in_band_stable_history_is_quiet():
    assert A.detect_anomalies(_hist([1.0, 1.02, 0.98, 1.01]), target_vrr=1.0) == []


def test_out_of_band_high_severity_and_actionable():
    out = A.detect_anomalies(_hist([1.0, 1.05, 1.31]), target_vrr=1.0)
    kinds = {a.kind for a in out}
    assert "out_of_band" in kinds
    a = next(x for x in out if x.kind == "out_of_band")
    assert a.severity == "high" and a.actionable
    assert "over-replicating" in a.detail and "1.31" in a.detail


def test_under_replicating_names_the_side():
    out = A.detect_anomalies(_hist([1.0, 0.95, 0.80]), target_vrr=1.0)
    a = next(x for x in out if x.kind == "out_of_band")
    assert "under-replicating" in a.detail


def test_sustained_drift_needs_run_and_magnitude():
    # 3 consecutive +0.04 moves = 0.12 total -> drift (still inside the band)
    out = A.detect_anomalies(_hist([0.94, 0.98, 1.02, 1.06]), target_vrr=1.0,
                             band=(0.5, 1.5))
    assert [a.kind for a in out] == ["sustained_drift"]
    assert "3 consecutive" in out[0].detail
    # same run but tiny magnitude -> quiet
    assert A.detect_anomalies(_hist([1.00, 1.01, 1.02, 1.03]), target_vrr=1.0,
                              band=(0.5, 1.5)) == []
    # sign flip breaks the run
    assert A.detect_anomalies(_hist([0.94, 1.02, 0.98, 1.06]), target_vrr=1.0,
                              band=(0.5, 1.5)) == []


def test_extrapolated_pvt_vetoes_valve_action():
    # suspect inputs: out_of_band is still reported but NOT actionable (guardrail §6)
    out = A.detect_anomalies(_hist([1.0, 1.05, 1.31], extrapolated_last=True),
                             target_vrr=1.0)
    kinds = {a.kind: a for a in out}
    assert not kinds["extrapolated_pvt"].actionable
    assert not kinds["out_of_band"].actionable


def test_pattern_band_overrides_global_band():
    # 1.15 is outside the default band but inside this pattern's learned band
    assert A.detect_anomalies(_hist([1.12, 1.15]), target_vrr=1.0,
                              band=(0.9, 1.2)) == []


def test_build_draft_actionable_cites_wells_rho_and_precedent():
    anom = A.detect_anomalies(_hist([1.0, 1.05, 1.31]), target_vrr=1.0)[0]
    rec = R.recommend_injection_change(
        prod_res=1400, inj_res=1834, target_vrr=1.0,
        injectors=[R.InjectorState("INJ_WELL_001", 1.0, 1.0, 1834, 1834)])
    prec = {"summary": "Last time (2026-03-01) driver 'water injection' triggered a "
                       "reduce_injection of 10%% → VRR 1.25 → 1.04."}
    d = A.build_draft(pattern_id="PUNITY", pattern_name="UNITY", anomaly=anom,
                      recommendation=rec, driver="water injection", precedent=prec,
                      response_factor=1.1, n_adjustments=4)
    assert d["action_type"] == "reduce_injection" and d["confidence"] == "high"
    n = d["narrative"]
    assert "INJ_WELL_001" in n and "rho=1.10" in n and "Precedent:" in n
    assert "analyst → RM → site" in n
    assert d["recommendation"]["expected_post_vrr"] is not None


def test_build_draft_suspect_inputs_never_proposes_a_change():
    anom = A.Anomaly(kind="extrapolated_pvt", severity="low", detail="suspect PVT",
                     vrr_date="2026-03-01", vrr=1.31, actionable=False)
    rec = R.recommend_injection_change(
        prod_res=1400, inj_res=1834, target_vrr=1.0,
        injectors=[R.InjectorState("INJ_WELL_001", 1.0, 1.0, 1834, 1834)])
    d = A.build_draft(pattern_id="PUNITY", pattern_name="UNITY", anomaly=anom,
                      recommendation=rec)
    assert d["action_type"] == "investigate_inputs"
    assert d["recommendation"] is None and d["confidence"] == "low"
    assert "no valve change" in d["narrative"]
