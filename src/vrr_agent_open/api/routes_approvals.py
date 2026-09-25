"""The approval chain — draft → analyst → rm → site → executed.

**Role enforcement lives here, and the role comes from the TOKEN.** Three versions of
this check, each fixing the previous one:

  1. Streamlit hid the approve button when your role did not match the stage — UX, not
     a control; anyone could POST the transition.
  2. The first FastAPI cut re-checked the role server-side, but read it from the REQUEST
     BODY. The state machine was enforced against a role the caller chose.
  3. Now the role is a signed JWT claim (`api/auth.py`). To act as the site engineer you
     sign in as the site engineer; the body cannot say otherwise, because it no longer
     carries a role at all.

Advancing to `executed` also writes `vrr_agent.adjustment_history` — the row the ρ
learning loop reads back. That write and the stage update happen for the same action_id
in one request; if the history insert fails, the stage does not move.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..core import approval as AP
from .auth import CurrentUser
from .db import execute, query

router = APIRouter(prefix="/api", tags=["approvals"])

# Which role may advance an item that currently sits at each stage.
APPROVER_FOR_STAGE = {"draft": "analyst", "analyst": "rm", "rm": "site", "site": "site"}
ROLES = ["analyst", "rm", "site"]

_INSERT_ADJUSTMENT = (
    "INSERT INTO vrr_agent.adjustment_history (action_id, id_pattern, pattern_name,"
    " vrr_date, driver, anomaly, change_type, d_inj_res_bbl, d_surface_pct, pre_vrr,"
    " predicted_post_vrr, actual_post_vrr, decision, approved_by, outcome) VALUES"
    " (%(action_id)s,%(id_pattern)s,%(pattern_name)s,%(vrr_date)s,%(driver)s,%(anomaly)s,"
    " %(change_type)s,%(d_inj_res_bbl)s,%(d_surface_pct)s,%(pre_vrr)s,"
    " %(predicted_post_vrr)s,%(actual_post_vrr)s,%(decision)s,%(approved_by)s,%(outcome)s)")


@router.get("/stages")
def stages() -> dict:
    """The state machine, so the UI never hard-codes it."""
    return {"stages": [*AP.STAGES, "rejected"], "roles": ROLES,
            "approver_for_stage": APPROVER_FOR_STAGE}


@router.get("/board")
def board() -> dict:
    """Every lane at once — what the swim-lane view renders.

    One query rather than six: the board is a single picture of where work is stuck, and
    six round trips would let the lanes disagree with each other mid-render.
    """
    rows = query("SELECT * FROM vrr_agent.action_queue"
                 " ORDER BY severity, created_at DESC")
    lanes: dict[str, list[dict]] = {s: [] for s in [*AP.STAGES, "rejected"]}
    for r in rows:
        lanes.setdefault(r["stage"], []).append(r)
    return {"lanes": lanes,
            "counts": {k: len(v) for k, v in lanes.items()},
            "approver_for_stage": APPROVER_FOR_STAGE,
            "order": [*AP.STAGES, "rejected"]}


@router.get("/queue")
def queue(stage: str = Query("draft")) -> list[dict]:
    return query("SELECT * FROM vrr_agent.action_queue WHERE stage=%(s)s "
                 "ORDER BY severity, created_at DESC", {"s": stage})


@router.get("/adjustments")
def adjustments(limit: int = Query(25, le=200)) -> list[dict]:
    """Executed changes — the ρ-learning input."""
    return query("SELECT action_id, pattern_name, vrr_date, change_type, d_surface_pct,"
                 " pre_vrr, predicted_post_vrr, actual_post_vrr, approved_by, ts"
                 " FROM vrr_agent.adjustment_history ORDER BY ts DESC LIMIT %(n)s",
                 {"n": limit})


def _load(action_id: str) -> dict:
    rows = query("SELECT * FROM vrr_agent.action_queue WHERE action_id=%(i)s",
                 {"i": action_id})
    if not rows:
        raise HTTPException(404, f"unknown action {action_id}")
    return rows[0]


@router.post("/queue/{action_id}/advance")
def advance(action_id: str, user: CurrentUser) -> dict:
    """Move one item to the next stage, checking the caller's TOKEN role against it."""
    item = _load(action_id)
    stage = item["stage"]
    nxt = AP.next_stage(stage)
    if not nxt:
        raise HTTPException(409, f"stage '{stage}' is terminal — no further transitions")

    needed = APPROVER_FOR_STAGE.get(stage)
    if user["role"] != needed:
        raise HTTPException(
            403, f"stage '{stage}' advances on {needed} sign-off; you are signed in as "
                 f"'{user['username']}' ({user['role']})")

    if nxt == "executed":
        # The ρ loop reads this table, so the row is written BEFORE the stage moves —
        # an executed item with no history row would silently never be learned from.
        rec = item.get("recommendation")
        if isinstance(rec, str):
            import json
            rec = json.loads(rec)
        execute(_INSERT_ADJUSTMENT,
                AP.build_adjustment_row(item, rec, approver=user["username"]))

    execute("UPDATE vrr_agent.action_queue SET stage=%(n)s, stage_by=%(u)s, stage_ts=now()"
            " WHERE action_id=%(i)s",
            {"n": nxt, "u": user["username"], "i": action_id})
    return {"action_id": action_id, "from": stage, "to": nxt, "by": user["username"],
            "wrote_adjustment_history": nxt == "executed"}


@router.post("/queue/{action_id}/reject")
def reject(action_id: str, user: CurrentUser) -> dict:
    """Any approver in the chain may reject; rejection is terminal."""
    item = _load(action_id)
    stage = item["stage"]
    if stage in ("executed", "rejected"):
        raise HTTPException(409, f"stage '{stage}' is terminal")
    needed = APPROVER_FOR_STAGE.get(stage)
    if user["role"] != needed:
        raise HTTPException(403, f"stage '{stage}' is actioned by {needed}, not "
                                 f"'{user['role']}'")
    execute("UPDATE vrr_agent.action_queue SET stage='rejected', stage_by=%(u)s,"
            " stage_ts=now() WHERE action_id=%(i)s",
            {"u": user["username"], "i": action_id})
    return {"action_id": action_id, "from": stage, "to": "rejected",
            "by": user["username"]}
