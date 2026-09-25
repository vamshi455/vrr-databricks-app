"""Deterministic reservoir math — the trust core of the VRR agent.

Governing principle (docs/vrr_agent_master_design §Governing principles): **the LLM
never does arithmetic.** Every VRR number is produced here, deterministically, with
provenance. This module is pure Python (no Spark / no I/O) so it unit-tests
off-cluster and the same functions run inside the Spark builders and the tools.

Two responsibilities:
  1. ``pvt_lookup`` — interpolate PVT properties (Bo, Bw, Bg, Rs, Rv, injection
     FVFs) at a pattern pressure, and label the method exact/interpolated/
     **extrapolated** — that label IS the confidence flag surfaced to the engineer.
  2. ``completion_contribution`` — the per-completion reservoir-volume terms
     (oil_res, water_res, free_gas_res, water_inj_res, gas_inj_res) from FACTOR,
     raw volumes, and the interpolated FVFs.

Unit note (prototype): raw volumes are STB (oil/water) / KSCF (gas); FVFs convert
to reservoir barrels. We apply ``volume * FVF`` and treat the product as reservoir
bbl — dimensionally simplified for the prototype (the Snowflake builder does the
rigorous unit handling). VRR is a *ratio* so consistent simplification cancels.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# PVT property keys interpolated at pressure. Producer FVFs + solution/volatilized
# ratios, plus the injection FVFs (evaluated at the same pattern pressure).
PVT_KEYS = ("bo", "bw", "bg", "rs", "rv", "bw_inj", "bg_inj")

# Method labels, aligned to the production vrr_sql_builder's CalculatedPVT ladder.
EXACT, INTERP, EXTRAP, CLOSEST, NONE = (
    "exact", "interpolated", "extrapolated", "closest", "none")
# Bg is stored DECIMAL(_,5) in RMDE; round to match (vrr_sql_builder CHECKPOINT 8).
BG_ROUND_DP = 5


@dataclass(frozen=True)
class PVTPoint:
    """One measured PVT row (a lab test) for a completion, keyed by pressure."""
    pressure_psi: float
    bo: float = 1.0
    bw: float = 1.0
    bg: float = 0.0
    rs: float = 0.0
    rv: float = 0.0
    bw_inj: float = 1.0
    bg_inj: float = 0.0
    test_date: Optional[str] = None


@dataclass(frozen=True)
class PVTResult:
    """Interpolated PVT at a target pressure + the confidence method."""
    props: dict
    method: str                      # exact | interpolated | extrapolated
    bracket: tuple = field(default=())  # (lo_pressure, hi_pressure) used
    note: str = ""

    @property
    def is_low_confidence(self) -> bool:
        return self.method in (EXTRAP, CLOSEST, NONE)


def _lerp(x0: float, y0: float, x1: float, y1: float, x: float) -> float:
    if x1 == x0:
        return y0
    t = (x - x0) / (x1 - x0)
    return y0 + t * (y1 - y0)


def pvt_lookup(points: list[PVTPoint], pressure_psi: Optional[float]) -> PVTResult:
    """Interpolate/extrapolate PVT at ``pressure_psi`` from a completion's PVT points.

    Faithful to the production ``vrr_sql_builder`` CalculatedPVT ladder (NO DEFAULTS):
      1. ``exact``        — a measured point equals the target pressure.
      2. ``interpolated`` — linear between the nearest lower & upper bounds (in-range ✅).
      3. ``extrapolated`` — outside range: **2-point linear extrapolation** using the
         two nearest points on that side (not a flat clamp).
      4. ``closest``      — only one point on one side and no second → use it (low conf).
      5. ``none``         — no points → all props NULL.
    ``Bg`` is rounded to 5 dp to match RMDE storage. ``props`` always has every key.
    """
    if not points:
        return PVTResult(props={k: None for k in PVT_KEYS}, method=NONE,
                         note="no PVT points for completion")
    pts = sorted(points, key=lambda p: p.pressure_psi)
    if pressure_psi is None:
        p = pts[-1]
        return PVTResult(props=_round_bg({k: getattr(p, k) for k in PVT_KEYS}),
                         method=CLOSEST, note="no pattern pressure; used latest PVT point")

    lower = [p for p in pts if p.pressure_psi < pressure_psi]   # ascending
    upper = [p for p in pts if p.pressure_psi > pressure_psi]   # ascending
    exact = next((p for p in pts if p.pressure_psi == pressure_psi), None)

    if exact is not None:
        return PVTResult(props=_round_bg({k: getattr(exact, k) for k in PVT_KEYS}),
                         method=EXACT, bracket=(exact.pressure_psi, exact.pressure_psi))
    if lower and upper:                                   # interpolate (nearest each side)
        a, b = lower[-1], upper[0]
        props = {k: _lerp(a.pressure_psi, getattr(a, k), b.pressure_psi, getattr(b, k),
                          pressure_psi) for k in PVT_KEYS}
        return PVTResult(props=_round_bg(props), method=INTERP,
                         bracket=(a.pressure_psi, b.pressure_psi))
    if lower and len(lower) >= 2:                          # extrapolate below (2 lower)
        a, b = lower[-1], lower[-2]                        # nearest, second-nearest
        props = {k: _lerp(a.pressure_psi, getattr(a, k), b.pressure_psi, getattr(b, k),
                          pressure_psi) for k in PVT_KEYS}
        return PVTResult(props=_round_bg(props), method=EXTRAP,
                         bracket=(b.pressure_psi, a.pressure_psi))
    if upper and len(upper) >= 2:                          # extrapolate above (2 upper)
        a, b = upper[0], upper[1]
        props = {k: _lerp(a.pressure_psi, getattr(a, k), b.pressure_psi, getattr(b, k),
                          pressure_psi) for k in PVT_KEYS}
        return PVTResult(props=_round_bg(props), method=EXTRAP,
                         bracket=(a.pressure_psi, b.pressure_psi))
    closest = (lower[-1] if lower else upper[0])           # single point on one side
    return PVTResult(props=_round_bg({k: getattr(closest, k) for k in PVT_KEYS}),
                     method=CLOSEST, bracket=(closest.pressure_psi, closest.pressure_psi))


def _round_bg(props: dict) -> dict:
    if props.get("bg") is not None:
        props["bg"] = round(props["bg"], BG_ROUND_DP)
    return props


@dataclass(frozen=True)
class ContribTerms:
    """Per-completion reservoir-volume contributions (all in reservoir bbl).

    ``free_gas_res`` is ``None`` for non-producing / OIL=0 rows (gated out), and is
    treated as 0 in the pattern sum — matching the SQL builder's NULL-excluded-from-SUM.
    """
    oil_res: float
    water_res: float
    free_gas_res: Optional[float]
    water_inj_res: float
    gas_inj_res: float

    @property
    def prod_res(self) -> float:
        return self.oil_res + self.water_res + (self.free_gas_res or 0.0)

    @property
    def inj_res(self) -> float:
        return self.water_inj_res + self.gas_inj_res


def _num(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# Unit reconciliation to reservoir barrels:
#   OIL/WATER are STB; Bo/Bw/Bw_inj are rb/STB       -> STB·(rb/STB) = rb   (direct)
#   GAS/GAS_INJ are KSCF; Bg/Bg_inj are rb/scf       -> need scf, so KSCF·1000
#   Rs (SOLUTION_GAS_OIL_RATIO) is scf/STB           -> Rs·OIL already in scf
# So free gas (scf) = GAS·1000 − Rs·OIL, then ·Bg (rb/scf). Set gas_kscf_to_scf=1
# if the raw gas column is already scf, or adjust for the _metric SM3 variant.
GAS_KSCF_TO_SCF = 1000.0


def completion_contribution(
    *,
    factor: float,
    oil: float,
    water: float,
    gas: float,
    water_inj: float,
    gas_inj: float,
    pvt: dict,
    is_producer: Optional[bool] = None,
    gas_kscf_to_scf: float = GAS_KSCF_TO_SCF,
) -> ContribTerms:
    """Reservoir-volume terms for one completion on one date (design §1).

        oil_res       = FACTOR · OIL                     · Bo
        water_res     = FACTOR · WATER                   · Bw
        free_gas_res  = FACTOR · (GAS·1000 − Rs·OIL)     · Bg      (producers only; may be < 0)
        water_inj_res = FACTOR · WATER_INJ               · Bw_inj
        gas_inj_res   = FACTOR · GAS_INJ·1000            · Bg_inj

    Free gas (scf) = surface gas minus the gas dissolved in the produced oil
    (Rs·OIL), converted to reservoir bbl by Bg (rb/scf) — see ``GAS_KSCF_TO_SCF``.
    ``pvt`` is the ``props`` dict from :func:`pvt_lookup`. Missing FVFs (None) are
    treated as 0 for that term — the missing-input flag lives on the contrib row,
    not silently in the number.
    """
    f = _num(factor)
    bo, bw, bg = _num(pvt.get("bo")), _num(pvt.get("bw")), _num(pvt.get("bg"))
    rs = _num(pvt.get("rs"))
    bw_inj, bg_inj = _num(pvt.get("bw_inj")), _num(pvt.get("bg_inj"))

    # Amount_Type = Production iff any surface production volume > 0 (vrr_sql_builder
    # CHECKPOINT 3). Free gas is computed ONLY for producing rows with OIL_VOL > 0
    # and non-null Rs/Bg — otherwise NULL (excluded from the pattern sum), matching
    # legacy `((GAS/NULLIF(OIL,0)) - Rs) * OIL`. `is_producer` overrides when given.
    is_production = is_producer if is_producer is not None else (
        _num(oil) + _num(water) + _num(gas)) > 0

    oil_res = f * _num(oil) * bo
    water_res = f * _num(water) * bw
    if is_production and _num(oil) > 0 and pvt.get("rs") is not None and pvt.get("bg") is not None:
        free_gas_scf = _num(gas) * gas_kscf_to_scf - rs * _num(oil)
        free_gas_res = f * free_gas_scf * bg          # negative allowed
    else:
        free_gas_res = None
    water_inj_res = f * _num(water_inj) * bw_inj
    gas_inj_res = f * _num(gas_inj) * gas_kscf_to_scf * bg_inj
    return ContribTerms(oil_res, water_res, free_gas_res, water_inj_res, gas_inj_res)


def vrr(inj_res: float, prod_res: float) -> Optional[float]:
    """VRR = INJ_RES / PROD_RES. None when production is zero (undefined, not 0)."""
    p = _num(prod_res)
    if p == 0:
        return None
    return _num(inj_res) / p
