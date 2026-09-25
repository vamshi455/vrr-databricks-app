"""Outcome write-back: fill actual_post_vrr and EMA-update the response factor (ρ).

The last link in the closed loop. After an adjustment is executed, the next
``make build`` produces a later monthly voidage replacement ratio (VRR); this job
copies that observation onto ``vrr_agent.adjustment_history.actual_post_vrr`` and
moves ``pattern_memory.response_factor`` toward actual/predicted via
``core.recommend.update_response_factor``.

"Next" means the earliest ``vrr_curated.pattern_vrr`` row with ``grain='monthly'``
and ``vrr_date`` strictly after the adjustment's period. A gap is fine — we wait
for whichever later month actually landed, not for a consecutive calendar month.
No later period yet is a no-op: the history row stays NULL and ρ does not move.

    make writeback
    python -m vrr_agent_open.pipeline.outcome_writeback
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Optional, Sequence

from ..core.recommend import update_response_factor

# The rest of the agent reasons on monthly VRR; daily grain is the lineage layer.
GRAIN = "monthly"

_PENDING_SQL = """
SELECT action_id, id_pattern, pattern_name, vrr_date, pre_vrr, predicted_post_vrr
  FROM vrr_agent.adjustment_history
 WHERE outcome = 'executed' AND actual_post_vrr IS NULL
 ORDER BY id_pattern, vrr_date, ts
"""

_NEXT_PERIOD_SQL = """
SELECT vrr_date, vrr_bblbbl
  FROM vrr_curated.pattern_vrr
 WHERE id_pattern = %(p)s
   AND grain = 'monthly'
   AND vrr_date > %(d)s
   AND vrr_bblbbl IS NOT NULL
 ORDER BY vrr_date ASC
 LIMIT 1
"""

_RHO_SQL = """
SELECT response_factor FROM vrr_agent.pattern_memory WHERE id_pattern = %(p)s
"""

_FILL_SQL = """
UPDATE vrr_agent.adjustment_history
   SET actual_post_vrr = %(v)s
 WHERE action_id = %(id)s AND actual_post_vrr IS NULL
"""

_RHO_UPDATE_SQL = """
UPDATE vrr_agent.pattern_memory
   SET response_factor = %(rho)s,
       n_adjustments = COALESCE(n_adjustments, 0) + 1,
       updated_at = now()
 WHERE id_pattern = %(p)s
"""


def _as_date(v) -> Optional[dt.date]:
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _num(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def pick_next_period(dates: Sequence, after) -> Optional[dt.date]:
    """Earliest curated period strictly after ``after``, or None if none has landed."""
    bound = _as_date(after)
    if bound is None:
        return None
    later = sorted(
        d for d in (_as_date(x) for x in dates) if d is not None and d > bound)
    return later[0] if later else None


def vrr_deltas(pre_vrr, predicted_post_vrr, actual_post_vrr
               ) -> Optional[tuple[float, float]]:
    """``(predicted_dvrr, actual_dvrr)``, or None if any term is missing.

    ΔVRR is post minus pre. ``update_response_factor`` already guards a zero
    predicted move (returns ρ unchanged).
    """
    pre, pred, actual = _num(pre_vrr), _num(predicted_post_vrr), _num(actual_post_vrr)
    if pre is None or pred is None or actual is None:
        return None
    return (pred - pre, actual - pre)


@dataclass(frozen=True)
class WritebackPlan:
    """What to write for one executed adjustment. Pure; the job just applies it."""

    action_id: str
    id_pattern: str
    actual_post_vrr: Optional[float]   # None => leave the row alone
    skip_reason: Optional[str]
    predicted_dvrr: Optional[float]
    actual_dvrr: Optional[float]
    old_rho: Optional[float]
    new_rho: Optional[float]           # None => do not touch pattern_memory


def plan_writeback(
    *,
    action_id: str,
    id_pattern: str,
    pre_vrr,
    predicted_post_vrr,
    later_vrr,
    current_rho,
    alpha: float = 0.3,
) -> WritebackPlan:
    """Decide fill vs skip vs ρ update. No I/O — the job supplies the lookups."""
    observed = _num(later_vrr)
    if observed is None:
        return WritebackPlan(
            action_id=action_id, id_pattern=id_pattern,
            actual_post_vrr=None, skip_reason="no later period",
            predicted_dvrr=None, actual_dvrr=None,
            old_rho=_num(current_rho), new_rho=None)

    deltas = vrr_deltas(pre_vrr, predicted_post_vrr, observed)
    rho = _num(current_rho)
    new_rho = None
    predicted_dvrr = actual_dvrr = None
    if deltas is not None and rho is not None:
        predicted_dvrr, actual_dvrr = deltas
        new_rho = update_response_factor(rho, predicted_dvrr, actual_dvrr, alpha=alpha)

    return WritebackPlan(
        action_id=action_id, id_pattern=id_pattern,
        actual_post_vrr=observed, skip_reason=None,
        predicted_dvrr=predicted_dvrr, actual_dvrr=actual_dvrr,
        old_rho=rho, new_rho=new_rho)


def run(conn=None) -> list[dict]:
    """Fill observed VRR and EMA-update ρ for every executed row still waiting.

    Returns one summary dict per pending row (written or still waiting). Idempotent:
    a second run sees ``actual_post_vrr`` already set and does nothing.
    """
    # psycopg / config stay inside run so the decision helpers unit-test off-DB.
    import psycopg
    from ..config import load_config

    if conn is None:
        with psycopg.connect(load_config().pg_dsn) as c:
            return run(c)

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(_PENDING_SQL)
        pending = cur.fetchall()

    # In-run cache so two executed rows for the same pattern EMA-chain in date order
    # rather than both reading the stale ρ from before this job started.
    rho_by_pattern: dict[str, Optional[float]] = {}
    out: list[dict] = []

    for row in pending:
        pid = row["id_pattern"]
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(_NEXT_PERIOD_SQL, {"p": pid, "d": row["vrr_date"]})
            nxt = cur.fetchone()
            if pid not in rho_by_pattern:
                cur.execute(_RHO_SQL, {"p": pid})
                mem = cur.fetchone()
                rho_by_pattern[pid] = (
                    _num(mem["response_factor"]) if mem is not None else None)

        later_vrr = None if nxt is None else nxt["vrr_bblbbl"]
        later_date = None if nxt is None else nxt["vrr_date"]
        plan = plan_writeback(
            action_id=row["action_id"], id_pattern=pid,
            pre_vrr=row["pre_vrr"], predicted_post_vrr=row["predicted_post_vrr"],
            later_vrr=later_vrr, current_rho=rho_by_pattern[pid])

        summary = {
            "action_id": plan.action_id, "pattern": row["pattern_name"],
            "id_pattern": pid, "vrr_date": row["vrr_date"],
            "observed_date": later_date,
            "actual_post_vrr": plan.actual_post_vrr,
            "predicted_dvrr": plan.predicted_dvrr,
            "actual_dvrr": plan.actual_dvrr,
            "old_rho": plan.old_rho, "new_rho": plan.new_rho,
            "status": "waiting" if plan.actual_post_vrr is None else "written",
            "skip_reason": plan.skip_reason,
        }

        if plan.actual_post_vrr is None:
            out.append(summary)
            continue

        with conn.cursor() as cur:
            cur.execute(_FILL_SQL, {"v": plan.actual_post_vrr, "id": plan.action_id})
            if plan.new_rho is not None:
                cur.execute(_RHO_UPDATE_SQL, {"rho": plan.new_rho, "p": pid})
                rho_by_pattern[pid] = plan.new_rho
        out.append(summary)

    conn.commit()
    return out


if __name__ == "__main__":
    rows = run()
    written = [r for r in rows if r["status"] == "written"]
    waiting = sum(1 for r in rows if r["status"] == "waiting")
    for r in written:
        rho_bit = (f"  ρ {r['old_rho']:.3f} → {r['new_rho']:.3f}"
                   if r.get("new_rho") is not None else "  ρ unchanged")
        print(f"  {r['pattern']} {r['vrr_date']}  "
              f"actual_post_vrr={r['actual_post_vrr']:.4f}{rho_bit}")
    if not rows:
        print("  nothing pending — every executed adjustment already has an observed VRR")
    else:
        print(f"  wrote {len(written)} outcome(s), waiting on a later period: {waiting}")
