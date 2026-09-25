"""Deterministic VRR tools over PostgreSQL (data + compute) — the ONLY source of
numbers the agent may narrate. Same trust model as the Databricks version: the LLM
picks tools + narrates; every figure comes from here with provenance.

Postgres replaces the SQL warehouse; psycopg replaces the Spark/warehouse layer.
The tool CONTRACTS (names, args, returns) match the Databricks tools so the agent
graph and the faithfulness gate are unchanged.

The toolbelt, in the order an analyst usually needs it:
  LIST_PATTERNS      what patterns exist, latest VRR
  VRR_OVERVIEW       portfolio ranked by drift from target (start here at field scale)
  DATA_QUALITY       ingestion checks: allocation sums, orphan volumes, missing PVT
  VRR_TREND          the series behind the chart (date-filtered)
  VRR_GET            one period + provenance
  VRR_DECOMPOSE      ΔVRR attribution a→b (core.decompose, exact + additive)
  LIST_COMPLETIONS   the completions in a pattern, role + share of production/injection
  PATTERN_LAYOUT     the same wells as a schematic: shape, contribution, shared completions
  VRR_LINEAGE        how THIS number was built: monthly ← completions ← raw + PVT
  VRR_AUDIT          recompute from raw + core.audit verdict (DATA_ARTIFACT/REAL_SIGNAL)
  INPUT_AUDIT        stored verdicts per pattern-period (the input-audit gate)
  PATTERN_CONTEXT    target, learned band/ρ, safety limits, prior adjustments
  DETECT_ANOMALIES   core.anomaly over the pattern's history
  RECOMMEND_CHANGE   core.recommend — bounded, ρ-calibrated valve change
  SUBMIT_FOR_APPROVAL write the draft into the action queue (stage='draft')
  SEARCH_KNOWLEDGE   pgvector search over ingested reservoir docs (needs embeddings)
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Optional

import psycopg

from ..config import load_config
from ..core import anomaly as AN
from ..core import audit as AU
from ..core import decompose as DC
from ..core import pattern_layout as PL
from ..core import physics
from ..core import recommend as RE
from . import tracing

CFG = load_config()

TERM_COLUMNS = ("oil_res", "water_res", "free_gas_res", "water_inj_res", "gas_inj_res")


def _rows(sql: str, params: dict | None = None) -> list[dict]:
    with psycopg.connect(CFG.pg_dsn, row_factory=psycopg.rows.dict_row) as c:
        with c.cursor() as cur:
            cur.execute(sql, params or {})
            return cur.fetchall() if cur.description else []


def _execute(sql: str, params: dict) -> int:
    with psycopg.connect(CFG.pg_dsn) as c, c.cursor() as cur:
        cur.execute(sql, params)
        n = cur.rowcount
        c.commit()
    return n


def _resolve(pattern: str) -> Optional[dict]:
    """Accept either a pattern_id ('PAT-001') or a name ('UNITY'), case-insensitive."""
    # Accept the pattern NAME, the full 16-char hex ID, or an ID prefix of >= 6 chars —
    # IDs are opaque surrogate keys (core/ids.py) and nobody types them in full.
    r = _rows("SELECT id_pattern AS pattern_id, pattern_name, asset FROM vrr_raw.pattern "
              "WHERE upper(id_pattern)=upper(%(p)s) OR upper(pattern_name)=upper(%(p)s)",
              {"p": pattern})
    if not r and isinstance(pattern, str) and len(pattern.strip()) >= 6:
        r = _rows("SELECT id_pattern AS pattern_id, pattern_name, asset FROM vrr_raw.pattern"
                  " WHERE id_pattern LIKE upper(%(p)s) || '%%' LIMIT 2",
                  {"p": pattern.strip()})
        if len(r) > 1:                    # ambiguous prefix: refuse rather than guess
            return None
    return r[0] if r else None


# ---- the deterministic tools ------------------------------------------------

def list_patterns() -> list[dict]:
    return _rows("SELECT id_pattern AS pattern_id, max(pattern_name) pattern_name,"
                 " count(*) n, max(vrr_date) last_date FROM vrr_curated.pattern_vrr"
                 " WHERE grain='monthly' GROUP BY id_pattern ORDER BY id_pattern")


@tracing.trace("VRR_TREND", span_type="TOOL")
def vrr_trend(pattern: str, date_from: str | None = None,
              date_to: str | None = None, grain: str = "monthly") -> dict:
    """The pattern's VRR series (the data behind the chart), optionally date-filtered."""
    p = _resolve(pattern)
    if not p:
        return {"found": False, "pattern": pattern}
    rows = _rows(
        "SELECT id_pattern AS pattern_id, pattern_name, vrr_date,"
        " vrr_bblbbl AS vrr, res_production_volume_bbl AS prod_res_bbl,"
        " res_injection_volume_bbl AS inj_res_bbl, pattern_pressure_psia,"
        " n_completions, any_extrapolated, run_id FROM vrr_curated.pattern_vrr"
        " WHERE grain=%(g)s AND id_pattern=%(p)s"
        " AND (%(a)s::date IS NULL OR vrr_date >= %(a)s::date)"
        " AND (%(b)s::date IS NULL OR vrr_date <= %(b)s::date) ORDER BY vrr_date",
        {"p": p["pattern_id"], "a": date_from, "b": date_to, "g": grain})
    return {"found": bool(rows), "pattern_id": p["pattern_id"], "grain": grain,
            "pattern_name": p["pattern_name"], "rows": rows,
            "provenance": {"table": "vrr_curated.pattern_vrr", "grain": grain,
                           "filter": {"id_pattern": p["pattern_id"],
                                      "from": date_from, "to": date_to}}}


@tracing.trace("VRR_GET", span_type="TOOL")
def vrr_get(pattern: str, date: str) -> dict:
    p = _resolve(pattern)
    pid = p["pattern_id"] if p else pattern
    r = _rows("SELECT *, id_pattern AS pattern_id, vrr_bblbbl AS vrr,"
              " res_production_volume_bbl AS prod_res_bbl,"
              " res_injection_volume_bbl AS inj_res_bbl FROM vrr_curated.pattern_vrr "
              "WHERE grain='monthly' AND id_pattern=%(p)s AND vrr_date=%(d)s",
              {"p": pid, "d": date})
    if not r:
        return {"found": False, "pattern": pattern, "date": date}
    row = r[0]
    row["found"] = True
    row["provenance"] = {"table": "vrr_curated.pattern_vrr",
                         "keys": {"id_pattern": pid, "vrr_date": date, "grain": "monthly"}}
    return row


def _term_totals(pattern_id: str, date: str) -> dict:
    r = _rows(
        "SELECT sum(res_oil_volume_bbl) oil_res, sum(res_water_volume_bbl) water_res,"
        " sum(coalesce(res_free_gas_volume_bbl,0)) free_gas_res,"
        " sum(res_water_inj_volume_bbl) water_inj_res,"
        " sum(res_gas_inj_volume_bbl) gas_inj_res FROM vrr_curated.completion_contrib"
        " WHERE id_pattern=%(p)s AND date_trunc('month', vrr_date)=%(d)s::date",
        {"p": pattern_id, "d": date})
    return {k: (v or 0.0) for k, v in (r[0] if r else {}).items()}


@tracing.trace("VRR_DECOMPOSE", span_type="TOOL")
def vrr_decompose(pattern: str, date_a: str, date_b: str) -> dict:
    """ΔVRR attribution a→b via the exact log-mean (LMDI) math in ``core.decompose``.

    Term totals come from ``vrr_curated.completion_contrib`` — the lineage layer — so
    the attribution is traceable to the same rows that built the monthly VRR.
    """
    p = _resolve(pattern)
    if not p:
        return {"ok": False, "reason": f"unknown pattern '{pattern}'"}
    a, b = _term_totals(p["pattern_id"], date_a), _term_totals(p["pattern_id"], date_b)
    if not a or not b:
        return {"ok": False, "reason": "no contribution rows for one of the periods"}
    result = DC.decompose_vrr(a, b)
    result.update({"pattern_id": p["pattern_id"], "pattern_name": p["pattern_name"],
                   "date_a": date_a, "date_b": date_b,
                   "provenance": {"table": "vrr_curated.completion_contrib",
                                  "grain": "sum of *_res terms per month"}})
    return result


@tracing.trace("VRR_LINEAGE", span_type="TOOL")
def vrr_lineage(pattern: str, date: str) -> dict:
    """Full derivation of ONE monthly VRR: aggregate ← completions ← raw + PVT method.

    This is the lineage answer an auditor asks for — every root input, the PVT lookup
    method that produced the FVFs, the per-completion reservoir terms, and the formula
    each was computed with.
    """
    p = _resolve(pattern)
    if not p:
        return {"found": False, "pattern": pattern}
    pid = p["pattern_id"]
    monthly = vrr_get(pid, date)
    completions = _rows(
        "SELECT id_completion AS completion_id, count(*) n_days,"
        " min(pattern_pressure_psia) min_pressure, max(pattern_pressure_psia) max_pressure,"
        " string_agg(DISTINCT pvt_method, ',') pvt_methods, avg(factor) factor,"
        " sum(oil_volume_bbl) oil, sum(water_volume_bbl) water, sum(gas_volume_kscf) gas,"
        " sum(water_inj_volume_bbl) water_inj, sum(gas_inj_volume_kscf) gas_inj,"
        " sum(res_oil_volume_bbl) oil_res, sum(res_water_volume_bbl) water_res,"
        " sum(coalesce(res_free_gas_volume_bbl,0)) free_gas_res,"
        " sum(res_water_inj_volume_bbl) water_inj_res, sum(res_gas_inj_volume_bbl) gas_inj_res"
        " FROM vrr_curated.completion_contrib WHERE id_pattern=%(p)s"
        " AND date_trunc('month', vrr_date)=%(d)s::date GROUP BY id_completion"
        " ORDER BY id_completion", {"p": pid, "d": date})
    totals = _term_totals(pid, date)
    prod = sum(totals.get(t, 0.0) for t in DC.PROD_TERMS)
    inj = sum(totals.get(t, 0.0) for t in DC.INJ_TERMS)
    # FACTOR is the first multiplicand in every formula below, so the graph has to show
    # it as numbers and not just name the table it came from. Summarised here rather than
    # in the browser to keep the rule that React never derives a figure; the per-completion
    # values are already in `completions` for the table underneath.
    factors = [c["factor"] for c in completions if c.get("factor") is not None]
    allocation = {
        "n": len(factors),
        "min": min(factors) if factors else None,
        "max": max(factors) if factors else None,
        # Weighted by the reservoir barrels each completion actually contributed — an
        # unweighted mean would let a shut-in well with factor 0.95 dominate the summary.
        "weighted_mean": (
            sum(c["factor"] * (c["oil_res"] + c["water_res"] + c["free_gas_res"]
                               + c["water_inj_res"] + c["gas_inj_res"])
                for c in completions if c.get("factor") is not None)
            / denom if (denom := sum(c["oil_res"] + c["water_res"] + c["free_gas_res"]
                                     + c["water_inj_res"] + c["gas_inj_res"]
                                     for c in completions)) else None
        ),
    }
    return {
        "found": bool(completions), "pattern_id": pid,
        "pattern_name": p["pattern_name"], "vrr_date": date,
        "monthly": monthly, "completions": completions, "term_totals": totals,
        "allocation": allocation,
        "recomputed_from_terms": {"prod_res_bbl": prod, "inj_res_bbl": inj,
                                  "vrr": physics.vrr(inj, prod)},
        "formulas": {
            "res_oil_volume_bbl": "FACTOR · OIL_VOL · Bo",
            "res_water_volume_bbl": "FACTOR · WATER_VOL · Bw",
            "res_free_gas_volume_bbl":
                "((GAS_VOL·1000) − Rs·OIL_VOL) · FACTOR · Bg   [producers, OIL_VOL>0]",
            "res_water_inj_volume_bbl": "FACTOR · WATER_INJ_VOL · Bw_inj",
            "res_gas_inj_volume_bbl": "GAS_INJ_VOL·1000 · FACTOR · Bg_inj",
            "vrr_bblbbl": "COALESCE(RES_INJECTION_VOLUME / NULLIF(RES_PRODUCTION_VOLUME,0), 0)",
        },
        "sources": {
            "raw_volumes": "vrr_raw.production_volumes_daily (per completion)",
            "allocation": "vrr_raw.pattern_contribution_factor (windowed by effect_date)",
            "pressure": "vrr_raw.pattern_pressure (windowed; reading holds to the next)",
            "pvt": "vrr_raw.completion_pvt_characteristics → core.physics.pvt_lookup",
            "lineage_layer": "vrr_curated.completion_contrib",
            "aggregate": "vrr_curated.pattern_vrr (grain=monthly)",
            "code": "vrr_agent_open.core.physics.completion_contribution",
        },
    }


@tracing.trace("VRR_AUDIT", span_type="TOOL")
def vrr_audit(pattern: str, date: str, tolerance: float = 1e-6) -> dict:
    """Independently RECOMPUTE the month's VRR from raw tables and diff it vs stored.

    Answers "is the number on the screen actually right?" without trusting the curated
    layer: re-reads raw volumes/pressure/PVT, re-runs ``core.physics`` here, and
    compares. A mismatch means the curated build is stale or the raw data changed.
    """
    p = _resolve(pattern)
    if not p:
        return {"ok": False, "reason": f"unknown pattern '{pattern}'"}
    pid = p["pattern_id"]

    pvt_points: dict[str, list] = {}
    for r in _rows("SELECT * FROM vrr_raw.completion_pvt_characteristics"):
        pvt_points.setdefault((r["id_completion"], r["test_date"]), []).append(
            physics.PVTPoint(pressure_psi=r["pressure"], bo=r["bo"], bw=r["bw"],
                             bg=r["bg"], rs=r["rs"], rv=r["rv"],
                             bw_inj=r["bw_inj"], bg_inj=r["bg_inj"]))

    # Recompute the month from RAW, redoing the windowed joins independently of the
    # builder: volumes ⋈ contribution-factor window ⋈ pressure window.
    raw = _rows(
        "WITH factors AS (SELECT id_completion, id_pattern, factor, effect_date,"
        "   LEAD(effect_date) OVER (PARTITION BY id_completion, id_pattern"
        "                           ORDER BY effect_date) end_date"
        "   FROM vrr_raw.pattern_contribution_factor),"
        " pressures AS (SELECT id_pattern, pressure_date, pressure,"
        "   LEAD(pressure_date) OVER (PARTITION BY id_pattern"
        "                             ORDER BY pressure_date) end_date"
        "   FROM vrr_raw.pattern_pressure)"
        " SELECT v.id_completion, v.prod_date, v.uom, f.factor, pr.pressure,"
        "   GREATEST(COALESCE(v.alloc_oil_vol_stb,0),0) oil,"
        "   GREATEST(COALESCE(v.alloc_water_vol_stb,0),0) water,"
        "   GREATEST(COALESCE(v.alloc_gas_vol_kscf,0),0) gas,"
        "   GREATEST(COALESCE(v.alloc_water_inj_vol_stb,0),0) water_inj,"
        "   GREATEST(COALESCE(v.alloc_gas_inj_vol_kscf,0),0) gas_inj"
        " FROM vrr_raw.production_volumes_daily v"
        " JOIN factors f ON f.id_completion=v.id_completion"
        "   AND v.prod_date >= f.effect_date"
        "   AND (f.end_date IS NULL OR v.prod_date < f.end_date)"
        " LEFT JOIN pressures pr ON pr.id_pattern=f.id_pattern"
        "   AND v.prod_date >= pr.pressure_date"
        "   AND (pr.end_date IS NULL OR v.prod_date < pr.end_date)"
        " WHERE f.id_pattern=%(p)s AND date_trunc('month', v.prod_date)=%(d)s::date",
        {"p": pid, "d": date})
    if not raw:
        return {"ok": False, "reason": f"no raw rows for {pid} in {date}"}

    prod = inj = 0.0
    methods: set[str] = set()
    tests = sorted({k[1] for k in pvt_points})
    for r in raw:
        applicable = [d for d in tests if d <= r["prod_date"]]
        tdate = applicable[-1] if applicable else (tests[0] if tests else None)
        pvt = physics.pvt_lookup(pvt_points.get((r["id_completion"], tdate), []),
                                 r["pressure"])
        methods.add(pvt.method)
        producing = (r["oil"] + r["water"] + r["gas"]) > 0      # derived Amount_Type
        t = physics.completion_contribution(
            factor=r["factor"], oil=r["oil"], water=r["water"], gas=r["gas"],
            water_inj=r["water_inj"], gas_inj=r["gas_inj"], pvt=pvt.props,
            is_producer=producing,
            gas_kscf_to_scf=1000.0 if (r["uom"] or "OilField") == "OilField" else 1.0)
        prod += t.prod_res
        inj += t.inj_res

    recomputed = physics.vrr(inj, prod)
    stored = vrr_get(pid, date)
    stored_vrr = stored.get("vrr")
    diff = None if (recomputed is None or stored_vrr is None) else recomputed - stored_vrr
    # The audit is not just "do the numbers match" — core.audit turns what we observed
    # into the decision the workflow needs (parent Slice A).
    verdict = AU.assess_inputs(
        n_rows=len(raw), pvt_methods=sorted(methods),
        n_missing_pressure=sum(1 for r in raw if r["pressure"] is None),
        recompute_difference=diff, tolerance=tolerance)
    return {
        "audit": verdict, "verdict": verdict["verdict"],
        "actionable": verdict["actionable"], "route": AU.route_for(verdict["verdict"]),
        "ok": True, "pattern_id": pid, "pattern_name": p["pattern_name"],
        "vrr_date": date, "n_raw_rows": len(raw),
        "recomputed": {"vrr": recomputed, "prod_res_bbl": prod, "inj_res_bbl": inj},
        "stored": {"vrr": stored_vrr, "prod_res_bbl": stored.get("prod_res_bbl"),
                   "inj_res_bbl": stored.get("inj_res_bbl"), "run_id": stored.get("run_id")},
        "difference": diff,
        "matches": diff is not None and abs(diff) <= tolerance,
        "pvt_methods": sorted(methods),
        "low_confidence_inputs": bool(methods & {physics.EXTRAP, physics.CLOSEST, physics.NONE}),
        "provenance": {"recomputed_from": ["vrr_raw.production_volumes_daily",
                                           "vrr_raw.pattern_contribution_factor",
                                           "vrr_raw.pattern_pressure",
                                           "vrr_raw.completion_pvt_characteristics"],
                       "code": "core.physics.pvt_lookup + completion_contribution"},
    }


@tracing.trace("VRR_OVERVIEW", span_type="TOOL")
def vrr_overview(asset: str | None = None, limit: int = 50) -> dict:
    """Portfolio view: every pattern's latest VRR vs target, ranked by absolute drift.

    The entry point for a 40-pattern field — "where should I look first?" — and the
    backing for the app's Portfolio tab.
    """
    rows = _rows(
        "WITH latest AS ("
        "  SELECT DISTINCT ON (v.id_pattern) v.id_pattern, v.pattern_name, v.vrr_date,"
        "         v.vrr_bblbbl AS vrr, v.res_production_volume_bbl AS prod_res_bbl,"
        "         v.res_injection_volume_bbl AS inj_res_bbl, v.n_completions,"
        "         v.any_extrapolated"
        "    FROM vrr_curated.pattern_vrr v WHERE v.grain='monthly'"
        "   ORDER BY v.id_pattern, v.vrr_date DESC)"
        " SELECT l.*, p.asset, COALESCE(t.target_vrr, %(dt)s) AS target_vrr,"
        "        m.typical_low, m.typical_high, m.response_factor,"
        "        abs(l.vrr - COALESCE(t.target_vrr, %(dt)s)) AS drift"
        "   FROM latest l"
        "   LEFT JOIN vrr_raw.pattern p ON p.id_pattern = l.id_pattern"
        "   LEFT JOIN vrr_raw.pattern_target t ON t.id_pattern = l.id_pattern"
        "   LEFT JOIN vrr_agent.pattern_memory m ON m.id_pattern = l.id_pattern"
        "  WHERE (%(a)s::text IS NULL OR p.asset = %(a)s)"
        "  ORDER BY drift DESC LIMIT %(n)s",
        {"a": asset, "n": limit, "dt": CFG.default_target_vrr})
    for r in rows:
        lo = r.get("typical_low") or 0.9
        hi = r.get("typical_high") or 1.1
        r["verdict"] = ("on target" if lo <= r["vrr"] <= hi else
                        "OVER-replicating" if r["vrr"] > hi else "UNDER-replicating")
    return {"n_patterns": len(rows), "asset": asset, "patterns": rows,
            "off_target": [r for r in rows if r["verdict"] != "on target"],
            "provenance": {"table": "vrr_curated.pattern_vrr (grain=monthly, latest period)"}}


@tracing.trace("DATA_QUALITY", span_type="TOOL")
def data_quality(pattern: str | None = None) -> dict:
    """Ingestion-quality checks a real feed needs, as data rather than assumptions.

    Each check returns offending keys, so "is the input data sane?" is answerable before
    anyone argues about a VRR number.
    """
    pid = None
    if pattern:
        p = _resolve(pattern)
        if not p:
            return {"ok": False, "reason": f"unknown pattern '{pattern}'"}
        pid = p["pattern_id"]
    params = {"p": pid}
    checks = {
        # allocation can never exceed 100% of a completion on a given day
        "factor_sum_over_one": (
            "WITH w AS (SELECT id_completion, id_pattern, factor, effect_date,"
            "   LEAD(effect_date) OVER (PARTITION BY id_completion, id_pattern"
            "     ORDER BY effect_date) end_date"
            "   FROM vrr_raw.pattern_contribution_factor)"
            " SELECT id_completion, effect_date, round(sum(factor)::numeric,4) total"
            "   FROM w WHERE (%(p)s::text IS NULL OR id_pattern = %(p)s)"
            "  GROUP BY 1,2 HAVING sum(factor) > 1.0001 LIMIT 50"),
        "volumes_without_allocation": (
            "SELECT DISTINCT v.id_completion FROM vrr_raw.production_volumes_daily v"
            " LEFT JOIN vrr_raw.pattern_contribution_factor f"
            "   ON f.id_completion = v.id_completion"
            " WHERE f.id_completion IS NULL AND %(p)s::text IS NULL LIMIT 50"),
        "allocation_without_volumes": (
            "SELECT DISTINCT f.id_completion, f.id_pattern"
            " FROM vrr_raw.pattern_contribution_factor f"
            " LEFT JOIN vrr_raw.production_volumes_daily v"
            "   ON v.id_completion = f.id_completion"
            " WHERE v.id_completion IS NULL"
            "   AND (%(p)s::text IS NULL OR f.id_pattern = %(p)s) LIMIT 50"),
        "patterns_without_pressure": (
            "SELECT p.id_pattern, p.pattern_name FROM vrr_raw.pattern p"
            " LEFT JOIN vrr_raw.pattern_pressure pr ON pr.id_pattern = p.id_pattern"
            " WHERE pr.id_pattern IS NULL"
            "   AND (%(p)s::text IS NULL OR p.id_pattern = %(p)s) LIMIT 50"),
        "completions_without_pvt": (
            "SELECT DISTINCT f.id_completion FROM vrr_raw.pattern_contribution_factor f"
            " LEFT JOIN vrr_raw.completion_pvt_characteristics c"
            "   ON c.id_completion = f.id_completion"
            " WHERE c.id_completion IS NULL"
            "   AND (%(p)s::text IS NULL OR f.id_pattern = %(p)s) LIMIT 50"),
        "unregistered_completions": (
            "SELECT DISTINCT v.id_completion FROM vrr_raw.production_volumes_daily v"
            " LEFT JOIN vrr_raw.completion c ON c.id_completion = v.id_completion"
            " WHERE c.id_completion IS NULL AND %(p)s::text IS NULL LIMIT 50"),
    }
    findings = {name: _rows(sql, params) for name, sql in checks.items()}
    n = sum(len(v) for v in findings.values())
    return {"ok": n == 0, "pattern_id": pid, "n_findings": n,
            "findings": {k: v for k, v in findings.items() if v},
            "checks_run": list(checks),
            "clean_checks": [k for k, v in findings.items() if not v]}


@tracing.trace("LIST_COMPLETIONS", span_type="TOOL")
def list_completions(pattern: str, date: str | None = None) -> dict:
    """The completions making up a pattern, with each one's role and share of the VRR.

    Producer vs injector is derived from what the completion actually contributed in the
    period (not a static well list), so a converted well shows up under the role it
    played that month. Shares are of the pattern's production / injection totals.
    """
    p = _resolve(pattern)
    if not p:
        return {"found": False, "pattern": pattern}
    pid = p["pattern_id"]
    if not date:
        rows = vrr_trend(pid)["rows"]
        if not rows:
            return {"found": False, "pattern_id": pid, "reason": "no VRR history"}
        date = str(rows[-1]["vrr_date"])

    completions = _rows(
        "SELECT id_completion AS completion_id, count(*) n_days, avg(factor) factor,"
        " string_agg(DISTINCT pvt_method, ',') pvt_methods,"
        " min(pattern_pressure_psia) min_pressure, max(pattern_pressure_psia) max_pressure,"
        " sum(oil_volume_bbl) oil, sum(water_volume_bbl) water, sum(gas_volume_kscf) gas,"
        " sum(water_inj_volume_bbl) water_inj, sum(gas_inj_volume_kscf) gas_inj,"
        " sum(res_oil_volume_bbl) oil_res, sum(res_water_volume_bbl) water_res,"
        " sum(coalesce(res_free_gas_volume_bbl,0)) free_gas_res,"
        " sum(res_water_inj_volume_bbl) water_inj_res,"
        " sum(res_gas_inj_volume_bbl) gas_inj_res FROM vrr_curated.completion_contrib"
        " WHERE id_pattern=%(p)s AND date_trunc('month', vrr_date)=%(d)s::date"
        " GROUP BY id_completion ORDER BY id_completion", {"p": pid, "d": date})

    prod_total = sum(c["oil_res"] + c["water_res"] + c["free_gas_res"] for c in completions)
    inj_total = sum(c["water_inj_res"] + c["gas_inj_res"] for c in completions)
    for c in completions:
        c["prod_res"] = c["oil_res"] + c["water_res"] + c["free_gas_res"]
        c["inj_res"] = c["water_inj_res"] + c["gas_inj_res"]
        c["role"] = ("injector" if c["inj_res"] > c["prod_res"] else
                     "producer" if c["prod_res"] > 0 else "idle")
        c["share_of_production"] = c["prod_res"] / prod_total if prod_total else 0.0
        c["share_of_injection"] = c["inj_res"] / inj_total if inj_total else 0.0
    return {
        "found": bool(completions), "pattern_id": pid, "pattern_name": p["pattern_name"],
        "vrr_date": date, "n_completions": len(completions),
        "n_producers": sum(1 for c in completions if c["role"] == "producer"),
        "n_injectors": sum(1 for c in completions if c["role"] == "injector"),
        "prod_res_bbl": prod_total, "inj_res_bbl": inj_total,
        "vrr": physics.vrr(inj_total, prod_total), "completions": completions,
        "provenance": {"table": "vrr_curated.completion_contrib",
                       "keys": {"pattern_id": pid, "month": date}},
    }


@tracing.trace("PATTERN_LAYOUT", span_type="TOOL")
def pattern_layout(pattern: str, date: str | None = None) -> dict:
    """The pattern as a picture: who injects, who produces, and how strongly they belong.

    Everything positional comes from `core.pattern_layout`, which places wells by
    contribution factor because this database holds no coordinates. What this function
    adds on top of `list_completions` is the two things the figure needs and the table
    does not: the human well name, and how many patterns each completion is split
    across — a producer shared three ways is the usual reason a VRR argument starts, and
    it is invisible in every other view.
    """
    base = list_completions(pattern, date)
    if not base.get("found"):
        return {"found": False, **{k: v for k, v in base.items() if k != "found"}}

    ids = [c["completion_id"] for c in base["completions"]]
    meta = {
        r["completion_id"]: r
        for r in _rows(
            "SELECT c.id_completion AS completion_id, c.completion_name, c.uwi,"
            " c.completion_type AS designed_type,"
            " (SELECT count(DISTINCT f.id_pattern) FROM vrr_raw.pattern_contribution_factor f"
            "  WHERE f.id_completion = c.id_completion) AS n_patterns"
            " FROM vrr_raw.completion c WHERE c.id_completion = ANY(%(ids)s)",
            {"ids": ids})
    }
    for c in base["completions"]:
        c.update({k: v for k, v in meta.get(c["completion_id"], {}).items()
                  if k != "completion_id"})

    layout = PL.build(base["completions"])
    return {
        "found": True,
        "pattern_id": base["pattern_id"], "pattern_name": base["pattern_name"],
        "vrr_date": base["vrr_date"], "vrr": base["vrr"],
        # The fluid balance the schematic is drawn around: barrels put back vs barrels
        # taken out, both at reservoir conditions. This is the whole of VRR in two numbers.
        "prod_res_bbl": base["prod_res_bbl"], "inj_res_bbl": base["inj_res_bbl"],
        **layout,
        "provenance": {
            "tables": ["vrr_curated.completion_contrib", "vrr_raw.completion",
                       "vrr_raw.pattern_contribution_factor"],
            "keys": {"pattern_id": base["pattern_id"], "month": base["vrr_date"]},
            "note": "schematic: wells placed by contribution factor, not by coordinates",
        },
    }


@tracing.trace("INPUT_AUDIT", span_type="TOOL")
def input_audit(pattern: str | None = None, verdict: str | None = None) -> dict:
    """Stored input-audit verdicts (``pipeline/input_audit.py``) — one per pattern-period.

    Cheap to read, so the agent and the portfolio view can ask "which periods are even
    trustworthy?" without recomputing anything.
    """
    pid = None
    if pattern:
        p = _resolve(pattern)
        if not p:
            return {"found": False, "pattern": pattern}
        pid = p["pattern_id"]
    rows = _rows(
        "SELECT id_pattern, pattern_name, vrr_date, verdict, actionable, summary,"
        " findings, run_id, audited_at FROM vrr_agent.input_audit"
        " WHERE (%(p)s::text IS NULL OR id_pattern = %(p)s)"
        "   AND (%(v)s::text IS NULL OR verdict = %(v)s)"
        " ORDER BY vrr_date DESC, pattern_name", {"p": pid, "v": verdict})
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    return {"found": bool(rows), "n": len(rows), "by_verdict": counts, "audits": rows,
            "provenance": {"table": "vrr_agent.input_audit"}}


@tracing.trace("PATTERN_CONTEXT", span_type="TOOL")
def pattern_context(pattern: str) -> dict:
    """Target, learned band/ρ, safety limits and prior adjustments for one pattern."""
    p = _resolve(pattern)
    if not p:
        return {"found": False, "pattern": pattern}
    pid = p["pattern_id"]
    mem = _rows("SELECT * FROM vrr_agent.pattern_memory WHERE id_pattern=%(p)s", {"p": pid})
    tgt = _rows("SELECT target_vrr FROM vrr_raw.pattern_target WHERE id_pattern=%(p)s", {"p": pid})
    return {
        "found": True, "pattern_id": pid, "pattern_name": p["pattern_name"],
        "target_vrr": (tgt[0]["target_vrr"] if tgt else CFG.default_target_vrr),
        "memory": mem[0] if mem else {},
        "safety_limits": _rows("SELECT *, id_completion AS completion_id FROM"
                               " vrr_agent.safety_limits WHERE id_pattern=%(p)s",
                               {"p": pid}),
        "adjustment_history": _rows(
            "SELECT * FROM vrr_agent.adjustment_history WHERE id_pattern=%(p)s"
            " ORDER BY ts DESC LIMIT 20", {"p": pid}),
    }


@tracing.trace("DETECT_ANOMALIES", span_type="TOOL")
def detect_anomalies(pattern: str, as_of: str | None = None) -> dict:
    """core.anomaly over the pattern's monthly history (band from memory).

    ``as_of`` truncates the history, so a period can be assessed as it looked then — and
    so the analyst can reach these rules THROUGH a traced tool rather than calling
    ``core.anomaly`` directly (a figure with no tool span behind it is unauditable).
    """
    ctx = pattern_context(pattern)
    if not ctx.get("found"):
        return {"ok": False, "reason": f"unknown pattern '{pattern}'"}
    hist = vrr_trend(ctx["pattern_id"])["rows"]
    if as_of:
        hist = [r for r in hist if str(r["vrr_date"]) <= str(as_of)]
    mem = ctx.get("memory") or {}
    band = (mem.get("typical_low"), mem.get("typical_high"))
    found = AN.detect_anomalies(hist, target_vrr=ctx["target_vrr"],
                                band=band if all(band) else None)
    return {"ok": True, "pattern_id": ctx["pattern_id"],
            "pattern_name": ctx["pattern_name"], "target_vrr": ctx["target_vrr"],
            "band": band if all(band) else None, "as_of": as_of,
            "anomalies": [a.__dict__ for a in found]}


def _injector_states(pattern_id: str, date: str) -> list[RE.InjectorState]:
    """Current injector state for the month, straight off the lineage layer.
    ``bw_inj`` is recovered as inj_res/(FACTOR·surface) — the same FVF the build used."""
    rows = _rows(
        "SELECT id_completion AS completion_id, avg(factor) factor,"
        " sum(water_inj_volume_bbl) surface, sum(res_water_inj_volume_bbl) inj_res"
        " FROM vrr_curated.completion_contrib"
        " WHERE id_pattern=%(p)s AND date_trunc('month', vrr_date)=%(d)s::date"
        " AND water_inj_volume_bbl > 0 GROUP BY id_completion ORDER BY id_completion",
        {"p": pattern_id, "d": date})
    out = []
    for r in rows:
        denom = (r["factor"] or 0) * (r["surface"] or 0)
        out.append(RE.InjectorState(
            completion_id=r["completion_id"], factor=r["factor"],
            bw_inj=(r["inj_res"] / denom) if denom else 0.0,
            water_inj_surface=r["surface"], inj_res=r["inj_res"]))
    return out


@tracing.trace("RECOMMEND_CHANGE", span_type="TOOL")
def recommend_change(pattern: str, date: str | None = None) -> dict:
    """core.recommend — bounded, ρ-calibrated injection change for one period."""
    ctx = pattern_context(pattern)
    if not ctx.get("found"):
        return {"ok": False, "reason": f"unknown pattern '{pattern}'"}
    pid = ctx["pattern_id"]
    if not date:
        rows = vrr_trend(pid)["rows"]
        if not rows:
            return {"ok": False, "reason": "no VRR history"}
        date = str(rows[-1]["vrr_date"])
    period = vrr_get(pid, date)
    if not period.get("found"):
        return {"ok": False, "reason": f"no VRR row for {pid} on {date}"}

    mem = ctx.get("memory") or {}
    limits = [lim["max_inj_rate_change_pct"] for lim in ctx["safety_limits"]
              if lim["max_inj_rate_change_pct"] is not None]
    rec = RE.recommend_injection_change(
        prod_res=period["prod_res_bbl"], inj_res=period["inj_res_bbl"],
        target_vrr=ctx["target_vrr"], injectors=_injector_states(pid, date),
        response_factor=mem.get("response_factor") or 1.0,
        max_change_pct=min(limits) if limits else RE.DEFAULT_MAX_CHANGE_PCT)
    rec.update({"pattern_id": pid, "pattern_name": ctx["pattern_name"], "vrr_date": date,
                "response_factor_source": "vrr_agent.pattern_memory",
                "safety_limit_source": "vrr_agent.safety_limits",
                "any_extrapolated": period.get("any_extrapolated")})
    return rec


def find_precedent(pattern: str, driver: str | None = None,
                   direction: str | None = None) -> dict:
    ctx = pattern_context(pattern)
    if not ctx.get("found"):
        return {"found": False}
    prec = RE.find_precedent(ctx["adjustment_history"], driver=driver, direction=direction)
    return {"found": bool(prec), **(prec or {})}


@tracing.trace("SUBMIT_FOR_APPROVAL", span_type="TOOL")
def submit_for_approval(pattern: str, date: str, *, draft: dict,
                        submitted_by: str = "agent") -> dict:
    """Write the draft into ``vrr_agent.action_queue`` at stage='draft'.

    The agent's authority ENDS here (guardrail §6): a draft is advisory, and every
    forward step (analyst → RM → site → executed) is a human act in the approval UI.
    """
    action_id = f"ACT-{uuid.uuid4().hex[:10]}"
    _execute(
        "INSERT INTO vrr_agent.action_queue (action_id, id_pattern, pattern_name,"
        " vrr_date, anomaly_kind, severity, anomaly_detail, driver, action_type,"
        " recommendation, precedent, confidence, narrative, stage, stage_by, stage_ts)"
        " VALUES (%(id)s,%(pid)s,%(name)s,%(d)s,%(kind)s,%(sev)s,%(det)s,%(drv)s,"
        " %(at)s,%(rec)s,%(prec)s,%(conf)s,%(narr)s,'draft',%(by)s, now())",
        {"id": action_id, "pid": draft.get("pattern_id", pattern),
         "name": draft.get("pattern_name"), "d": draft.get("vrr_date", date),
         "kind": draft.get("anomaly_kind"), "sev": draft.get("severity"),
         "det": draft.get("anomaly_detail"), "drv": draft.get("driver"),
         "at": draft.get("action_type"),
         "rec": json.dumps(draft.get("recommendation"), default=str),
         "prec": json.dumps(draft.get("precedent"), default=str),
         "conf": draft.get("confidence"), "narr": draft.get("narrative"),
         "by": submitted_by})
    return {"ok": True, "action_id": action_id, "stage": "draft",
            "next_approver": "analyst",
            "note": "Draft queued — advisory only until analyst → RM → site sign-off."}


@tracing.retriever_span("SEARCH_KNOWLEDGE")
def search_knowledge(query: str, k: int = 3, doc_kind: str | None = "reservoir") -> dict:
    """pgvector search over ingested reservoir docs. Needs a local embedding model.

    Traced as a RETRIEVER span carrying `mlflow.entities.Document`s, which is what makes
    the retrieval scorers (groundedness / relevance / sufficiency) able to score this
    system at all — a TOOL span is invisible to them.

    `doc_kind` defaults to `reservoir`, so the LLM-facing tool spec below — and every
    existing caller — searches the reservoir corpus only. The application user guide
    lives in the same table under `app_help` and is reached from `chat._help_answer`,
    never from the model's own tool loop: the model has no reason to answer a question
    about the reservoir out of a page describing a button.
    """
    try:
        from ..config import load_config
        from ..pipeline.knowledge_ingest import search
        # `hits` may legitimately be empty: chunks below the similarity floor are noise,
        # and reporting the floor lets the caller say WHY it is abstaining.
        return {"ok": True, "hits": search(query, k, doc_kind=doc_kind),
                "min_score": load_config().retrieval_min_score}
    except Exception as e:                     # no Ollama / no ingested docs
        return {"ok": False, "reason": f"knowledge search unavailable: {e}"}


# ---- LLM-facing specs + dispatch --------------------------------------------

def _spec(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


_PATTERN = {"pattern": {"type": "string", "description": "pattern id or name"}}
_DATE = {"date": {"type": "string", "description": "month start, YYYY-MM-DD"}}

TOOL_SPECS: list[dict[str, Any]] = [
    _spec("LIST_PATTERNS", "List patterns and their latest VRR.", {}, []),
    _spec("VRR_OVERVIEW", "Portfolio: every pattern's latest VRR vs target, ranked by drift.",
          {"asset": {"type": "string"}}, []),
    _spec("DATA_QUALITY", "Ingestion-quality checks over the raw layer (allocation, pressure, PVT).",
          {**_PATTERN}, []),
    _spec("VRR_TREND", "VRR series for a pattern, optionally date-filtered.",
          {**_PATTERN, "date_from": {"type": "string"}, "date_to": {"type": "string"}},
          ["pattern"]),
    _spec("VRR_GET", "Get a pattern's VRR + provenance on a date.",
          {**_PATTERN, **_DATE}, ["pattern", "date"]),
    _spec("VRR_DECOMPOSE", "Attribute a VRR change between two dates to its drivers.",
          {**_PATTERN, "date_a": {"type": "string"}, "date_b": {"type": "string"}},
          ["pattern", "date_a", "date_b"]),
    _spec("VRR_LINEAGE", "Show how a monthly VRR was built: completions, raw inputs, PVT.",
          {**_PATTERN, **_DATE}, ["pattern", "date"]),
    _spec("VRR_AUDIT", "Recompute a month's VRR from raw tables and diff vs the stored value.",
          {**_PATTERN, **_DATE}, ["pattern", "date"]),
    _spec("LIST_COMPLETIONS", "List the completions in a pattern with role and share of VRR.",
          {**_PATTERN, **_DATE}, ["pattern"]),
    _spec("PATTERN_LAYOUT", "The pattern's well layout: injectors, producers, contribution "
          "factors, shared completions, and which canonical shape (five-spot, nine-spot, "
          "line drive) it makes.", {**_PATTERN, **_DATE}, ["pattern"]),
    _spec("INPUT_AUDIT", "Stored input-audit verdicts: is a period's data trustworthy?",
          {**_PATTERN, "verdict": {"type": "string"}}, []),
    _spec("PATTERN_CONTEXT", "Target, learned band/rho, safety limits, adjustment history.",
          _PATTERN, ["pattern"]),
    _spec("DETECT_ANOMALIES", "Run the deterministic anomaly rules over a pattern.",
          {**_PATTERN, "as_of": {"type": "string"}}, ["pattern"]),
    _spec("RECOMMEND_CHANGE", "Compute a bounded injection change to steer VRR to target.",
          {**_PATTERN, **_DATE}, ["pattern"]),
    _spec("FIND_PRECEDENT", "Most recent executed adjustment matching a driver.",
          {**_PATTERN, "driver": {"type": "string"}}, ["pattern"]),
    _spec("SEARCH_KNOWLEDGE", "Search ingested reservoir documents.",
          {"query": {"type": "string"}}, ["query"]),
]

DISPATCH = {
    "LIST_PATTERNS": lambda a: {"patterns": list_patterns()},
    "VRR_OVERVIEW": lambda a: vrr_overview(a.get("asset")),
    "DATA_QUALITY": lambda a: data_quality(a.get("pattern")),
    "VRR_TREND": lambda a: vrr_trend(a["pattern"], a.get("date_from"), a.get("date_to")),
    "VRR_GET": lambda a: vrr_get(a["pattern"], a["date"]),
    "VRR_DECOMPOSE": lambda a: vrr_decompose(a["pattern"], a["date_a"], a["date_b"]),
    "VRR_LINEAGE": lambda a: vrr_lineage(a["pattern"], a["date"]),
    "VRR_AUDIT": lambda a: vrr_audit(a["pattern"], a["date"]),
    "LIST_COMPLETIONS": lambda a: list_completions(a["pattern"], a.get("date")),
    "PATTERN_LAYOUT": lambda a: pattern_layout(a["pattern"], a.get("date")),
    "INPUT_AUDIT": lambda a: input_audit(a.get("pattern"), a.get("verdict")),
    "PATTERN_CONTEXT": lambda a: pattern_context(a["pattern"]),
    "DETECT_ANOMALIES": lambda a: detect_anomalies(a["pattern"], a.get("as_of")),
    "RECOMMEND_CHANGE": lambda a: recommend_change(a["pattern"], a.get("date")),
    "FIND_PRECEDENT": lambda a: find_precedent(a["pattern"], a.get("driver")),
    "SEARCH_KNOWLEDGE": lambda a: search_knowledge(a["query"]),
}


@tracing.trace("tool_call", span_type="TOOL")
def call_tool(name: str, args: dict) -> dict:
    fn = DISPATCH.get(name)
    if not fn:
        return {"error": f"unknown tool {name}"}
    try:
        return fn(args)
    except Exception as e:                      # tool errors are data, not crashes
        return {"error": f"{name} failed: {e}"}
