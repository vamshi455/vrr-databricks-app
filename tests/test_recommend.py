"""Unit tests for the deterministic recommendation engine — off-cluster.

Scenario: a UNITY-like pattern, PROD_RES=1400, one injector (1500 surface -> 1500
reservoir bbl), so VRR = 1500/1400 = 1.071 (over-replicating vs target 1.0).
"""
import pytest

from vrr_agent_open.core import recommend as R


def _inj(surface=1500.0, inj_res=1500.0, factor=1.0, bw_inj=1.0, cid="INJ_WELL_001"):
    return R.InjectorState(completion_id=cid, factor=factor, bw_inj=bw_inj,
                           water_inj_surface=surface, inj_res=inj_res)


def test_over_replicating_recommends_a_calibrated_cut_that_hits_target():
    r = R.recommend_injection_change(prod_res=1400, inj_res=1500, target_vrr=1.0,
                                     injectors=[_inj()])
    assert r["ok"] and r["direction"] == "reduce_injection"
    ch = r["injector_changes"][0]
    assert ch["change_pct"] == pytest.approx(-100 / 1500, abs=1e-6)   # ~6.67% cut
    assert not ch["bounded"] and not r["exceeds_safety"]
    assert r["expected_post_vrr"] == pytest.approx(1.0, abs=1e-6)      # reaches target


def test_response_factor_tempers_magnitude_but_still_reaches_target():
    base = R.recommend_injection_change(prod_res=1400, inj_res=1500, target_vrr=1.0,
                                        injectors=[_inj()], response_factor=1.0)
    over = R.recommend_injection_change(prod_res=1400, inj_res=1500, target_vrr=1.0,
                                        injectors=[_inj()], response_factor=1.3)
    # a pattern that over-responds (rho=1.3) gets a SMALLER recommended cut...
    assert abs(over["injector_changes"][0]["change_pct"]) < abs(base["injector_changes"][0]["change_pct"])
    # ...yet still expected to reach target (because it over-responds)
    assert over["expected_post_vrr"] == pytest.approx(1.0, abs=1e-6)


def test_safety_bound_clamps_and_flags_escalation():
    # VRR 1.5, needs a huge cut -> clamped to 15%, target not reached
    r = R.recommend_injection_change(prod_res=1400, inj_res=2100, target_vrr=1.0,
                                     injectors=[_inj(surface=2100, inj_res=2100)])
    ch = r["injector_changes"][0]
    assert ch["change_pct"] == pytest.approx(-0.15) and ch["bounded"]
    assert r["exceeds_safety"] and r["expected_post_vrr"] > r["target_vrr"]  # under-corrected


def test_under_replicating_recommends_increase():
    r = R.recommend_injection_change(prod_res=1400, inj_res=1120, target_vrr=1.0,
                                     injectors=[_inj(surface=1120, inj_res=1120)])
    assert r["direction"] == "increase_injection" and r["d_inj_res_recommended"] > 0


def test_on_target_recommends_nothing():
    r = R.recommend_injection_change(prod_res=1400, inj_res=1400, target_vrr=1.0,
                                     injectors=[_inj(inj_res=1400)])
    assert r["direction"] == "none" and r["injector_changes"] == []


def test_response_factor_learning_ema():
    # predicted a -0.07 VRR move, actual was -0.091 (over-responded 1.3x) -> rho rises
    rho = R.update_response_factor(1.0, predicted_dvrr=-0.07, actual_dvrr=-0.091, alpha=0.3)
    assert 1.0 < rho < 1.3
    # divide-by-zero guard
    assert R.update_response_factor(1.2, predicted_dvrr=0.0, actual_dvrr=0.5) == 1.2
    # clamped to a sane range
    assert 0.3 <= R.update_response_factor(1.0, 0.01, -100.0) <= 3.0


def test_find_precedent_picks_recent_same_driver():
    hist = [
        {"vrr_date": "2026-01-01", "driver": "free gas", "change_type": "reduce_injection",
         "d_surface_pct": -0.08, "pre_vrr": 1.12, "actual_post_vrr": 1.03, "outcome": "executed", "ts": "2026-01-02"},
        {"vrr_date": "2026-04-01", "driver": "free gas", "change_type": "reduce_injection",
         "d_surface_pct": -0.07, "pre_vrr": 1.10, "actual_post_vrr": 1.02, "outcome": "executed", "ts": "2026-04-02"},
        {"vrr_date": "2026-03-01", "driver": "water injection", "change_type": "reduce_injection",
         "d_surface_pct": -0.05, "pre_vrr": 1.05, "actual_post_vrr": 1.01, "outcome": "executed", "ts": "2026-03-02"},
    ]
    p = R.find_precedent(hist, driver="free gas")
    assert p["vrr_date"] == "2026-04-01"           # most recent free-gas case
    assert "1.10" in p["summary"] and "1.02" in p["summary"]
    assert R.find_precedent(hist, driver="nonexistent") is None
