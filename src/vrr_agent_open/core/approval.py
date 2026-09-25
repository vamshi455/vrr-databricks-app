"""Deterministic approval-workflow state machine for the action_queue (design §5.4B / §6).

Pure Python (no I/O) so it unit-tests off-cluster — same rule as the rest of `core/`.
The approval app calls these to decide legal stage transitions and to assemble the
`adjustment_history` row written when a recommendation is executed. The *policy*
(who may act, what's next) lives here; the app only renders it and runs the SQL.

Staged approval (guardrail §6 — advisory only, multi-level human sign-off):

    draft → analyst → rm → site → executed
                  ↘──────────────↗
                     (any stage) → rejected   [terminal]

The agent writes `draft`; every forward step and the terminal states are HUMAN acts.
"""
from __future__ import annotations

# Forward path (index order matters). `executed` / `rejected` are terminal.
STAGES = ["draft", "analyst", "rm", "site", "executed"]
TERMINAL = {"executed", "rejected"}


def next_stage(stage: str) -> str | None:
    """The stage a forward-approval moves to, or None if terminal/unknown."""
    if stage in TERMINAL or stage not in STAGES:
        return None
    i = STAGES.index(stage)
    return STAGES[i + 1] if i + 1 < len(STAGES) else None


def can_transition(frm: str, to: str) -> bool:
    """True iff `to` is the legal next forward stage or a rejection from a live stage."""
    if frm in TERMINAL:
        return False
    if to == "rejected":
        return True
    return to == next_stage(frm)


def _num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def build_adjustment_row(queue_row: dict, recommendation: dict | None,
                         approver: str, decision: str = "approved",
                         outcome: str = "executed") -> dict:
    """Assemble an `adjustment_history` row from an executed queue draft.

    Pulls the predicted numbers from the recommendation payload so the learning loop
    (`pipeline/outcome_writeback.py`, `make writeback`) can later compare predicted vs
    actual ΔVRR. `actual_post_vrr` is left None — the write-back job fills it from the
    next monthly curated VRR after this period.
    """
    rec = recommendation or {}
    changes = rec.get("injector_changes") or []
    avg_pct = (sum(_num(c.get("change_pct")) for c in changes) / len(changes)) if changes else None
    return {
        "action_id": queue_row.get("action_id"),
        "id_pattern": queue_row.get("id_pattern") or queue_row.get("pattern_id"),
        "pattern_name": queue_row.get("pattern_name"),
        "vrr_date": queue_row.get("vrr_date"),
        "driver": queue_row.get("driver"),
        "anomaly": queue_row.get("anomaly_detail"),
        "change_type": rec.get("direction") or queue_row.get("action_type"),
        "d_inj_res_bbl": rec.get("d_inj_res_recommended"),
        "d_surface_pct": avg_pct,
        "pre_vrr": rec.get("current_vrr"),
        "predicted_post_vrr": rec.get("expected_post_vrr"),
        "actual_post_vrr": None,          # filled later from the field
        "decision": decision,
        "approved_by": approver,
        "outcome": outcome,
    }
