"""VRR builder: `vrr_raw` → `vrr_curated`, following the production pipeline
(`CreateVRR/src/vrr_sql_builder.sql`) checkpoint for checkpoint. Mapping and column
provenance: [docs/vrr_data_model.md](../../../docs/vrr_data_model.md).

    FactorsWithEndDate      LEAD(effect_date)  → allocation window per (completion, pattern)
    PressureWithEndDate     LEAD(pressure_date) → pattern pressure window
    DailyVolumes            volumes, negatives clamped with GREATEST(x, 0)
    VolumeContext           volumes ⋈ factor window ⋈ pressure window; Amount_Type derived
    CalculatedPVT           FVFs interpolated by PRESSURE inside the applicable test_date
                            window (core.physics.pvt_lookup — labels the method)
    VolumeWithPVT           → materialised as vrr_curated.completion_contrib
    PatternData / FinalData → vrr_curated.pattern_vrr (daily + monthly) with the HAVING
                              gate and volume-weighted average FVFs
    cumulative              → vrr_curated.pattern_vrr_cumulative (running Σ per pattern)

Every reservoir volume is computed by ``core.physics``, never by SQL, so the numbers are
identical to the ones the unit tests pin and to what ``VRR_AUDIT`` recomputes.
"""
from __future__ import annotations

import uuid

import psycopg

from ..config import load_config
from ..core import physics
from .dbio import chunked, copy_rows

CFG = load_config()

# PVT method labels that mean "don't trust this row's inputs" (core.anomaly rule 1).
LOW_CONFIDENCE = (physics.EXTRAP, physics.CLOSEST, physics.NONE)

# KSCF → SCF for the OilField unit convention (mult in the production SQL).
GAS_MULT = {"OilField": 1000.0, "Metric": 1.0}

# --- VolumeContext: volumes ⋈ windowed factor ⋈ windowed pressure -------------
# The two LEAD()s are FactorsWithEndDate / PressureWithEndDate; the join predicates are
# the production ones: DATE >= start AND DATE < next_start (half-open windows).
_VOLUME_CONTEXT_SQL = """
WITH factors AS (
  SELECT id_completion, id_pattern, factor, effect_date,
         LEAD(effect_date) OVER (PARTITION BY id_completion, id_pattern
                                 ORDER BY effect_date) AS end_date
    FROM vrr_raw.pattern_contribution_factor
), pressures AS (
  SELECT id_pattern, pressure_date, pressure,
         LEAD(pressure_date) OVER (PARTITION BY id_pattern
                                   ORDER BY pressure_date) AS end_date
    FROM vrr_raw.pattern_pressure
), daily AS (
  SELECT id_completion, prod_date, uom,
         GREATEST(COALESCE(alloc_oil_vol_stb, 0), 0)      AS oil_vol,
         GREATEST(COALESCE(alloc_water_vol_stb, 0), 0)    AS water_vol,
         GREATEST(COALESCE(alloc_gas_vol_kscf, 0), 0)     AS gas_vol,
         GREATEST(COALESCE(alloc_water_inj_vol_stb, 0), 0) AS water_inj_vol,
         GREATEST(COALESCE(alloc_gas_inj_vol_kscf, 0), 0)  AS gas_inj_vol
    FROM vrr_raw.production_volumes_daily
)
SELECT f.id_pattern, p.pattern_name, d.id_completion, d.prod_date AS vrr_date, d.uom,
       f.factor, f.effect_date AS factor_effect_date, pr.pressure AS pattern_pressure,
       d.oil_vol, d.water_vol, d.gas_vol, d.water_inj_vol, d.gas_inj_vol,
       CASE WHEN d.oil_vol + d.water_vol + d.gas_vol > 0
            THEN 'Production' ELSE 'Injection' END AS amount_type
  FROM daily d
  JOIN factors f ON f.id_completion = d.id_completion
                AND d.prod_date >= f.effect_date
                AND (f.end_date IS NULL OR d.prod_date < f.end_date)
  LEFT JOIN vrr_raw.pattern p ON p.id_pattern = f.id_pattern
  LEFT JOIN pressures pr ON pr.id_pattern = f.id_pattern
                        AND d.prod_date >= pr.pressure_date
                        AND (pr.end_date IS NULL OR d.prod_date < pr.end_date)
 ORDER BY f.id_pattern, d.prod_date, d.id_completion
"""

_CONTRIB_COLUMNS = (
    "id_pattern", "pattern_name", "id_completion", "vrr_date", "amount_type", "factor",
    "factor_effect_date", "pattern_pressure_psia", "oil_volume_bbl", "water_volume_bbl",
    "gas_volume_kscf", "water_inj_volume_bbl", "gas_inj_volume_kscf", "pvt_method",
    "pvt_test_date", "bo", "bw", "bg", "bg_inj", "bw_inj", "rs", "rv",
    "res_oil_volume_bbl", "res_water_volume_bbl", "res_free_gas_volume_bbl",
    "res_water_inj_volume_bbl", "res_gas_inj_volume_bbl", "run_id")

# --- PatternData / FinalData: aggregate + HAVING gate + weighted-average FVFs --
# `grain` is the only difference between DAILY_PATTERN_VRR and MONTHLY_PATTERN_VRR, so
# one statement builds both. Weighted averages weight each FVF by the surface volume it
# was applied to — the production model's volume-weighted AVG_*_FVF columns.
_PATTERN_VRR_INSERT = """
INSERT INTO vrr_curated.pattern_vrr
 (id_alias, id_pattern, pattern_name, vrr_date, grain, pattern_pressure_psia,
  oil_volume_bbl, water_volume_bbl, water_inj_volume_bbl, gas_volume_kscf,
  gas_inj_volume_kscf, res_oil_volume_bbl, res_water_volume_bbl,
  res_free_gas_volume_bbl, res_water_inj_volume_bbl, res_gas_inj_volume_bbl,
  res_production_volume_bbl, res_injection_volume_bbl, vrr_bblbbl,
  avg_oil_fvf, avg_water_fvf, avg_gas_fvf, avg_inj_water_fvf, avg_inj_gas_fvf,
  avg_solution_gas_oil_ratio, avg_volatized_oil_gas_ratio,
  n_completions, any_extrapolated, run_id)
SELECT CONCAT(c.id_pattern, %(id_fmt)s::text), c.id_pattern,
       COALESCE(MAX(c.pattern_name), c.id_pattern),
       {period}::date AS vrr_date, %(grain)s::text,
       AVG(c.pattern_pressure_psia),
       SUM(c.oil_volume_bbl), SUM(c.water_volume_bbl), SUM(c.water_inj_volume_bbl),
       SUM(c.gas_volume_kscf), SUM(c.gas_inj_volume_kscf),
       SUM(c.res_oil_volume_bbl), SUM(c.res_water_volume_bbl),
       SUM(c.res_free_gas_volume_bbl),            -- NULLs excluded, as in the SQL builder
       SUM(c.res_water_inj_volume_bbl), SUM(c.res_gas_inj_volume_bbl),
       SUM(c.res_oil_volume_bbl + c.res_water_volume_bbl
           + COALESCE(c.res_free_gas_volume_bbl, 0)) AS res_production_volume_bbl,
       SUM(c.res_water_inj_volume_bbl + c.res_gas_inj_volume_bbl) AS res_injection_volume_bbl,
       COALESCE(SUM(c.res_water_inj_volume_bbl + c.res_gas_inj_volume_bbl)
                / NULLIF(SUM(c.res_oil_volume_bbl + c.res_water_volume_bbl
                             + COALESCE(c.res_free_gas_volume_bbl, 0)), 0), 0) AS vrr_bblbbl,
       SUM(c.bo * c.oil_volume_bbl) / NULLIF(SUM(c.oil_volume_bbl), 0),
       SUM(c.bw * c.water_volume_bbl) / NULLIF(SUM(c.water_volume_bbl), 0),
       SUM(c.bg * c.gas_volume_kscf) / NULLIF(SUM(c.gas_volume_kscf), 0),
       SUM(c.bw_inj * c.water_inj_volume_bbl) / NULLIF(SUM(c.water_inj_volume_bbl), 0),
       SUM(c.bg_inj * c.gas_inj_volume_kscf) / NULLIF(SUM(c.gas_inj_volume_kscf), 0),
       SUM(c.rs * c.oil_volume_bbl) / NULLIF(SUM(c.oil_volume_bbl), 0),
       SUM(c.rv * c.gas_volume_kscf) / NULLIF(SUM(c.gas_volume_kscf), 0),
       COUNT(DISTINCT c.id_completion),
       BOOL_OR(c.pvt_method = ANY(%(low)s)),
       %(run_id)s
  FROM vrr_curated.completion_contrib c
 GROUP BY c.id_pattern, {period}
 -- the production HAVING gate: keep a period only if it has real produced volume or
 -- real injection. Drops all-zero rows instead of emitting VRR = 0.
HAVING (SUM(c.res_oil_volume_bbl) + SUM(c.res_water_volume_bbl)) <> 0
    OR SUM(c.water_inj_volume_bbl + c.gas_inj_volume_kscf) > 0
"""

# ← vrr_cumulative_calculator.sql
_CUMULATIVE_INSERT = """
INSERT INTO vrr_curated.pattern_vrr_cumulative
 (id_pattern, pattern_name, vrr_date, grain,
  res_cumulative_oil_production_volume_bbl, res_cumulative_water_production_volume_bbl,
  res_cumulative_water_injection_volume_bbl, res_cumulative_gas_injection_volume_bbl,
  res_cumulative_production_volume_bbl, res_cumulative_injection_volume_bbl,
  cumulative_vrr_bblbbl, run_id)
SELECT id_pattern, pattern_name, vrr_date, grain,
       SUM(res_oil_volume_bbl) OVER w,
       SUM(res_water_volume_bbl) OVER w,
       SUM(res_water_inj_volume_bbl) OVER w,
       SUM(res_gas_inj_volume_bbl) OVER w,
       SUM(res_production_volume_bbl) OVER w,
       SUM(res_injection_volume_bbl) OVER w,
       COALESCE(SUM(res_injection_volume_bbl) OVER w
                / NULLIF(SUM(res_production_volume_bbl) OVER w, 0), 0),
       %(run_id)s
  FROM vrr_curated.pattern_vrr
 WHERE grain = %(grain)s
WINDOW w AS (PARTITION BY id_pattern ORDER BY vrr_date
             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
"""


def _pvt_by_completion(conn) -> dict[str, list[tuple]]:
    """PVT lab rows per completion as (test_date, PVTPoint), ordered by test_date."""
    out: dict[str, list[tuple]] = {}
    with conn.cursor() as cur:
        cur.execute("SELECT id_completion, test_date, pressure, bo, bw, bg, bg_inj,"
                    " bw_inj, rs, rv FROM vrr_raw.completion_pvt_characteristics"
                    " ORDER BY id_completion, test_date, pressure")
        for cid, tdate, psi, bo, bw, bg, bg_inj, bw_inj, rs, rv in cur.fetchall():
            out.setdefault(cid, []).append((tdate, physics.PVTPoint(
                pressure_psi=psi, bo=bo, bw=bw, bg=bg, rs=rs, rv=rv,
                bw_inj=bw_inj, bg_inj=bg_inj)))
    return out


def _applicable_test(points: list[tuple], on_date) -> tuple:
    """The PVT test set in force on ``on_date``: the latest test_date on or before it
    (the production model's TEST_DATE window), falling back to the earliest test when the
    volume date precedes every test."""
    dates = sorted({t for t, _ in points})
    if not dates:
        return None, []
    applicable = [d for d in dates if d <= on_date]
    chosen = applicable[-1] if applicable else dates[0]
    return chosen, [p for t, p in points if t == chosen]


def build_contrib(conn, run_id: str) -> int:
    """VolumeContext + CalculatedPVT → vrr_curated.completion_contrib (lineage layer).

    Streams: the volume context is read through a server-side cursor in batches and the
    contribution rows are COPY'd out on a second connection, so a 370k-row field never
    materialises as one Python list.
    """
    pvt = _pvt_by_completion(conn)
    cache: dict[tuple, tuple] = {}

    def rows_for(batch) -> list[tuple]:
        out = []
        for (id_pattern, pattern_name, id_completion, vrr_date, uom, factor,
             factor_effect_date, pressure, oil, water, gas, water_inj, gas_inj,
             amount_type) in batch:
            test_date, points = _applicable_test(pvt.get(id_completion, []), vrr_date)
            key = (id_completion, test_date, pressure)
            look = cache.get(key)
            if look is None:
                look = cache[key] = physics.pvt_lookup(points, pressure)
            props = look.props
            terms = physics.completion_contribution(
                factor=factor, oil=oil, water=water, gas=gas, water_inj=water_inj,
                gas_inj=gas_inj, pvt=props, is_producer=(amount_type == "Production"),
                gas_kscf_to_scf=GAS_MULT.get(uom or "OilField", 1000.0))
            out.append((
                id_pattern, pattern_name, id_completion, vrr_date, amount_type, factor,
                factor_effect_date, pressure, oil, water, gas, water_inj, gas_inj,
                look.method, test_date,
                props.get("bo"), props.get("bw"), props.get("bg"), props.get("bg_inj"),
                props.get("bw_inj"), props.get("rs"), props.get("rv"),
                terms.oil_res, terms.water_res, terms.free_gas_res,
                terms.water_inj_res, terms.gas_inj_res, run_id))
        return out

    written = 0
    with psycopg.connect(CFG.pg_dsn) as writer:          # separate conn: COPY + TRUNCATE
        with writer.cursor() as cur:
            cur.execute("TRUNCATE vrr_curated.completion_contrib")
        with conn.cursor(name="volume_context") as cur:   # server-side: streams raw
            cur.execute(_VOLUME_CONTEXT_SQL)
            for batch in chunked(cur):
                written += copy_rows(writer, "vrr_curated.completion_contrib",
                                     _CONTRIB_COLUMNS, rows_for(batch))
        writer.commit()
    return written


def build_pattern_vrr(conn, run_id: str) -> dict[str, int]:
    """PatternData/FinalData at both grains + the cumulative table."""
    counts: dict[str, int] = {}
    grains = {"daily": "c.vrr_date", "monthly": "date_trunc('month', c.vrr_date)"}
    with conn.cursor() as cur:
        cur.execute("TRUNCATE vrr_curated.pattern_vrr")
        cur.execute("TRUNCATE vrr_curated.pattern_vrr_cumulative")
        for grain, period in grains.items():
            cur.execute(_PATTERN_VRR_INSERT.format(period=period),
                        {"run_id": run_id, "grain": grain,
                         "id_fmt": "_M" if grain == "monthly" else "_D",
                         "low": list(LOW_CONFIDENCE)})
            counts[f"vrr_curated.pattern_vrr ({grain})"] = cur.rowcount
        for grain in grains:
            cur.execute(_CUMULATIVE_INSERT, {"run_id": run_id, "grain": grain})
            counts[f"vrr_curated.pattern_vrr_cumulative ({grain})"] = cur.rowcount
    conn.commit()
    return counts


def run(conn=None, run_id: str | None = None) -> dict[str, int]:
    """Full raw → curated rebuild. Every table carries the same ``run_id``."""
    run_id = run_id or uuid.uuid4().hex[:12]
    if conn is None:
        with psycopg.connect(CFG.pg_dsn) as c:
            return run(c, run_id)
    out = {"vrr_curated.completion_contrib": build_contrib(conn, run_id)}
    out.update(build_pattern_vrr(conn, run_id))
    return out


if __name__ == "__main__":
    for table, count in run().items():
        print(f"  built {count:>6} rows → {table}")
