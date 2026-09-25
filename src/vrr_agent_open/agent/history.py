"""Durable analyst chat transcript in Postgres (`vrr_agent.chat_history`).

Deliberately NOT part of `tools.py`. Everything there is registered in `TOOL_SPECS` /
`DISPATCH` — the LLM's menu — and wrapped in a TOOL span. Chat history is unverified
prose: letting the model read prior answers back would break the trust model (every
number must trace to a tool with provenance), and a history write showing up as a TOOL
span would be scored by the deterministic trace scorers in `evaluation/`.

`log_turn` is called ONLY from the API layer, never from `chat.respond()`:
`scripts/create_traces.py` calls `respond()` for all ten evaluation questions, and
logging there would write synthetic rows into the shared transcript on every
`make traces`.

Scope is per PATTERN and shared across users — opening a pattern shows what anyone
already asked about it, so a review is not restarted from zero.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import psycopg

from ..config import load_config

CFG = load_config()

MAX_PAYLOAD_CHARS = 100_000     # VRR_TREND/VRR_LINEAGE return whole series; cap the row

# Kept byte-identical to the block in pipeline/schema.sql. That file is only mounted as
# a Postgres init script (docker-compose), so it never reaches an already-created
# database — this copy is what makes the table exist for everyone else.
_DDL = """
CREATE TABLE IF NOT EXISTS vrr_agent.chat_history (
  chat_id text PRIMARY KEY, id_pattern text NOT NULL, pattern_name text, vrr_date date,
  question text NOT NULL, answer text, intent text, agentic boolean DEFAULT false,
  llm_used boolean, model text, gate text, tools_called jsonb, meta jsonb, payload jsonb,
  asked_by text, deleted_at timestamptz, run_id text, created_at timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chat_history_pattern_created_idx
  ON vrr_agent.chat_history (id_pattern, created_at DESC);
"""


def _connect(**kw):
    return psycopg.connect(CFG.pg_dsn, **kw)


def ensure_table() -> None:
    """Idempotent DDL. Cheap enough to call once per process; never per rerun."""
    with _connect() as c, c.cursor() as cur:
        cur.execute(_DDL)
        c.commit()


def _json(value: Any, limit: int | None = None) -> str | None:
    """jsonb payload as text, truncated to a sane row size (same style as tools.py)."""
    if value in (None, {}, []):
        return None
    blob = json.dumps(value, default=str)
    if limit and len(blob) > limit:
        return json.dumps({"truncated": True, "chars": len(blob),
                           "keys": sorted(value)[:20] if isinstance(value, dict) else None},
                          default=str)
    return blob


def log_turn(*, pattern_id: str, pattern_name: str | None, date: str | None,
             question: str, result: dict, asked_by: str | None,
             agentic: bool = False) -> str:
    """Record one question+answer turn. Returns the chat_id.

    `result` is whatever `chat.respond()` returned. Its `meta` keys are branch-dependent
    and every one of them is optional, so read them with .get() and keep the whole dict
    in the `meta` jsonb column — the promoted columns are only for querying.
    """
    meta = result.get("meta") or {}
    chat_id = f"CHT-{uuid.uuid4().hex[:10]}"
    with _connect() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO vrr_agent.chat_history (chat_id, id_pattern, pattern_name,"
            " vrr_date, question, answer, intent, agentic, llm_used, model, gate,"
            " tools_called, meta, payload, asked_by)"
            " VALUES (%(id)s,%(pid)s,%(name)s,%(d)s,%(q)s,%(a)s,%(i)s,%(ag)s,%(llm)s,"
            " %(model)s,%(gate)s,%(tools)s,%(meta)s,%(payload)s,%(by)s)",
            {"id": chat_id, "pid": pattern_id, "name": pattern_name, "d": date or None,
             "q": question, "a": result.get("text"), "i": result.get("intent"),
             "ag": bool(agentic), "llm": bool(meta.get("llm")),
             "model": meta.get("model"), "gate": meta.get("gate"),
             "tools": _json(meta.get("tools_called")), "meta": _json(meta),
             "payload": _json(result.get("data"), MAX_PAYLOAD_CHARS), "by": asked_by})
        c.commit()
    return chat_id


def recent(pattern_id: str, limit: int = 50, since=None) -> list[dict]:
    """This pattern's turns, oldest first, so the drawer reads like a conversation.

    `since` is the session-local "hide history in this view" cutoff — the rows stay in
    the table (they are shared with everyone else), they just stop being rendered here.
    """
    with _connect(row_factory=psycopg.rows.dict_row) as c, c.cursor() as cur:
        cur.execute(
            "SELECT * FROM (SELECT * FROM vrr_agent.chat_history"
            " WHERE id_pattern=%(p)s AND deleted_at IS NULL"
            "   AND (%(s)s::timestamptz IS NULL OR created_at > %(s)s)"
            " ORDER BY created_at DESC LIMIT %(k)s) t ORDER BY created_at",
            {"p": pattern_id, "k": limit, "s": since})
        return cur.fetchall()
