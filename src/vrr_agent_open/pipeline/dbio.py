"""Bulk-load helpers. `COPY`, not `executemany`.

At demo scale (3 patterns) row-by-row inserts were fine. At field scale — ~260k raw
volume rows and ~370k contribution rows — `executemany` is the bottleneck and the
all-rows-in-a-list pattern is a memory problem too. Everything here streams.
"""
from __future__ import annotations

from typing import Iterable, Iterator, Sequence


def copy_rows(conn, table: str, columns: Sequence[str],
              rows: Iterable[Sequence]) -> int:
    """`COPY` an iterable of row tuples into ``table``. Returns the row count."""
    cols = ", ".join(columns)
    n = 0
    with conn.cursor() as cur:
        with cur.copy(f"COPY {table} ({cols}) FROM STDIN") as cp:
            for row in rows:
                cp.write_row(row)
                n += 1
    return n


def chunked(cursor, size: int = 50_000) -> Iterator[list]:
    """Read a server-side cursor in batches, so a big result never lands in one list."""
    while True:
        batch = cursor.fetchmany(size)
        if not batch:
            return
        yield batch
