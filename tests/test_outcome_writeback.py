"""Outcome write-back decision logic — off-DB.

The job's SQL is I/O; what to *do* with a later (or missing) monthly VRR is pure, so
the skip / fill / ρ-update cases live here the same way `tests/test_recommend.py`
covers `update_response_factor` without Postgres.
"""
from datetime import date

import pytest

from vrr_agent_open.core.recommend import update_response_factor
from vrr_agent_open.pipeline import outcome_writeback as W


def test_pick_next_period_is_the_earliest_strictly_later_month():
    periods = [date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1), date(2026, 4, 1)]
    assert W.pick_next_period(periods, date(2026, 2, 1)) == date(2026, 3, 1)
    # ISO strings and datetimes are accepted — the job sees both from psycopg / JSON
    assert W.pick_next_period(["2026-01-01", "2026-03-01"], "2026-01-01") == date(2026, 3, 1)


def test_pick_next_period_skips_a_gap_and_waits_when_nothing_later_exists():
    # May is missing; the next *available* period after April is June, not a invented May.
    periods = [date(2026, 4, 1), date(2026, 6, 1)]
    assert W.pick_next_period(periods, date(2026, 4, 1)) == date(2026, 6, 1)
    # The adjustment's own month does not count as observed post-VRR.
    assert W.pick_next_period(periods, date(2026, 6, 1)) is None
    assert W.pick_next_period([], date(2026, 4, 1)) is None


def test_vrr_deltas_are_post_minus_pre():
    assert W.vrr_deltas(1.18, 1.06, 1.08) == pytest.approx((-0.12, -0.10))
    assert W.vrr_deltas(None, 1.06, 1.08) is None
    assert W.vrr_deltas(1.18, None, 1.08) is None
    assert W.vrr_deltas(1.18, 1.06, None) is None


def test_no_later_period_is_a_noop_and_does_not_touch_rho():
    plan = W.plan_writeback(
        action_id="ACT-1", id_pattern="PUNITY",
        pre_vrr=1.18, predicted_post_vrr=1.06, later_vrr=None, current_rho=1.0)
    assert plan.actual_post_vrr is None
    assert plan.skip_reason == "no later period"
    assert plan.new_rho is None
    assert plan.old_rho == 1.0


def test_observed_vrr_fills_actual_and_ema_updates_rho():
    # Same numbers as test_response_factor_learning_ema: predicted -0.07, actual -0.091.
    plan = W.plan_writeback(
        action_id="ACT-1", id_pattern="PUNITY",
        pre_vrr=1.10, predicted_post_vrr=1.03, later_vrr=1.009, current_rho=1.0)
    assert plan.actual_post_vrr == pytest.approx(1.009)
    assert plan.skip_reason is None
    assert plan.predicted_dvrr == pytest.approx(-0.07)
    assert plan.actual_dvrr == pytest.approx(-0.091)
    expected = update_response_factor(1.0, -0.07, -0.091, alpha=0.3)
    assert plan.new_rho == pytest.approx(expected)
    assert 1.0 < plan.new_rho < 1.3          # over-responded → ρ rises


def test_missing_prediction_still_fills_actual_but_leaves_rho_alone():
    plan = W.plan_writeback(
        action_id="ACT-1", id_pattern="PUNITY",
        pre_vrr=1.18, predicted_post_vrr=None, later_vrr=1.08, current_rho=1.0)
    assert plan.actual_post_vrr == pytest.approx(1.08)
    assert plan.new_rho is None


def test_no_pattern_memory_row_still_fills_actual_but_leaves_rho_alone():
    plan = W.plan_writeback(
        action_id="ACT-1", id_pattern="PUNITY",
        pre_vrr=1.18, predicted_post_vrr=1.06, later_vrr=1.08, current_rho=None)
    assert plan.actual_post_vrr == pytest.approx(1.08)
    assert plan.new_rho is None


def test_zero_predicted_move_keeps_rho_via_the_existing_guard():
    plan = W.plan_writeback(
        action_id="ACT-1", id_pattern="PUNITY",
        pre_vrr=1.00, predicted_post_vrr=1.00, later_vrr=1.05, current_rho=1.2)
    assert plan.predicted_dvrr == 0.0
    assert plan.new_rho == pytest.approx(1.2)


def test_two_outcomes_for_one_pattern_chain_the_ema():
    first = W.plan_writeback(
        action_id="A", id_pattern="P",
        pre_vrr=1.18, predicted_post_vrr=1.06, later_vrr=1.08, current_rho=1.0)
    second = W.plan_writeback(
        action_id="B", id_pattern="P",
        pre_vrr=1.08, predicted_post_vrr=1.02, later_vrr=1.01, current_rho=first.new_rho)
    assert first.new_rho != pytest.approx(1.0)
    assert second.new_rho != pytest.approx(first.new_rho)


def test_job_sql_selects_executed_nulls_and_the_next_monthly_period():
    """Pin the lookup the job actually runs, so a rewrite cannot silently switch grain
    or start treating the adjustment's own month as the observed post-VRR."""
    assert "outcome = 'executed'" in W._PENDING_SQL
    assert "actual_post_vrr IS NULL" in W._PENDING_SQL
    assert "grain = 'monthly'" in W._NEXT_PERIOD_SQL
    assert "vrr_date > %(d)s" in W._NEXT_PERIOD_SQL
    assert W.GRAIN == "monthly"
