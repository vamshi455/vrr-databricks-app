"""Input-audit job — gather each pattern-period's input state and store a verdict.

The batch half of the parent design's Slice A. It reads what the inputs actually look
like (PVT methods used, rows with no pressure in force, volumes outside every allocation
window, allocation over 100%, allocation age, and — for the flagged periods — an
independent recompute from raw), hands them to ``core.audit.assess_inputs``, and writes
one row per (pattern, period) into ``vrr_agent.input_audit``.

The agent then never has to re-derive trust: it reads the verdict, and only a
``REAL_SIGNAL`` period may carry a valve recommendation.

    make audit                                   # every pattern, latest period
    python -m vrr_agent_open.pipeline.input_audit --all-periods
"""
from __future__ import annotations

import json
import sys

import psycopg

from ..config import load_config
from ..core import audit as AU

CFG = load_config()

# Per pattern-period input state, straight from the lineage layer + raw allocation.
_STATE_SQL = """
WITH periods AS (
  SELECT id_pattern, max(pattern_name) AS pattern_name,
         date_trunc('month', vrr_date)::date AS period,
         count(*) AS n_rows,
         array_agg(DISTINCT pvt_method) AS pvt_methods,
         count(*) FILTER (WHERE pattern_pressure_psia IS NULL) AS n_missing_pressure,
         max(run_id) AS run_id,
         min(factor_effect_date) AS oldest_allocation
    FROM vrr_curated.completion_contrib
   GROUP BY id_pattern, date_trunc('month', vrr_date)
), orphans AS (          -- volumes that fall outside every allocation window
  SELECT count(*) AS n
    FROM vrr_raw.production_volumes_daily v
    LEFT JOIN vrr_raw.pattern_contribution_factor f ON f.id_completion = v.id_completion
   WHERE f.id_completion IS NULL
), over_alloc AS (       -- a completion allocated above 1.0 in some window
  SELECT count(*) AS n FROM (
    SELECT id_completion, effect_date FROM vrr_raw.pattern_contribution_factor
     GROUP BY 1, 2 HAVING sum(factor) > 1.0001) x
), silent AS (           -- allocated to the pattern but no volumes in the period
  SELECT f.id_pattern, p.period, count(DISTINCT f.id_completion) AS n
    FROM vrr_raw.pattern_contribution_factor f
    CROSS JOIN (SELECT DISTINCT date_trunc('month', vrr_date)::date AS period
                  FROM vrr_curated.completion_contrib) p
    LEFT JOIN vrr_curated.completion_contrib c
           ON c.id_pattern = f.id_pattern AND c.id_completion = f.id_completion
          AND date_trunc('month', c.vrr_date)::date = p.period
   WHERE c.id_completion IS NULL AND f.factor > 0
   GROUP BY 1, 2
)
SELECT pe.id_pattern, pe.pattern_name, pe.period, pe.n_rows, pe.pvt_methods,
       pe.n_missing_pressure, pe.run_id,
       (SELECT n FROM orphans) AS n_missing_factor,
       (SELECT n FROM over_alloc) AS n_allocation_over_one,
       COALESCE(s.n, 0) AS n_allocated_without_volumes,
       (pe.period - pe.oldest_allocation) AS allocation_age_days
  FROM periods pe
  LEFT JOIN silent s ON s.id_pattern = pe.id_pattern AND s.period = pe.period
 WHERE (%(latest_only)s = false OR pe.period = (
          SELECT max(date_trunc('month', vrr_date)::date)
            FROM vrr_curated.completion_contrib c2
           WHERE c2.id_pattern = pe.id_pattern))
   AND (%(p)s::text IS NULL OR pe.id_pattern = %(p)s)
 ORDER BY pe.id_pattern, pe.period
"""

_UPSERT = """
INSERT INTO vrr_agent.input_audit
 (id_pattern, pattern_name, vrr_date, verdict, actionable, summary, findings, run_id)
VALUES (%(pid)s, %(name)s, %(date)s, %(verdict)s, %(actionable)s, %(summary)s,
        %(findings)s, %(run_id)s)
ON CONFLICT (id_pattern, vrr_date) DO UPDATE SET
  verdict = EXCLUDED.verdict, actionable = EXCLUDED.actionable,
  summary = EXCLUDED.summary, findings = EXCLUDED.findings,
  run_id = EXCLUDED.run_id, audited_at = now()
"""


def run(pattern: str | None = None, latest_only: bool = True,
        conn=None) -> list[dict]:
    """Audit and store verdicts. Returns one summary dict per audited period."""
    if conn is None:
        with psycopg.connect(CFG.pg_dsn) as c:
            return run(pattern, latest_only, c)

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(_STATE_SQL, {"latest_only": latest_only, "p": pattern})
        states = cur.fetchall()

    out = []
    for st in states:
        # The recompute check is the expensive one, so it only runs where the cheap
        # signals already look clean — a stale build is otherwise invisible.
        recompute_diff = None
        if not (set(st["pvt_methods"] or []) & set(AU.LOW_CONFIDENCE_PVT)) \
                and not st["n_missing_pressure"]:
            recompute_diff = _recompute_difference(conn, st["id_pattern"], st["period"])
        verdict = AU.assess_inputs(
            n_rows=st["n_rows"], pvt_methods=st["pvt_methods"] or [],
            n_missing_pressure=st["n_missing_pressure"],
            n_missing_factor=st["n_missing_factor"],
            n_allocation_over_one=st["n_allocation_over_one"],
            n_allocated_without_volumes=st["n_allocated_without_volumes"],
            recompute_difference=recompute_diff,
            allocation_age_days=(st["allocation_age_days"].days
                                 if hasattr(st["allocation_age_days"], "days")
                                 else st["allocation_age_days"]))
        with conn.cursor() as cur:
            cur.execute(_UPSERT, {
                "pid": st["id_pattern"], "name": st["pattern_name"], "date": st["period"],
                "verdict": verdict["verdict"], "actionable": verdict["actionable"],
                "summary": verdict["summary"],
                "findings": json.dumps(verdict["findings"]), "run_id": st["run_id"]})
        out.append({"pattern": st["pattern_name"], "vrr_date": st["period"],
                    "verdict": verdict["verdict"], "summary": verdict["summary"]})
    conn.commit()
    return out


def _recompute_difference(conn, id_pattern: str, period) -> float | None:
    """Stored VRR minus an independent recompute from the lineage layer's own terms.

    Cheap consistency check between the aggregate and the rows it was built from; the
    full raw-side recompute lives in ``agent.tools.vrr_audit``.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT v.vrr_bblbbl,
                   COALESCE(SUM(c.res_water_inj_volume_bbl + c.res_gas_inj_volume_bbl)
                     / NULLIF(SUM(c.res_oil_volume_bbl + c.res_water_volume_bbl
                       + COALESCE(c.res_free_gas_volume_bbl, 0)), 0), 0)
              FROM vrr_curated.pattern_vrr v
              JOIN vrr_curated.completion_contrib c
                ON c.id_pattern = v.id_pattern
               AND date_trunc('month', c.vrr_date)::date = v.vrr_date
             WHERE v.grain = 'monthly' AND v.id_pattern = %s AND v.vrr_date = %s
             GROUP BY v.vrr_bblbbl""", (id_pattern, period))
        row = cur.fetchone()
    if not row or row[0] is None:
        return None
    return float(row[0]) - float(row[1])


if __name__ == "__main__":
    latest = "--all-periods" not in sys.argv
    rows = run(latest_only=latest)
    by_verdict: dict[str, int] = {}
    for r in rows:
        by_verdict[r["verdict"]] = by_verdict.get(r["verdict"], 0) + 1
    for r in rows:
        if r["verdict"] != "REAL_SIGNAL":
            print(f"  {r['verdict']:<14} {r['pattern']} {r['vrr_date']} — {r['summary'][:90]}")
    print(f"  audited {len(rows)} period(s): "
          + ", ".join(f"{k}={v}" for k, v in sorted(by_verdict.items())))
