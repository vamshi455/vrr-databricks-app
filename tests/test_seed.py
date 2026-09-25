"""Off-DB tests for the synthetic field (`pipeline/seed.py`).

These never touch Postgres: ``generate_raw`` is pure, so the builder's math is re-run here
with ``core.physics`` — including the **windowed, many-to-many** contribution-factor join
and the pressure window, which is where the data model actually lives.

Two fixtures, both small enough to stay fast:
  SCRIPTED  3 patterns × 36 months — the scenario assertions (drift needs the full history)
  SHARED    10 patterns × 6 months — the allocation shapes (sharing only affects noise patterns)
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

import pytest

from vrr_agent_open.config import TARGET_BAND
from vrr_agent_open.core import ids, physics
from vrr_agent_open.pipeline import seed


def _windows(rows: list[tuple], key_idx: tuple, date_idx: int):
    """Turn (…, effect_date) rows into half-open windows, as LEAD() does in SQL."""
    by_key = defaultdict(list)
    for r in rows:
        by_key[tuple(r[i] for i in key_idx)].append(r)
    for key, rs in by_key.items():
        rs.sort(key=lambda r: r[date_idx])
        for i, r in enumerate(rs):
            end = rs[i + 1][date_idx] if i + 1 < len(rs) else None
            yield key, r, r[date_idx], end


def _monthly_vrr(raw: seed.RawData) -> dict[tuple[str, dt.date], dict]:
    """Re-implement the builder in memory: volumes ⋈ factor window ⋈ pressure window."""
    pvt: dict[tuple, list] = defaultdict(list)
    for (cid, tdate, psi, bo, bg, bw, bg_inj, bw_inj, rs, rv) in \
            raw.completion_pvt_characteristics:
        pvt[(cid, tdate)].append(physics.PVTPoint(
            pressure_psi=psi, bo=bo, bw=bw, bg=bg, rs=rs, rv=rv,
            bw_inj=bw_inj, bg_inj=bg_inj))
    tests = sorted({k[1] for k in pvt})

    pressure_windows = list(_windows(raw.pattern_pressure, (0,), 1))
    volumes = defaultdict(list)
    for row in raw.production_volumes_daily:
        volumes[row[0]].append(row)

    agg: dict[tuple[str, dt.date], dict] = {}
    for (cid, pid), frow, f_start, f_end in _windows(raw.pattern_contribution_factor,
                                                     (0, 1), 3):
        factor = frow[2]
        for (_, vdate, oil, water, gas, water_inj, gas_inj, uom) in volumes[cid]:
            if vdate < f_start or (f_end is not None and vdate >= f_end):
                continue                                    # outside this factor window
            pressure = next((p[2] for (kp,), p, s, e in pressure_windows
                             if kp == pid and s <= vdate and (e is None or vdate < e)), None)
            applicable = [d for d in tests if d <= vdate]
            tdate = applicable[-1] if applicable else tests[0]
            look = physics.pvt_lookup(pvt[(cid, tdate)], pressure)
            t = physics.completion_contribution(
                factor=factor, oil=oil, water=water, gas=gas, water_inj=water_inj,
                gas_inj=gas_inj, pvt=look.props, is_producer=(oil + water + gas) > 0,
                gas_kscf_to_scf=1000.0 if uom == "OilField" else 1.0)
            a = agg.setdefault((pid, vdate.replace(day=1)),
                               {"prod": 0.0, "inj": 0.0, "extrap": False})
            a["prod"] += t.prod_res
            a["inj"] += t.inj_res
            a["extrap"] = a["extrap"] or look.method in (physics.EXTRAP, physics.CLOSEST,
                                                         physics.NONE)
    for a in agg.values():
        a["vrr"] = physics.vrr(a["inj"], a["prod"])
    return agg


SCRIPTED = seed.generate_raw(n_patterns=3, n_months=36)
MONTHLY = _monthly_vrr(SCRIPTED)
SPECS = {s.pattern_name: s for s in seed.pattern_specs(3)}

SHARED = seed.generate_raw(n_patterns=10, n_months=6)


def _series(pattern_name: str) -> list[float]:
    pid = SPECS[pattern_name].id_pattern
    return [a["vrr"] for (p, _), a in sorted(MONTHLY.items()) if p == pid]


def test_generation_is_deterministic():
    assert seed.generate_raw(n_patterns=3, n_months=36).production_volumes_daily == \
        SCRIPTED.production_volumes_daily


def test_all_ids_are_surrogate_hex_keys():
    assert all(ids.is_valid(r[0]) for r in SCRIPTED.pattern)
    assert all(ids.is_valid(r[0]) for r in SCRIPTED.completion)
    assert all(ids.is_valid(r[0]) and ids.is_valid(r[1])
               for r in SCRIPTED.pattern_contribution_factor)


def test_volumes_are_keyed_by_completion_not_pattern():
    """The data-model rule: a volume row knows nothing about patterns — membership comes
    from pattern_contribution_factor."""
    row = SCRIPTED.production_volumes_daily[0]
    assert len(row) == 8 and ids.is_valid(row[0]) and isinstance(row[1], dt.date)
    assert {len(r) for r in SCRIPTED.pattern_contribution_factor} == {4}


def test_completion_registry_covers_every_volume():
    registered = {r[0] for r in SCRIPTED.completion}
    assert {r[0] for r in SCRIPTED.production_volumes_daily} <= registered
    assert {r[4] for r in SCRIPTED.completion} <= {"producer", "injector"}


# ---- allocation shapes (many-to-many) --------------------------------------------

def test_completions_are_shared_across_patterns():
    per_completion = defaultdict(set)
    for cid, pid, _factor, _eff in SHARED.pattern_contribution_factor:
        per_completion[cid].add(pid)
    shared = [c for c, ps in per_completion.items() if len(ps) > 1]
    assert shared, "expected some producers to feed more than one pattern"
    assert max(len(ps) for ps in per_completion.values()) >= 2


def test_allocation_never_exceeds_one_hundred_percent():
    """Σ FACTOR across patterns for a completion, in any window, must stay ≤ 1."""
    starts = defaultdict(float)
    for cid, _pid, factor, eff in SHARED.pattern_contribution_factor:
        starts[(cid, eff)] += factor
    assert max(starts.values()) <= 1.0001


def test_some_completions_have_multiple_windows():
    windows = defaultdict(int)
    for cid, pid, _f, _e in SHARED.pattern_contribution_factor:
        windows[(cid, pid)] += 1
    assert max(windows.values()) >= 2, "expected a mid-life split change or migration"


def test_scripted_patterns_keep_dedicated_allocation():
    """The three scenario patterns must not be perturbed by sharing."""
    scripted_ids = {s.id_pattern for s in seed.pattern_specs(3)}
    per_completion = defaultdict(set)
    for cid, pid, _f, _e in SCRIPTED.pattern_contribution_factor:
        per_completion[cid].add(pid)
    assert all(ps <= scripted_ids and len(ps) == 1 for ps in per_completion.values())


# ---- scenarios --------------------------------------------------------------------

def test_covers_every_pattern_and_month():
    months = {m for _, m in MONTHLY}
    assert len(months) == 36
    assert {p for p, _ in MONTHLY} == {s.id_pattern for s in seed.pattern_specs(3)}


def test_patterns_start_on_target():
    for name, spec in SPECS.items():
        assert _series(name)[0] == pytest.approx(spec.target_vrr, rel=0.05)


def test_unity_behaves_then_drifts_out_of_band():
    series = _series("UNITY")
    start_month = SPECS["UNITY"].ramp_start_month
    assert max(series[:start_month]) <= TARGET_BAND[1]      # behaves for two years
    assert series[-1] > TARGET_BAND[1]                      # then out of band
    assert series[-1] > series[start_month]                 # monotone drift after the change


def test_horizon_stays_inside_the_band_for_its_whole_life():
    lo, hi = TARGET_BAND
    assert all(lo <= v <= hi for v in _series("HORIZON"))


def test_meridian_goes_extrapolated_as_pressure_falls():
    pid = SPECS["MERIDIAN"].id_pattern
    flags = [a["extrap"] for (p, _), a in sorted(MONTHLY.items()) if p == pid]
    assert flags[0] is False and flags[-1] is True


def test_agent_seed_rows_are_consistent():
    rows = seed.agent_rows(n_patterns=3)
    specs = seed.pattern_specs(3)
    assert {r[0] for r in rows["pattern_memory"]} == {s.id_pattern for s in specs}
    assert len(rows["safety_limits"]) == sum(s.n_injectors for s in specs)
    assert all(0 < r[2] <= 1 for r in rows["safety_limits"])       # max change pct
    assert all(ids.is_valid(r[1]) for r in rows["safety_limits"])  # completion IDs
    assert rows["adjustment_history"][0][-1] == "executed"         # precedent exists
