"""One date in, one list of volume rows out. Pure — no I/O, no shared state.

This is the piece `pipeline/seed.py` could not give us. `generate_raw` is deterministic,
but it is not *decomposable*: a single `random.Random(seed)` is consumed in strict sequence
through completions → PVT → pressure → base rates → allocation → the day loop, so day N's
noise depends on every draw before it. You cannot ask it for one day.

The arithmetic underneath, though, is stateless once the calibrated base rates are known:

    decline = 0.995 ** month_index                    ~0.5%/month production decline
    ramp    = decline * (1 + inj_ramp) ** drift       injection deviates after ramp_start

So this module takes the base rates as an argument (see `rates.py`, which persists them)
and draws its noise from a generator keyed on `(seed, completion, date)` instead of a
shared sequence. Two consequences that matter for streaming:

* **Any date can be generated in isolation** — the producer never replays history.
* **Replaying an offset reproduces byte-identical volumes**, which is what makes
  at-least-once delivery safe. Redelivering a message cannot change a number.

The row shape is exactly `seed._RAW_COLUMNS["production_volumes_daily"]`, so a streamed row
and a seeded row are indistinguishable once they land in `vrr_raw`.
"""
from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass

from ..pipeline.seed import SEED, START

# Same knobs the seed generator uses. Kept as module constants rather than magic numbers
# in the expressions so the two files can be diffed against each other.
MONTHLY_DECLINE = 0.995
PRODUCER_NOISE = 0.05      # ±5% daily, uniform
INJECTOR_NOISE = 0.04      # ±4% daily, uniform

COLUMNS = ("id_completion", "prod_date", "alloc_oil_vol_stb", "alloc_water_vol_stb",
           "alloc_gas_vol_kscf", "alloc_water_inj_vol_stb", "alloc_gas_inj_vol_kscf",
           "uom")


@dataclass(frozen=True)
class BaseRate:
    """One completion's calibrated daily rates, as persisted by `rates.py`."""
    id_completion: str
    id_pattern: str
    role: str                  # producer | injector
    base_oil: float = 0.0
    base_water: float = 0.0
    base_gas: float = 0.0
    base_inj: float = 0.0


def month_index(date: dt.date, start: dt.date = START) -> int:
    """Months elapsed since the field's first month — the exponent in the decline."""
    return (date.year - start.year) * 12 + (date.month - start.month)


def noise(seed: int, id_completion: str, date: dt.date, spread: float) -> float:
    """A multiplier in [1-spread, 1+spread], stable for this (completion, date).

    Keyed rather than sequential: the same completion on the same day always draws the
    same number, no matter what was generated before it or in what order. That is the
    whole trick — it buys isolation and replay-safety at once.
    """
    rng = random.Random(f"{seed}|{id_completion}|{date.isoformat()}")
    return 1.0 + rng.uniform(-spread, spread)


def volume_rows_for_date(
    date: dt.date,
    rates: dict[str, BaseRate],
    ramp: dict[str, tuple[float, int]],
    *,
    seed: int = SEED,
    start: dt.date = START,
) -> list[tuple]:
    """Every completion's row for one production date.

    `ramp` maps id_pattern → (inj_ramp, ramp_start_month) from the pattern spec; injection
    follows the production decline until `ramp_start_month`, then drifts away from it,
    which is what eventually pushes a pattern off its VRR target.
    """
    i = month_index(date, start)
    decline = MONTHLY_DECLINE ** i
    rows: list[tuple] = []

    for r in rates.values():
        if r.role == "producer":
            n = noise(seed, r.id_completion, date, PRODUCER_NOISE)
            rows.append((r.id_completion, date,
                         round(r.base_oil * decline * n, 3),
                         round(r.base_water * decline * n, 3),
                         round(r.base_gas * decline * n, 3),
                         0.0, 0.0, "OilField"))
        else:
            inj_ramp, ramp_start = ramp.get(r.id_pattern, (0.0, 0))
            drift = max(0, i - ramp_start)
            n = noise(seed, r.id_completion, date, INJECTOR_NOISE)
            rows.append((r.id_completion, date, 0.0, 0.0, 0.0,
                         round(r.base_inj * decline * (1.0 + inj_ramp) ** drift * n, 3),
                         0.0, "OilField"))
    return rows


def ramp_map(specs) -> dict[str, tuple[float, int]]:
    """`pattern_specs()` → the two fields the injector arithmetic needs."""
    return {s.id_pattern: (s.inj_ramp, s.ramp_start_month) for s in specs}
