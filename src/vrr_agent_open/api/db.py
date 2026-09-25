"""The API's only direct SQL — everything else goes through `agent/tools.py`.

Two queries need raw SQL because they are UI concerns rather than agent tools: the
approval queue listing and the executed-adjustment history. Keeping them here (instead
of adding tools for them) preserves the rule that a *tool* is something the LLM may call
— the queue is a human workflow, and the model has no business advancing it.
"""
from __future__ import annotations

from typing import Any

import psycopg

from ..config import load_config

CFG = load_config()


def query(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
    with psycopg.connect(CFG.pg_dsn, row_factory=psycopg.rows.dict_row) as c, c.cursor() as cur:
        cur.execute(sql, params or {})
        return cur.fetchall() if cur.description else []


def execute(sql: str, params: dict) -> None:
    with psycopg.connect(CFG.pg_dsn) as c, c.cursor() as cur:
        cur.execute(sql, params)
        c.commit()
