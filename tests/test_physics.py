"""Unit tests for the deterministic reservoir math (physics.py) — off-cluster."""
import math

import pytest

from vrr_agent_open.core import physics as ph


def _pts():
    # low-pressure point: higher Bg, lower Rs; high-pressure point: lower Bg, higher Rs
    return [
        ph.PVTPoint(pressure_psi=2700, bo=1.25, bw=1.02, bg=0.00090, rs=520, bw_inj=1.0, bg_inj=0.0006),
        ph.PVTPoint(pressure_psi=3100, bo=1.28, bw=1.01, bg=0.00078, rs=620, bw_inj=1.0, bg_inj=0.0006),
    ]


def test_pvt_exact_hit():
    r = ph.pvt_lookup(_pts(), 2700)
    assert r.method == ph.EXACT
    assert r.props["bg"] == pytest.approx(0.00090)


def test_pvt_interpolated_in_range():
    r = ph.pvt_lookup(_pts(), 2780)          # design's April pressure
    assert r.method == ph.INTERP
    assert r.bracket == (2700, 3100)
    # linear interp of Bg between the two points at 2780, then rounded to 5 dp
    frac = (2780 - 2700) / (3100 - 2700)
    assert r.props["bg"] == pytest.approx(round(0.00090 + frac * (0.00078 - 0.00090), 5))
    assert not r.is_low_confidence


def test_pvt_extrapolated_is_two_point_linear():
    r = ph.pvt_lookup(_pts(), 2500)          # below range -> 2-point linear extrapolation
    assert r.method == ph.EXTRAP
    assert r.is_low_confidence
    # extrapolate below using the two lower points (2700, 3100)
    expect = round(0.00090 + (0.00090 - 0.00078) * (2500 - 2700) / (2700 - 3100), 5)
    assert r.props["bg"] == pytest.approx(expect)


def test_pvt_single_point_is_closest():
    r = ph.pvt_lookup([_pts()[0]], 2500)     # only one point -> closest, low confidence
    assert r.method == ph.CLOSEST
    assert r.is_low_confidence
    assert r.props["bg"] == pytest.approx(0.00090)


def test_pvt_bg_rises_as_pressure_falls():
    """Physics sanity: lower pattern pressure -> larger Bg (Bg ≈ 1/P)."""
    lo = ph.pvt_lookup(_pts(), 2780).props["bg"]
    hi = ph.pvt_lookup(_pts(), 3000).props["bg"]
    assert lo > hi


def test_missing_pvt_points():
    r = ph.pvt_lookup([], 2800)
    assert r.method == ph.NONE
    assert r.props["bg"] is None


def test_completion_contribution_terms():
    pvt = ph.pvt_lookup(_pts(), 2780).props
    t = ph.completion_contribution(
        factor=0.5, oil=260, water=240, gas=900, water_inj=0, gas_inj=0,
        pvt=pvt, is_producer=True)
    # free_gas (scf) = GAS*1000 - Rs*OIL, times factor times Bg (rb/scf)
    rs = pvt["rs"]; bg = pvt["bg"]
    assert t.free_gas_res == pytest.approx(0.5 * (900 * 1000 - rs * 260) * bg)
    assert t.oil_res == pytest.approx(0.5 * 260 * pvt["bo"])
    assert t.inj_res == 0.0


def test_injector_has_no_free_gas_term():
    pvt = ph.pvt_lookup(_pts(), 2780).props
    t = ph.completion_contribution(
        factor=1.0, oil=0, water=0, gas=0, water_inj=1500, gas_inj=0, pvt=pvt)
    assert t.free_gas_res is None           # gated out (OIL=0 / injection row)
    assert t.prod_res == 0.0                # None free gas treated as 0 in the sum
    assert t.water_inj_res == pytest.approx(1.0 * 1500 * pvt["bw_inj"])


def test_vrr_undefined_when_no_production():
    assert ph.vrr(100, 0) is None
    assert ph.vrr(100, 50) == pytest.approx(2.0)
