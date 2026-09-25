"""Live process facts — the same snapshot `/api/health` and the `status` intent both read.

One probe, two callers. A model is never asked to describe its own configuration, and
the HTTP health payload must not drift from the sentence the chat returns: if they
disagreed, one of them would be a second source of truth.

I/O lives here, not in `core/status.py`. Each probe is wrapped individually so a down
MLflow server cannot take the narrator line with it — the same reasoning as
`api/main.py:health` (which is now a thin wrapper around this).

Abbreviations: LLM (large language model), DSN (data source name).
"""
from __future__ import annotations

from typing import Any

import psycopg

from ..config import load_config
from . import llm as LLM
from . import tracing as TRACING
from . import tools as T

CFG = load_config()


def _rows(sql: str) -> list[dict]:
    """One query, or raise. Callers catch — a failed probe is absent, not zero."""
    with psycopg.connect(CFG.pg_dsn, row_factory=psycopg.rows.dict_row) as c:
        with c.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall() if cur.description else []


def _llm() -> dict[str, Any]:
    provider = None
    try:
        provider = LLM.provider()
    except Exception:
        pass
    available = False
    model = None
    try:
        available = bool(LLM.available())
        if available:
            model = LLM.pick_model()
    except Exception:
        pass
    return {"available": available, "model": model, "provider": provider}


def _tracing() -> dict[str, Any]:
    try:
        return {"enabled": bool(TRACING.enabled())}
    except Exception:
        return {"enabled": False}


def _postgres() -> dict[str, Any]:
    """Counts only — never the host. `/api/health` adds the DSN host, and share-mode
    redaction strips it; the chat status answer must never learn it in the first place.
    """
    monthly_rows = 0
    try:
        monthly_rows = _rows(
            "SELECT count(*) AS n FROM vrr_curated.pattern_vrr WHERE grain='monthly'"
        )[0]["n"]
    except Exception:
        pass
    n_patterns = None
    try:
        n_patterns = len(T.list_patterns())
    except Exception:
        n_patterns = None
    return {"monthly_rows": monthly_rows, "n_patterns": n_patterns}


def _knowledge() -> dict[str, Any]:
    knowledge = {"docs": 0, "chunks": 0, "pending_review": 0}
    try:
        row = _rows("SELECT count(DISTINCT doc_id) AS docs, count(*) AS chunks "
                    "FROM vrr_agent.reservoir_knowledge")[0]
        knowledge["docs"] = row["docs"]
        knowledge["chunks"] = row["chunks"]
    except Exception:
        pass
    try:
        knowledge["pending_review"] = _rows(
            "SELECT count(*) AS n FROM vrr_agent.knowledge_registry "
            "WHERE status = 'pending_review'")[0]["n"]
    except Exception:
        pass
    return knowledge


def probe() -> dict[str, Any]:
    """Everything `/api/health` reports about connectivity, minus auth and hosts.

    Never raises. A missing figure is `None` or a documented zero (knowledge / monthly
    rows keep the zeros the sidebar already showed when Postgres was down).
    """
    return {
        "llm": _llm(),
        "tracing": _tracing(),
        "postgres": _postgres(),
        "knowledge": _knowledge(),
        "retrieval_min_score": CFG.retrieval_min_score,
    }
