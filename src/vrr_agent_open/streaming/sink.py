"""The idempotent write into `vrr_stream.volume_events`.

Everything downstream of the broker depends on one property: **writing the same event
twice must be a no-op.** Kafka delivers at-least-once, Spark re-runs a `foreachBatch` after
a driver restart, and a replay from offset zero re-delivers the entire topic. None of that
can be allowed to change a number.

`(id_completion, event_date)` is the natural key and the `ON CONFLICT` target. Combined
with `generate.noise()` being keyed rather than sequential — so a replayed date reproduces
byte-identical volumes — redelivery genuinely cannot corrupt anything.

`n_deliveries` is incremented on conflict rather than ignored. It is the cheapest possible
evidence of at-least-once actually happening: a non-zero `sum(n_deliveries - 1)` is
redelivery you can point at in the database.

COPY is deliberately not used here. `pipeline/dbio.copy_rows` is faster, but COPY cannot
upsert, and correctness under redelivery is worth more than throughput at this scale.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import psycopg

from ..config import load_config

CFG = load_config()

UPSERT = """
INSERT INTO vrr_stream.volume_events
  (id_completion, event_date, alloc_oil_vol_stb, alloc_water_vol_stb,
   alloc_gas_vol_kscf, alloc_water_inj_vol_stb, alloc_gas_inj_vol_kscf, uom,
   emitted_ts, ingest_ts, partition, "offset")
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (id_completion, event_date) DO UPDATE SET
  alloc_oil_vol_stb       = EXCLUDED.alloc_oil_vol_stb,
  alloc_water_vol_stb     = EXCLUDED.alloc_water_vol_stb,
  alloc_gas_vol_kscf      = EXCLUDED.alloc_gas_vol_kscf,
  alloc_water_inj_vol_stb = EXCLUDED.alloc_water_inj_vol_stb,
  alloc_gas_inj_vol_kscf  = EXCLUDED.alloc_gas_inj_vol_kscf,
  ingest_ts  = EXCLUDED.ingest_ts,
  stored_ts  = clock_timestamp(),
  partition  = EXCLUDED.partition,
  "offset"   = EXCLUDED."offset",
  n_deliveries = vrr_stream.volume_events.n_deliveries + 1
"""


def connect():
    """A single long-lived connection.

    Every helper in this repo (`api/db.py`, `agent/tools.py`) opens a fresh connection per
    call, which is fine for a request and wrong for a hot loop — at a few hundred rows a
    second the connect handshake would dominate. Callers hold one of these open.
    """
    return psycopg.connect(CFG.pg_dsn)


def to_event_row(row: tuple, emitted_ts: dt.datetime, ingest_ts: dt.datetime,
                 partition: int | None = None, offset: int | None = None) -> tuple:
    """A generator row (the 8-tuple) plus the wall clocks the row was observed on."""
    return (*row, emitted_ts, ingest_ts, partition, offset)


def upsert_events(conn, rows: Sequence[tuple]) -> int:
    """Write a batch. Returns the number of rows offered, not the number changed —
    under replay those differ, and that difference is the point."""
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(UPSERT, rows)
    conn.commit()
    return len(rows)


def redelivery_count(conn) -> int:
    """How many times at-least-once actually delivered something twice."""
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(sum(n_deliveries - 1), 0) "
                    "FROM vrr_stream.volume_events")
        return int(cur.fetchone()[0])
