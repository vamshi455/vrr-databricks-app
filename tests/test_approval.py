"""Unit tests for the approval-workflow state machine — off-cluster."""

from vrr_agent_open.core import approval as AP


def test_forward_path_is_ordered():
    assert AP.next_stage("draft") == "analyst"
    assert AP.next_stage("analyst") == "rm"
    assert AP.next_stage("rm") == "site"
    assert AP.next_stage("site") == "executed"


def test_terminal_stages_have_no_next():
    assert AP.next_stage("executed") is None
    assert AP.next_stage("rejected") is None
    assert AP.next_stage("bogus") is None


def test_can_transition_only_allows_next_or_reject():
    assert AP.can_transition("draft", "analyst")
    assert not AP.can_transition("draft", "rm")        # can't skip a stage
    assert not AP.can_transition("draft", "site")
    assert AP.can_transition("site", "executed")
    assert AP.can_transition("analyst", "rejected")    # reject from any live stage
    assert not AP.can_transition("executed", "rejected")  # terminal is frozen
    assert not AP.can_transition("rejected", "analyst")


def test_build_adjustment_row_pulls_predicted_numbers():
    q = {"action_id": "PUNITY|2026-04-01|out_of_band", "pattern_id": "PUNITY",
         "pattern_name": "UNITY", "vrr_date": "2026-04-01", "driver": "water injection",
         "anomaly_detail": "over-replicating VRR 1.31", "action_type": "reduce_injection"}
    rec = {"current_vrr": 1.31, "expected_post_vrr": 1.02, "direction": "reduce_injection",
           "d_inj_res_recommended": -420.0,
           "injector_changes": [{"change_pct": -0.10}, {"change_pct": -0.12}]}
    row = AP.build_adjustment_row(q, rec, approver="rm@acme.com")
    assert row["id_pattern"] == "PUNITY"   # production key name (ID_PATTERN)
    assert row["change_type"] == "reduce_injection"
    assert row["pre_vrr"] == 1.31 and row["predicted_post_vrr"] == 1.02
    assert row["d_inj_res_bbl"] == -420.0
    assert row["d_surface_pct"] == -0.11          # mean of the two injector pcts
    assert row["actual_post_vrr"] is None          # filled later from the field
    assert row["decision"] == "approved" and row["outcome"] == "executed"
    assert row["approved_by"] == "rm@acme.com"


def test_build_adjustment_row_survives_missing_recommendation():
    q = {"action_id": "X", "pattern_id": "P", "action_type": "investigate_inputs"}
    row = AP.build_adjustment_row(q, None, approver="a@b.com", decision="rejected",
                                  outcome="skipped")
    assert row["change_type"] == "investigate_inputs"
    assert row["d_surface_pct"] is None and row["predicted_post_vrr"] is None
    assert row["decision"] == "rejected" and row["outcome"] == "skipped"
