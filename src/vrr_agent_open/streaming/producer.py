"""The simulator loop: clock → generate → transport.

Run it:

    make stream-produce                     # direct to Postgres, until Ctrl-C
    make stream-produce days=30 rate=8      # 30 simulated days at 8 days/sec
    make stream-produce transport=kafka     # once a broker exists

It starts the day after the seeded history ends, so it extends the field forward rather
than competing with the demo data. Restarting resumes from wherever the data got to —
`--from-date` forces a replay, which is how the idempotency claim gets tested.
"""
from __future__ import annotations

import argparse
import datetime as dt
import signal
import time

import psycopg

from ..config import load_config
from ..pipeline.seed import SEED, pattern_specs
from . import generate, rates, sink
from .clock import SimClock
from .transport import DirectTransport, KafkaTransport

CFG = load_config()
_stop = False


def _on_signal(*_):
    global _stop
    _stop = True
    print("\nstopping…")


def next_date(conn) -> dt.date:
    """Resume point: the day after the furthest date anything has reached.

    Checks the streaming table AND `vrr_raw`, so the very first run picks up from the
    seeded history (2026-07-31) rather than restarting three years ago.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT greatest("
                    " (SELECT coalesce(max(event_date), '1900-01-01') FROM vrr_stream.volume_events),"
                    " (SELECT coalesce(max(prod_date),  '1900-01-01') FROM vrr_raw.production_volumes_daily))")
        return cur.fetchone()[0] + dt.timedelta(days=1)


def run(*, rate: float, days: int, transport_name: str,
        from_date: dt.date | None = None) -> dict:
    conn = psycopg.connect(CFG.pg_dsn)
    base = rates.load_base_rates(conn)
    if not base:
        raise SystemExit("no base rates — run `make stream-bootstrap` first")
    ramp = generate.ramp_map(pattern_specs())
    start = from_date or next_date(conn)

    transport = (KafkaTransport(CFG.kafka_bootstrap, CFG.kafka_topic)
                 if transport_name == "kafka" else DirectTransport(conn))
    clock = SimClock(start_date=start, days_per_second=rate, max_days=days)

    print(f"simulating from {start} at {rate} sim-days/sec via {transport_name} "
          f"({len(base)} completions/day){'' if days else ' — Ctrl-C to stop'}")
    sent = t_last = 0
    t0 = time.monotonic()
    try:
        while not _stop and not clock.finished:
            for d in clock.due():
                rows = generate.volume_rows_for_date(d, base, ramp, seed=SEED)
                sent += transport.send(rows, dt.datetime.now(dt.timezone.utc))
            now = time.monotonic()
            if now - t_last >= 2.0:                       # progress, not a hot spin
                el = now - t0
                print(f"  {clock.frontier}  {sent:,} rows  {sent/max(el,1e-9):,.0f} rows/s")
                t_last = now
            time.sleep(0.02)
    finally:
        transport.close()
        el = time.monotonic() - t0
        conn.close()
    return {"rows": sent, "seconds": round(el, 1), "through": clock.frontier}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rate", type=float, default=4.0, help="simulated days per second")
    ap.add_argument("--days", type=int, default=0, help="stop after N days (0 = forever)")
    ap.add_argument("--transport", choices=("direct", "kafka"), default="direct")
    ap.add_argument("--from-date", type=dt.date.fromisoformat, default=None,
                    help="replay from this date instead of resuming")
    a = ap.parse_args()
    signal.signal(signal.SIGINT, _on_signal)
    out = run(rate=a.rate, days=a.days, transport_name=a.transport, from_date=a.from_date)
    print(f"\n{out['rows']:,} rows in {out['seconds']}s — history now through "
          f"{out['through'] - dt.timedelta(days=1)}")
    with sink.connect() as c:
        print(f"redelivered rows (at-least-once evidence): {sink.redelivery_count(c):,}")


if __name__ == "__main__":
    main()
