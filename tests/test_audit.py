"""The input-audit gate decides whether a VRR may be acted on at all."""
from __future__ import annotations

from vrr_agent_open.core.audit import (DATA_ARTIFACT, INCONCLUSIVE, REAL_SIGNAL,
                                       assess_inputs, route_for)

CLEAN = dict(n_rows=150, pvt_methods=["interpolated", "exact"], recompute_difference=0.0)


def test_clean_inputs_are_actionable():
    r = assess_inputs(**CLEAN)
    assert r["verdict"] == REAL_SIGNAL and r["actionable"] and not r["findings"]


def test_extrapolated_pvt_is_disqualifying():
    r = assess_inputs(**{**CLEAN, "pvt_methods": ["interpolated", "extrapolated"]})
    assert r["verdict"] == DATA_ARTIFACT and not r["actionable"]
    assert r["findings"][0]["code"] == "pvt_not_measured"


def test_each_high_severity_rule_blocks():
    for kwargs in ({"n_missing_pressure": 3}, {"n_missing_factor": 12},
                   {"n_allocation_over_one": 1}, {"recompute_difference": 1e-3}):
        r = assess_inputs(**{**CLEAN, **kwargs})
        assert r["verdict"] == DATA_ARTIFACT, kwargs


def test_recompute_difference_within_tolerance_is_fine():
    assert assess_inputs(**{**CLEAN, "recompute_difference": 1e-12})["verdict"] == REAL_SIGNAL


def test_medium_findings_alone_are_inconclusive_not_artifact():
    r = assess_inputs(**{**CLEAN, "n_allocated_without_volumes": 2})
    assert r["verdict"] == INCONCLUSIVE and not r["actionable"]
    r2 = assess_inputs(**{**CLEAN, "allocation_age_days": 2000})
    assert r2["verdict"] == INCONCLUSIVE


def test_fresh_allocation_is_not_flagged():
    assert assess_inputs(**{**CLEAN, "allocation_age_days": 200})["verdict"] == REAL_SIGNAL


def test_no_rows_is_inconclusive_rather_than_clean():
    r = assess_inputs(n_rows=0)
    assert r["verdict"] == INCONCLUSIVE and "no contributing rows" in r["summary"]


def test_routing_matches_the_verdict():
    assert route_for(REAL_SIGNAL)["action_type"] is None
    assert route_for(DATA_ARTIFACT) == {
        "action_type": "fix_inputs", "owner_role": "data_steward",
        "note": "fix the source data and re-run the build; no valve change proposed"}
    assert route_for(INCONCLUSIVE)["owner_role"] == "analyst"
