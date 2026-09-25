"""Streaming — production volumes that ARRIVE instead of existing.

`pipeline/seed.py` writes three years of daily volumes in one shot and then nothing ever
changes. This package makes the same data arrive a day at a time, through a broker, so the
pipeline itself becomes the thing under study: partitions, consumer groups, offsets,
watermarks, end-to-end latency, backpressure.

The seeded history ends 2026-07-31, so the simulator starts the day after and runs forward.
It never competes with the demo data — it extends it.

Layout, in the order data moves:

    rates.py      bootstrap + read the calibrated per-completion base rates
    generate.py   PURE — one date in, one list of volume rows out
    clock.py      the simulated clock (N simulated days per real second)
    transport.py  where rows go: direct to Postgres, or to Kafka
    producer.py   the loop that ties those four together
    sink.py       idempotent upsert into vrr_stream.volume_events
    consumer.py   Kafka → sink, with manual offset commits
    spark_job.py  the same, as Spark Structured Streaming
    promote.py    vrr_stream → vrr_raw, then the curated rebuild
    metrics.py    lag, latency and throughput, sourced not invented
"""
