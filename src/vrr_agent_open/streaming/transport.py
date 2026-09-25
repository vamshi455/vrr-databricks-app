"""Where generated rows go — straight to Postgres, or through Kafka.

The interface exists so Phase 0 is runnable before a broker or a JVM is installed. The
producer never learns which backend it has; swapping `direct` for `kafka` changes one
command-line flag and nothing else.

`DirectTransport` is not a toy. It writes through the same `sink.upsert_events` the Kafka
consumer will use, so the idempotency and the row shape are exercised from the first day —
only the *transport* is missing, not the pipeline. When Kafka arrives it slots in beside
this, and the difference in the latency numbers between the two is itself the lesson.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Protocol

from . import sink


class Transport(Protocol):
    """Accepts a batch of generator rows stamped with the wall clock."""

    def send(self, rows: list[tuple], emitted_ts: dt.datetime) -> int: ...
    def close(self) -> None: ...


class DirectTransport:
    """Straight into `vrr_stream.volume_events`. No broker, no JVM, no offsets.

    `ingest_ts` equals `emitted_ts` here because nothing sits between the two — which is
    exactly what the latency chart should show: a direct write has no transit stage. The
    moment Kafka is introduced the two separate, and the gap is the broker.
    """

    def __init__(self, conn=None):
        self.conn = conn or sink.connect()
        self._owned = conn is None

    def send(self, rows: list[tuple], emitted_ts: dt.datetime) -> int:
        now = dt.datetime.now(dt.timezone.utc)
        return sink.upsert_events(
            self.conn,
            [sink.to_event_row(r, emitted_ts, now) for r in rows])

    def close(self) -> None:
        if self._owned:
            self.conn.close()


class KafkaTransport:
    """Publish to the topic, keyed so a completion's history stays ordered.

    Imported lazily: `confluent_kafka` is an optional dependency and nothing in the core
    pipeline may require it.
    """

    def __init__(self, bootstrap: str, topic: str, key_by: str = "completion"):
        from confluent_kafka import Producer

        self.topic = topic
        self.key_by = key_by
        self.producer = Producer({
            "bootstrap.servers": bootstrap,
            "acks": "all",
            "enable.idempotence": True,
            "linger.ms": 20,
            "compression.type": "lz4",
        })

    def send(self, rows: list[tuple], emitted_ts: dt.datetime) -> int:
        stamp = emitted_ts.isoformat()
        for r in rows:
            payload = {
                "id_completion": r[0], "event_date": r[1].isoformat(),
                "alloc_oil_vol_stb": r[2], "alloc_water_vol_stb": r[3],
                "alloc_gas_vol_kscf": r[4], "alloc_water_inj_vol_stb": r[5],
                "alloc_gas_inj_vol_kscf": r[6], "uom": r[7],
                "emitted_ts": stamp,
            }
            self.producer.produce(self.topic, key=r[0].encode(),
                                  value=json.dumps(payload).encode())
        # poll() drives the delivery callbacks; without it the queue fills and produce()
        # eventually raises BufferError, which IS the producer-side backpressure signal.
        self.producer.poll(0)
        return len(rows)

    @property
    def queue_depth(self) -> int:
        """Messages buffered but not yet acknowledged — backpressure, quantified."""
        return len(self.producer)

    def close(self) -> None:
        self.producer.flush(10)
