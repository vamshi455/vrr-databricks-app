"""Synthetic VRR field generator + loader (`make seed`), shaped like a real ingestion.

Follows the production data model ([docs/vrr_data_model.md](../../../docs/vrr_data_model.md)):

  vrr_raw.pattern                         pattern registry (16-char hex IDs + name + asset)
  vrr_raw.completion                      completion registry (hex ID, UWI, type, asset)
  vrr_raw.production_volumes_daily        allocated daily volumes per COMPLETION only
  vrr_raw.pattern_contribution_factor     completion→pattern allocation, TIME-WINDOWED,
                                          MANY-TO-MANY (a producer can feed 2–3 patterns)
  vrr_raw.pattern_pressure                pattern datum pressure, time-windowed
  vrr_raw.completion_pvt_characteristics  lab PVT per (completion, test_date, pressure)
  vrr_raw.pattern_target                  per-pattern target VRR (local addition)
  vrr_agent.pattern_memory / safety_limits / adjustment_history

Then it runs the deterministic builder so `vrr_curated` is computed by ``core.physics``,
never invented here.

Scale is parameterised (`VRR_SEED_PATTERNS`, default 40; `VRR_SEED_MONTHS`, default 36)
so tests generate a small field while `make seed` builds a realistic one. Loading uses
`COPY` (see `pipeline/dbio.py`) because at 40 patterns this is ~260k volume rows.

Three named patterns keep the scripted scenarios the agent demo depends on:
  UNITY    over-injection ramp → VRR drifts out of band (out_of_band + sustained_drift)
  HORIZON  healthy — stays inside the band (the negative control)
  MERIDIAN pressure falls below its PVT test range → extrapolated lookups → suspect inputs
The remaining patterns are ordinary noise, so portfolio views and scale are real.

Allocation shapes generated (this is the part the old toy seed could not express):
  * dedicated       one completion → one pattern, FACTOR < 1 (partial participation)
  * static split    a producer feeds 2–3 patterns, FACTORs summing to ≤ 1
  * split change    the same producer's split changes mid-history (two windows)
  * migration       a producer moves wholly from one pattern to another
Only PRODUCERS are shared; injectors stay dedicated, which keeps each pattern's
month-0 injection calibration independent (see ``_calibrate_injection``).
"""
from __future__ import annotations

import datetime as dt
import os
import random
from dataclasses import dataclass, field

import psycopg

from ..config import load_config
from ..core import ids
from ..core import physics
from . import build
from .dbio import copy_rows

CFG = load_config()

SEED = 20260724                                     # fixed → `make seed` is reproducible
START = dt.date(2023, 8, 1)
N_MONTHS = int(os.environ.get("VRR_SEED_MONTHS", "36"))
N_PATTERNS = int(os.environ.get("VRR_SEED_PATTERNS", "40"))
SHARED_PRODUCER_FRACTION = 0.30                     # ~30% of producers feed >1 pattern

ASSETS = ("NORTH_DOME", "SOUTH_FLANK", "WEST_RIDGE", "EAST_TERRACE")
NOISE_NAMES = (
    "ORION", "VEGA", "LYRA", "ATLAS", "CASTOR", "POLLUX", "RIGEL", "MIRA", "ALTAIR",
    "DENEB", "SIRIUS", "ANTARES", "CANOPUS", "ARCTURUS", "CAPELLA", "SPICA", "BELLATRIX",
    "ELECTRA", "MAIA", "TAYGETA", "ALCYONE", "MEROPE", "ASTERION", "CHARA", "IZAR",
    "MIZAR", "ALIOTH", "PHECDA", "MEGREZ", "DUBHE", "MERAK", "ALKAID", "THUBAN",
    "KOCHAB", "PHERKAD", "YILDUN", "ALIFA", "ZOSMA", "CHERTAN", "REGULUS",
)

# One PVT campaign per completion at the start of history; a second campaign would just
# add another test_date (the builder picks the latest test on or before the volume date).
PVT_TEST_DATE = START - dt.timedelta(days=30)


@dataclass(frozen=True)
class PatternSpec:
    id_pattern: str
    pattern_name: str
    asset: str
    n_producers: int
    n_injectors: int
    target_vrr: float
    pressure_start: float
    pressure_slope: float           # psi per month (negative = depleting)
    # Injection TRACKS production decline, so `inj_ramp` is the *deviation* from
    # tracking — the physically meaningful knob. 0 = voidage stays replaced (VRR flat);
    # +0.03 = injecting 3%/month more than production justifies (VRR climbs).
    inj_ramp: float
    ramp_start_month: int           # the deviation only begins here (a pattern that behaved, then changed)
    pvt_pressures: tuple            # measured PVT test pressures for its completions
    tendencies: str
    scripted: bool = False


def pattern_specs(n_patterns: int = N_PATTERNS, seed: int = SEED) -> tuple[PatternSpec, ...]:
    """The three scripted scenario patterns, then noise patterns up to ``n_patterns``."""
    def pid(name: str) -> str:
        return ids.hex_id("pattern", name)

    scripted = [
        # over-injection starts two years in: behaves, then drifts out of band
        PatternSpec(pid("UNITY"), "UNITY", ASSETS[0], 3, 2, 1.0, 3200.0, -12.0, 0.028, 24,
                    (2600.0, 2900.0, 3200.0, 3500.0),
                    "responds quickly to water injection cuts; watch the north injector",
                    scripted=True),
        # the negative control: injection tracks production, VRR stays in band
        PatternSpec(pid("HORIZON"), "HORIZON", ASSETS[0], 3, 2, 1.0, 2800.0, -6.0, 0.0008, 0,
                    (2400.0, 2700.0, 3000.0), "stable; no adjustments on record",
                    scripted=True),
        # depletes past the bottom of its PVT range → extrapolated lookups → suspect inputs
        PatternSpec(pid("MERIDIAN"), "MERIDIAN", ASSETS[1], 2, 1, 1.0, 2500.0, -12.0, 0.004, 12,
                    (2300.0, 2600.0, 2900.0), "sparse PVT coverage; audit inputs first",
                    scripted=True),
    ]
    rng = random.Random(seed ^ 0x5EED)
    noise = []
    for i, name in enumerate(NOISE_NAMES[:max(0, n_patterns - len(scripted))]):
        p0 = round(rng.uniform(2200.0, 3600.0), 1)
        noise.append(PatternSpec(
            pid(name), name, ASSETS[i % len(ASSETS)],
            rng.randint(2, 5), rng.randint(1, 3), 1.0,
            p0, round(rng.uniform(-14.0, -2.0), 1),
            round(rng.uniform(-0.004, 0.005), 4),      # small deviations either side
            rng.choice((0, 6, 12, 18)),
            tuple(round(p0 + d + rng.uniform(-40, 40), 1) for d in (-500, -200, 150, 450)),
            "", scripted=False))
    return tuple(scripted[:n_patterns] + noise)


@dataclass
class RawData:
    """The `vrr_raw` tables, as row tuples ready for COPY."""
    pattern: list = field(default_factory=list)
    completion: list = field(default_factory=list)
    production_volumes_daily: list = field(default_factory=list)
    pattern_contribution_factor: list = field(default_factory=list)
    pattern_pressure: list = field(default_factory=list)
    completion_pvt_characteristics: list = field(default_factory=list)
    pattern_target: list = field(default_factory=list)


@dataclass
class Setup:
    """Everything `generate_raw` computes BEFORE the day loop.

    Split out because the streaming producer needs the calibrated per-completion base
    rates without regenerating three years of daily volumes. `rng` travels with it: the
    day loop continues consuming the SAME generator, so extracting this changed no
    random draw and `generate_raw` still returns byte-identical rows.
    """
    raw: RawData
    specs: tuple
    months: list
    producers_of: dict
    injectors_of: dict
    base_oil: dict
    base_water: dict
    base_gas: dict
    base_inj: dict
    rng: random.Random


def _month_starts(start: dt.date, n: int) -> list[dt.date]:
    out, y, m = [], start.year, start.month
    for _ in range(n):
        out.append(dt.date(y, m, 1))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _days_in_month(d: dt.date) -> int:
    nxt = dt.date(d.year + 1, 1, 1) if d.month == 12 else dt.date(d.year, d.month + 1, 1)
    return (nxt - d).days


def _calibrate_injection(*, producers, injectors, pvt_points, pressure, factor,
                         base_oil, base_water, base_gas, base_inj,
                         start_vrr: float) -> float:
    """Scale factor for month-0 injection so the pattern STARTS at its target VRR.

    Runs the real ``core.physics`` terms on the noise-free base rates, so the field starts
    on target regardless of the random draw and the drift afterwards is purely the spec's
    ramp. ``factor`` here is each completion's allocation *to this pattern*, so shared
    producers are counted at their share.
    """
    prod_res = inj_res = 0.0
    for c in producers:
        pvt = physics.pvt_lookup(pvt_points[c], pressure)
        prod_res += physics.completion_contribution(
            factor=factor.get(c, 0.0), oil=base_oil[c], water=base_water[c],
            gas=base_gas[c], water_inj=0.0, gas_inj=0.0, pvt=pvt.props,
            is_producer=True).prod_res
    for c in injectors:
        pvt = physics.pvt_lookup(pvt_points[c], pressure)
        inj_res += physics.completion_contribution(
            factor=factor.get(c, 0.0), oil=0.0, water=0.0, gas=0.0, water_inj=base_inj[c],
            gas_inj=0.0, pvt=pvt.props, is_producer=False).inj_res
    if inj_res <= 0 or prod_res <= 0:
        return 1.0
    return start_vrr * prod_res / inj_res


def _setup(seed: int = SEED, n_months: int = N_MONTHS, start: dt.date = START,
           n_patterns: int = N_PATTERNS) -> Setup:
    """Registries, PVT, pressure, allocation and the calibrated base rates.

    Everything up to but not including the daily volume loop. Pure and deterministic.
    """
    rng = random.Random(seed)
    raw = RawData()
    months = _month_starts(start, n_months)
    specs = pattern_specs(n_patterns, seed)
    history_open = start - dt.timedelta(days=1)     # first allocation window opens here

    # ---- completions, PVT, pressure, base rates -----------------------------------
    pvt_points: dict[str, list] = {}
    producers_of: dict[str, list[str]] = {}
    injectors_of: dict[str, list[str]] = {}
    base_oil: dict[str, float] = {}
    base_water: dict[str, float] = {}
    base_gas: dict[str, float] = {}
    base_inj: dict[str, float] = {}

    for spec in specs:
        raw.pattern.append((spec.id_pattern, spec.pattern_name, spec.asset))
        raw.pattern_target.append((spec.id_pattern, spec.target_vrr))

        prods, injs = [], []
        for i in range(spec.n_producers):
            cid = ids.hex_id("completion", spec.pattern_name, "P", i + 1)
            prods.append(cid)
            raw.completion.append((cid, f"{spec.pattern_name}-P{i+1}",
                                   f"42-{rng.randint(100, 499)}-{rng.randint(10000, 99999)}",
                                   spec.asset, "producer"))
        for i in range(spec.n_injectors):
            cid = ids.hex_id("completion", spec.pattern_name, "I", i + 1)
            injs.append(cid)
            raw.completion.append((cid, f"{spec.pattern_name}-I{i+1}",
                                   f"42-{rng.randint(100, 499)}-{rng.randint(10000, 99999)}",
                                   spec.asset, "injector"))
        producers_of[spec.id_pattern], injectors_of[spec.id_pattern] = prods, injs

        # PVT: Bo/Bw/Bg/Rs move in the usual directions with pressure. Rv = 0 (no
        # volatile-oil variant in this field — see the gap noted in vrr_data_model.md).
        for cid in prods + injs:
            jitter = 1.0 + rng.uniform(-0.02, 0.02)
            for p in spec.pvt_pressures:
                bo = round((1.18 + 0.00004 * (p - 2500.0)) * jitter, 5)
                bw = round(1.02 * jitter, 5)
                bg = round(0.00090 * (2500.0 / p), 5)
                rs = round(520.0 + 0.09 * (p - 2500.0), 3)
                bw_inj, bg_inj, rv = round(1.01 * jitter, 5), bg, 0.0
                raw.completion_pvt_characteristics.append(
                    (cid, PVT_TEST_DATE, p, bo, bg, bw, bg_inj, bw_inj, rs, rv))
                pvt_points.setdefault(cid, []).append(physics.PVTPoint(
                    pressure_psi=p, bo=bo, bw=bw, bg=bg, rs=rs, rv=rv,
                    bw_inj=bw_inj, bg_inj=bg_inj))

        for i, m in enumerate(months):              # pressure reading per month
            raw.pattern_pressure.append((spec.id_pattern, m, round(
                spec.pressure_start + spec.pressure_slope * i + rng.uniform(-8, 8), 1)))

        for c in prods:
            base_oil[c] = rng.uniform(180, 320)
            base_water[c] = rng.uniform(400, 900)
            base_gas[c] = rng.uniform(90, 180)      # KSCF
        for c in injs:
            base_inj[c] = rng.uniform(1400, 2200)

    # ---- allocation: dedicated, shared, split-change, migration --------------------
    # alloc[(completion, pattern)] = list of (factor, effect_date) windows
    alloc: dict[tuple[str, str], list[tuple[float, dt.date]]] = {}

    def allocate(cid: str, pid: str, factor: float, effect: dt.date) -> None:
        alloc.setdefault((cid, pid), []).append((round(factor, 2), effect))

    mid_history = months[len(months) // 2]
    # Sharing happens among the noise patterns; the three scripted scenarios keep
    # dedicated allocation so their stories stay clean and demo-stable.
    scripted_ids = {s_.id_pattern for s_ in specs if s_.scripted}
    all_producers = [(pid, c) for pid, cs in producers_of.items() for c in cs
                     if pid not in scripted_ids]
    for pid_, cs in producers_of.items():
        if pid_ in scripted_ids:
            for c in cs:
                alloc.setdefault((c, pid_), []).append((round(rng.uniform(0.4, 1.0), 2),
                                                        history_open))
    n_shared = int(len(all_producers) * SHARED_PRODUCER_FRACTION)
    shared = set(rng.sample(range(len(all_producers)), n_shared)) if n_shared else set()
    pattern_ids = [s.id_pattern for s in specs]

    for idx, (home, cid) in enumerate(all_producers):
        if idx not in shared or len(pattern_ids) < 2:
            allocate(cid, home, rng.uniform(0.4, 1.0), history_open)     # dedicated
            continue
        others = [p for p in pattern_ids if p != home and p not in scripted_ids]
        shape = idx % 3
        if shape == 0:                                                   # static split
            total = rng.uniform(0.7, 1.0)
            neighbours = rng.sample(others, k=min(2, len(others)))
            splits = [0.6, 0.4] if len(neighbours) == 2 else [1.0]
            allocate(cid, home, total * 0.5, history_open)
            for nb, part in zip(neighbours, splits):
                allocate(cid, nb, total * 0.5 * part, history_open)
        elif shape == 1:                                                 # split change
            nb = rng.choice(others)
            allocate(cid, home, 0.50, history_open)
            allocate(cid, nb, 0.50, history_open)
            allocate(cid, home, 0.30, mid_history)      # second window: split moves
            allocate(cid, nb, 0.70, mid_history)
        else:                                                            # migration
            nb = rng.choice(others)
            allocate(cid, home, rng.uniform(0.6, 1.0), history_open)
            allocate(cid, home, 0.0, mid_history)       # window closes (contributes 0)
            allocate(cid, nb, rng.uniform(0.6, 1.0), mid_history)

    for pid, cs in injectors_of.items():                                 # injectors: dedicated
        for c in cs:
            allocate(c, pid, rng.uniform(0.4, 1.0), history_open)

    for (cid, pid), windows in alloc.items():
        for factor, effect in windows:
            raw.pattern_contribution_factor.append((cid, pid, factor, effect))

    # ---- calibrate each pattern's month-0 injection to its target VRR --------------
    for spec in specs:
        # a completion's allocation to THIS pattern in the first window
        first = {}
        for (cid, pid), windows in alloc.items():
            if pid != spec.id_pattern:
                continue
            first[cid] = sorted(windows, key=lambda w: w[1])[0][0]
        contributors = [c for c in first if c in base_oil]
        p0 = next(p for (pp, d, p) in raw.pattern_pressure
                  if pp == spec.id_pattern and d == months[0])
        scale = _calibrate_injection(
            producers=contributors, injectors=injectors_of[spec.id_pattern],
            pvt_points=pvt_points, pressure=p0, factor=first, base_oil=base_oil,
            base_water=base_water, base_gas=base_gas, base_inj=base_inj,
            start_vrr=spec.target_vrr)
        for c in injectors_of[spec.id_pattern]:
            base_inj[c] *= scale

    return Setup(raw=raw, specs=specs, months=months,
                 producers_of=producers_of, injectors_of=injectors_of,
                 base_oil=base_oil, base_water=base_water,
                 base_gas=base_gas, base_inj=base_inj, rng=rng)


def generate_raw(seed: int = SEED, n_months: int = N_MONTHS, start: dt.date = START,
                 n_patterns: int = N_PATTERNS) -> RawData:
    """Build the synthetic raw field. Pure: deterministic given the parameters."""
    s = _setup(seed, n_months, start, n_patterns)
    raw, rng, months, specs = s.raw, s.rng, s.months, s.specs
    producers_of, injectors_of = s.producers_of, s.injectors_of
    base_oil, base_water = s.base_oil, s.base_water
    base_gas, base_inj = s.base_gas, s.base_inj

    # ---- daily volumes, keyed by COMPLETION only ----------------------------------
    for spec in specs:
        for i, m in enumerate(months):
            decline = 0.995 ** i                    # ~0.5%/month production decline
            # injection follows the decline, then deviates by inj_ramp once the drift
            # starts — so a flat pattern stays on target for its whole history
            drift_months = max(0, i - spec.ramp_start_month)
            ramp = decline * (1.0 + spec.inj_ramp) ** drift_months
            for day in range(_days_in_month(m)):
                d = m + dt.timedelta(days=day)
                for c in producers_of[spec.id_pattern]:
                    n = 1.0 + rng.uniform(-0.05, 0.05)          # daily noise
                    raw.production_volumes_daily.append((
                        c, d, round(base_oil[c] * decline * n, 3),
                        round(base_water[c] * decline * n, 3),
                        round(base_gas[c] * decline * n, 3), 0.0, 0.0, "OilField"))
                for c in injectors_of[spec.id_pattern]:
                    raw.production_volumes_daily.append((
                        c, d, 0.0, 0.0, 0.0,
                        round(base_inj[c] * ramp * (1.0 + rng.uniform(-0.04, 0.04)), 3),
                        0.0, "OilField"))
    return raw


# ---------------------------------------------------------------------------
# agent-schema seeds: safety limits bound recommendations, and one executed
# adjustment gives core.recommend.find_precedent something to cite.
# ---------------------------------------------------------------------------

def agent_rows(n_patterns: int = N_PATTERNS, seed: int = SEED) -> dict[str, list[tuple]]:
    specs = pattern_specs(n_patterns, seed)
    memory = [(s.id_pattern, s.pattern_name, None, None, 0.90, 1.10, 1.0, 0,
               s.tendencies or None) for s in specs]
    limits = []
    for s in specs:
        for i in range(s.n_injectors):
            limits.append((s.id_pattern, ids.hex_id("completion", s.pattern_name, "I", i + 1),
                           0.15, 3800.0, 0.72,
                           "prototype limit — replace with the field's completion limits"))
    unity = specs[0]
    history = [(
        "ACT-SEED-UNITY-01", unity.id_pattern, unity.pattern_name,
        START + dt.timedelta(days=120), "res_water_inj_volume_bbl", "out_of_band",
        "reduce_injection", -1800.0, -0.10, 1.18, 1.06, 1.08,
        "approved", "reservoir.manager", "executed")]
    return {"pattern_memory": memory, "safety_limits": limits,
            "adjustment_history": history}


# ---------------------------------------------------------------------------
# loaders (COPY — see pipeline/dbio.py)
# ---------------------------------------------------------------------------

_RAW_COLUMNS = {
    "pattern": ("id_pattern", "pattern_name", "asset"),
    "completion": ("id_completion", "completion_name", "uwi", "asset", "completion_type"),
    "production_volumes_daily": (
        "id_completion", "prod_date", "alloc_oil_vol_stb", "alloc_water_vol_stb",
        "alloc_gas_vol_kscf", "alloc_water_inj_vol_stb", "alloc_gas_inj_vol_kscf", "uom"),
    "pattern_contribution_factor": ("id_completion", "id_pattern", "factor", "effect_date"),
    "pattern_pressure": ("id_pattern", "pressure_date", "pressure"),
    "completion_pvt_characteristics": (
        "id_completion", "test_date", "pressure", "bo", "bg", "bw", "bg_inj", "bw_inj",
        "rs", "rv"),
    "pattern_target": ("id_pattern", "target_vrr"),
}

_AGENT_COLUMNS = {
    "pattern_memory": ("id_pattern", "pattern_name", "latest_vrr", "latest_date",
                       "typical_low", "typical_high", "response_factor", "n_adjustments",
                       "tendencies"),
    "safety_limits": ("id_pattern", "id_completion", "max_inj_rate_change_pct",
                      "max_inj_pressure", "fracture_gradient", "note"),
    "adjustment_history": ("action_id", "id_pattern", "pattern_name", "vrr_date", "driver",
                           "anomaly", "change_type", "d_inj_res_bbl", "d_surface_pct",
                           "pre_vrr", "predicted_post_vrr", "actual_post_vrr", "decision",
                           "approved_by", "outcome"),
}


def load(raw: RawData, conn, n_patterns: int = N_PATTERNS) -> dict[str, int]:
    """Replace `vrr_raw` + the seeded `vrr_agent` tables with this synthetic field."""
    counts: dict[str, int] = {}
    with conn.cursor() as cur:
        cur.execute("TRUNCATE vrr_raw.production_volumes_daily, vrr_raw.pattern,"
                    " vrr_raw.completion, vrr_raw.pattern_contribution_factor,"
                    " vrr_raw.pattern_pressure, vrr_raw.completion_pvt_characteristics,"
                    " vrr_raw.pattern_target")
        cur.execute("TRUNCATE vrr_agent.safety_limits, vrr_agent.adjustment_history")
        cur.execute("DELETE FROM vrr_agent.pattern_memory")
    for table, cols in _RAW_COLUMNS.items():
        counts[f"vrr_raw.{table}"] = copy_rows(conn, f"vrr_raw.{table}", cols,
                                               getattr(raw, table))
    for table, rows in agent_rows(n_patterns).items():
        counts[f"vrr_agent.{table}"] = copy_rows(conn, f"vrr_agent.{table}",
                                                 _AGENT_COLUMNS[table], rows)
    conn.commit()
    return counts


def refresh_memory(conn) -> int:
    """Point pattern_memory at the freshly built curated layer (latest VRR + date)."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE vrr_agent.pattern_memory m SET latest_vrr = s.vrr_bblbbl,
                   latest_date = s.vrr_date, updated_at = now()
            FROM (SELECT DISTINCT ON (id_pattern) id_pattern, vrr_date, vrr_bblbbl
                    FROM vrr_curated.pattern_vrr WHERE grain = 'monthly'
                   ORDER BY id_pattern, vrr_date DESC) s
            WHERE m.id_pattern = s.id_pattern
        """)
        n = cur.rowcount
    conn.commit()
    return n


def main() -> None:
    print(f"  generating {N_PATTERNS} patterns × {N_MONTHS} months …")
    raw = generate_raw()
    with psycopg.connect(CFG.pg_dsn) as conn:
        for t, n in load(raw, conn).items():
            print(f"  loaded {n:>7} rows → {t}")
        for t, n in build.run(conn).items():
            print(f"  built  {n:>7} rows → {t}")
        print(f"  refreshed pattern_memory for {refresh_memory(conn)} patterns")


if __name__ == "__main__":
    main()
