"""Persist the calibrated base rates, so a single day can be generated in isolation.

`seed._setup()` computes a per-completion daily rate for oil, water, gas and injection, then
calibrates each pattern's injection so month 0 lands on its VRR target. `generate_raw` uses
those numbers and drops them on the floor — they live in local scope and reach no table.

That is the only reason the streaming producer cannot simply ask for one day. Running the
setup phase once and writing the result to `vrr_stream.completion_base_rate` fixes it for
good: after bootstrap, generating any date is a table read and some arithmetic.

Bootstrap is idempotent and safe to re-run. It does NOT touch `vrr_raw` — unlike
`seed.load()`, which opens with a TRUNCATE of seven tables.
"""
from __future__ import annotations

import psycopg

from ..config import load_config
from ..pipeline.seed import N_PATTERNS, SEED, _setup
from .generate import BaseRate

CFG = load_config()

_UPSERT = """
INSERT INTO vrr_stream.completion_base_rate
  (id_completion, id_pattern, role, base_oil, base_water, base_gas, base_inj, seed)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (id_completion) DO UPDATE SET
  id_pattern = EXCLUDED.id_pattern, role = EXCLUDED.role,
  base_oil = EXCLUDED.base_oil, base_water = EXCLUDED.base_water,
  base_gas = EXCLUDED.base_gas, base_inj = EXCLUDED.base_inj,
  seed = EXCLUDED.seed, built_at = now()
"""


def base_rate_rows(seed: int = SEED, n_patterns: int = N_PATTERNS) -> list[tuple]:
    """Run the setup phase and flatten it into rows. Pure apart from the CPU it burns."""
    s = _setup(seed=seed, n_patterns=n_patterns)
    rows: list[tuple] = []
    for pid, completions in s.producers_of.items():
        for c in completions:
            rows.append((c, pid, "producer", s.base_oil[c], s.base_water[c],
                         s.base_gas[c], 0.0, seed))
    for pid, completions in s.injectors_of.items():
        for c in completions:
            # base_inj is post-calibration here: _setup applies the per-pattern scale
            # that puts month 0 on target, and that scaled value is the one worth keeping.
            rows.append((c, pid, "injector", 0.0, 0.0, 0.0, s.base_inj[c], seed))
    return rows


def bootstrap(conn=None, seed: int = SEED, n_patterns: int = N_PATTERNS) -> int:
    """Compute and store every completion's base rate. Returns the row count."""
    rows = base_rate_rows(seed, n_patterns)
    own = conn is None
    conn = conn or psycopg.connect(CFG.pg_dsn)
    try:
        with conn.cursor() as cur:
            cur.executemany(_UPSERT, rows)
        conn.commit()
    finally:
        if own:
            conn.close()
    return len(rows)


def load_base_rates(conn=None) -> dict[str, BaseRate]:
    """Read the base rates back. The producer calls this once and caches the result."""
    own = conn is None
    conn = conn or psycopg.connect(CFG.pg_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id_completion, id_pattern, role, base_oil, base_water,"
                        " base_gas, base_inj FROM vrr_stream.completion_base_rate")
            return {r[0]: BaseRate(*r) for r in cur.fetchall()}
    finally:
        if own:
            conn.close()


def main() -> None:
    n = bootstrap()
    print(f"base rates stored for {n} completions → vrr_stream.completion_base_rate")


if __name__ == "__main__":
    main()
