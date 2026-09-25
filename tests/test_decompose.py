"""ΔVRR attribution must be EXACT — contributions always sum to the VRR change."""
from __future__ import annotations

import pytest

from vrr_agent_open.core.decompose import INJ_TERMS, PROD_TERMS, decompose_vrr, log_mean

A = {"oil_res": 1000.0, "water_res": 3000.0, "free_gas_res": 200.0,
     "water_inj_res": 4200.0, "gas_inj_res": 0.0}


def _vrr(t):
    return sum(t[k] for k in INJ_TERMS) / sum(t[k] for k in PROD_TERMS)


def test_log_mean_bounds():
    assert log_mean(2.0, 2.0) == 2.0
    assert 2.0 < log_mean(2.0, 4.0) < 4.0            # between the two values
    assert log_mean(0.0, 4.0) == 0.0                 # outside the log domain


def test_injection_only_change_is_attributed_to_injection():
    b = {**A, "water_inj_res": 5200.0}
    d = decompose_vrr(A, b)
    assert d["ok"] and d["dominant_driver"] == "water_inj_res"
    assert d["contributions"]["water_inj_res"] > 0
    assert d["side_contributions"]["production"] == pytest.approx(0.0, abs=1e-12)


def test_contributions_sum_exactly_to_delta_vrr():
    b = {"oil_res": 900.0, "water_res": 3400.0, "free_gas_res": 150.0,
         "water_inj_res": 5000.0, "gas_inj_res": 120.0}
    d = decompose_vrr(A, b)
    assert sum(d["contributions"].values()) == pytest.approx(d["d_vrr"], rel=1e-9)
    assert d["d_vrr"] == pytest.approx(_vrr(b) - _vrr(A), rel=1e-12)


def test_exact_with_a_zero_term_and_a_negative_free_gas():
    """free_gas_res may be negative (Rs·OIL > produced gas) and gas_inj may be 0 —
    both are outside the log domain, so the split falls back to share-of-change."""
    b = {**A, "free_gas_res": -400.0, "gas_inj_res": 300.0, "water_inj_res": 4600.0}
    d = decompose_vrr(A, b)
    assert sum(d["contributions"].values()) == pytest.approx(d["d_vrr"], rel=1e-9)
    assert d["method"]["production"] == "share_of_change"


def test_no_change_gives_zero_contributions():
    d = decompose_vrr(A, dict(A))
    assert d["d_vrr"] == pytest.approx(0.0)
    assert all(v == pytest.approx(0.0) for v in d["contributions"].values())


def test_drivers_ranked_by_absolute_contribution_with_shares():
    b = {**A, "water_inj_res": 5000.0, "water_res": 3100.0}
    d = decompose_vrr(A, b)
    mags = [abs(x["contribution"]) for x in d["drivers"]]
    assert mags == sorted(mags, reverse=True)
    assert sum(x["share"] for x in d["drivers"]) == pytest.approx(1.0)


def test_zero_production_is_undefined_not_zero():
    assert decompose_vrr({**A, "oil_res": 0, "water_res": 0, "free_gas_res": 0}, A)["ok"] is False
