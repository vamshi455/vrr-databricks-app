"""The streaming simulator, tested off-DB and off-broker.

Two properties carry the whole design and both are asserted here:

* **`volume_rows_for_date` is pure and order-independent.** Generating day 500 before day 3
  gives the same numbers as the reverse. That is what lets the producer emit any date in
  isolation, and it is what makes replaying a Kafka offset safe — a redelivered message
  cannot change a figure.
* **The row shape is exactly the seed's.** A streamed row and a seeded row must be
  indistinguishable once they reach `vrr_raw`, so the column tuple is asserted against
  `seed._RAW_COLUMNS` positionally. A column reorder in `seed.py` fails here rather than
  silently writing gas into the water column.
"""
from __future__ import annotations

import datetime as dt

from vrr_agent_open.pipeline import seed
from vrr_agent_open.streaming import generate
from vrr_agent_open.streaming.clock import SimClock
from vrr_agent_open.streaming.generate import BaseRate

D = dt.date(2026, 8, 1)


def rates(n_prod: int = 2, n_inj: int = 1) -> dict[str, BaseRate]:
    out = {}
    for i in range(n_prod):
        out[f"P{i}"] = BaseRate(f"P{i}", "PAT", "producer",
                                base_oil=200.0, base_water=600.0, base_gas=120.0)
    for i in range(n_inj):
        out[f"I{i}"] = BaseRate(f"I{i}", "PAT", "injector", base_inj=1800.0)
    return out


RAMP = {"PAT": (0.03, 24)}


# ---- purity ----------------------------------------------------------------

def test_same_date_gives_identical_rows():
    r = rates()
    assert generate.volume_rows_for_date(D, r, RAMP) == \
           generate.volume_rows_for_date(D, r, RAMP)


def test_different_dates_give_different_noise():
    r = rates()
    a = generate.volume_rows_for_date(D, r, RAMP)
    b = generate.volume_rows_for_date(D + dt.timedelta(days=1), r, RAMP)
    assert a != b


def test_generation_is_order_independent():
    """The whole point of the keyed RNG. Generating a late date first must not change
    what an earlier date produces — that is what a sequential generator cannot promise,
    and what makes offset replay safe."""
    r = rates()
    forward = [generate.volume_rows_for_date(D + dt.timedelta(days=i), r, RAMP)
               for i in range(5)]
    backward = [generate.volume_rows_for_date(D + dt.timedelta(days=i), r, RAMP)
                for i in reversed(range(5))]
    assert forward == list(reversed(backward))


def test_noise_stays_inside_the_declared_envelope():
    r = rates(n_prod=1, n_inj=1)
    i = generate.month_index(D)
    decline = generate.MONTHLY_DECLINE ** i
    rows = {x[0]: x for x in generate.volume_rows_for_date(D, r, RAMP)}
    oil = rows["P0"][2] / (200.0 * decline)
    assert 1 - generate.PRODUCER_NOISE <= oil <= 1 + generate.PRODUCER_NOISE


# ---- shape and roles -------------------------------------------------------

def test_columns_match_the_seed_exactly():
    """Positional, not by name — the tuples are written by position."""
    assert generate.COLUMNS == seed._RAW_COLUMNS["production_volumes_daily"]


def test_producers_carry_no_injection_and_injectors_no_production():
    rows = generate.volume_rows_for_date(D, rates(), RAMP)
    prod = [r for r in rows if r[0].startswith("P")]
    inj = [r for r in rows if r[0].startswith("I")]
    assert prod and inj
    assert all(r[5] == 0.0 and r[6] == 0.0 for r in prod)      # no injection
    assert all(r[2] == 0.0 and r[3] == 0.0 and r[4] == 0.0 for r in inj)


def test_one_row_per_completion():
    r = rates(n_prod=7, n_inj=3)
    assert len(generate.volume_rows_for_date(D, r, RAMP)) == 10


# ---- the ramp, which is what eventually pushes a pattern off target --------

def test_injection_tracks_decline_until_the_ramp_starts():
    """Before ramp_start_month the injector follows production down; after it, it
    deviates. A pattern with inj_ramp=0 must stay on target for its whole history."""
    r = rates(n_prod=0, n_inj=1)
    flat = {"PAT": (0.0, 0)}
    early = dt.date(2023, 9, 1)                       # month 1, before any ramp
    i = generate.month_index(early)
    row = generate.volume_rows_for_date(early, r, flat)[0]
    expected = 1800.0 * generate.MONTHLY_DECLINE ** i
    assert abs(row[5] / expected - 1) <= generate.INJECTOR_NOISE


def test_month_index_counts_from_the_fields_first_month():
    assert generate.month_index(seed.START) == 0
    assert generate.month_index(dt.date(2026, 7, 1)) == 35      # last seeded month
    assert generate.month_index(dt.date(2026, 8, 1)) == 36      # first streamed month


# ---- the simulated clock ---------------------------------------------------

def test_clock_yields_the_rate_it_promises():
    c = SimClock(start_date=D, days_per_second=4.0, t0=0.0)
    assert c.due(now=1.0) == [D + dt.timedelta(days=i) for i in range(4)]


def test_clock_catches_up_after_a_stall_instead_of_losing_days():
    """A slow tick owes several days. Dropping them would silently create a gap in the
    history, which is exactly the failure a stream must not have."""
    c = SimClock(start_date=D, days_per_second=4.0, t0=0.0)
    c.due(now=1.0)
    assert len(c.due(now=4.0)) == 12                  # 3 stalled seconds, all owed


def test_clock_never_repeats_or_skips_a_date():
    c = SimClock(start_date=D, days_per_second=10.0, t0=0.0)
    seen = [d for t in (0.35, 0.7, 1.0, 2.0) for d in c.due(now=t)]
    assert seen == sorted(seen)
    assert len(seen) == len(set(seen))


def test_clock_stops_at_max_days():
    c = SimClock(start_date=D, days_per_second=100.0, max_days=3, t0=0.0)
    assert len(c.due(now=1.0)) == 3
    assert c.finished and c.due(now=2.0) == []


def test_event_time_spreads_a_day_across_that_day():
    """Without spreading, every row in a simulated day shares one timestamp and a
    watermark has nothing to advance through inside the day."""
    c = SimClock(start_date=D, days_per_second=1.0, t0=0.0)
    first, last = c.event_time(D, 0, 4), c.event_time(D, 3, 4)
    assert first.date() == D and last.date() == D
    assert first < last
