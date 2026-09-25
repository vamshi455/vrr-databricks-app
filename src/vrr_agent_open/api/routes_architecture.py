"""`GET /api/architecture` — the system's own diagram, with live numbers on it.

This is the one endpoint that describes the application rather than the reservoir, and
it is held to the same standard as every other: **no figure is hardcoded**. Each fact is
a query, a registry length, or a runtime probe taken when the request arrives. Anything
that cannot be measured is simply absent from the payload, and `core.architecture`
renders that box without a number rather than with a plausible one.

Why the counts are gathered here and placed in `core/`: the placement is testable off-DB
and the SQL is not, so they live on opposite sides of the line the rest of the repo
already draws. `core/` stays pure; this module does the I/O.

Every probe is individually wrapped. A workbench that will not draw its own architecture
because MLflow is down would be a worse failure than the one it is reporting — the same
reasoning as `/api/health`.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..agent import llm as LLM
from ..agent import tools as TOOLS
from ..agent import tracing as TRACING
from ..core import approval as APPROVAL
from ..core import architecture as ARCH
from ..config import load_config
from .db import query

router = APIRouter(prefix="/api", tags=["system"])

CFG = load_config()


def _scalar(sql: str, params: dict | None = None) -> Any:
    """One number, or None if the table is missing, empty of the column, or unreachable.

    None is meaningful downstream — it is what makes a box render blank instead of
    claiming zero. "Zero cards in this lane" and "I could not read the queue" are
    different statements and must not collapse into the same one.
    """
    try:
        rows = query(sql, params)
    except Exception:
        return None
    if not rows:
        return None
    return next(iter(rows[0].values()), None)


# Derived from the chain itself rather than retyped, so re-wiring the approval stages
# cannot leave the diagram probing for a lane that no longer exists.
STAGE_KEYS: tuple[str, ...] = tuple(f"queue_{stage}" for stage in APPROVAL.STAGES)


def _stage_counts() -> dict[str, Any]:
    """Cards per approval lane. Absent lanes report 0 — the query succeeded, they are
    genuinely empty — while an unreadable queue reports nothing at all."""
    try:
        rows = query("SELECT stage, count(*) AS n FROM vrr_agent.action_queue GROUP BY stage")
    except Exception:
        return {}
    seen = {r["stage"]: r["n"] for r in rows}
    return {key: seen.get(key.removeprefix("queue_"), 0) for key in STAGE_KEYS}


def _scorer_counts() -> dict[str, Any]:
    """How many scorers exist, read from the code that defines them.

    Imported lazily because the evaluation package pulls MLflow, which is a heavy import
    and an optional dependency of *running* the workbench. `_SPECS` is package-private
    but this is the same package; the alternative is writing the number 3 into a diagram
    and letting it rot.
    """
    out: dict[str, Any] = {}
    try:
        from ..evaluation.custom_scorers import DETERMINISTIC_SCORERS

        out["scorers_deterministic"] = len(DETERMINISTIC_SCORERS)
    except Exception:
        pass
    try:
        from ..evaluation.custom_judges import _SPECS

        # Stated as unmeasured on purpose. These judges contradict themselves on the same
        # eval case, and a diagram that shows the count without the caveat invites a
        # reader to quote them.
        out["judges_state"] = f"{len(_SPECS)} judge(s) · UNMEASURED"
    except Exception:
        pass
    return out


def collect_facts() -> dict[str, Any]:
    """Everything the diagram can display, measured now."""
    facts: dict[str, Any] = {}

    facts["raw_rows"] = _scalar("SELECT count(*) FROM vrr_raw.production_volumes_daily")
    facts["monthly_rows"] = _scalar(
        "SELECT count(*) FROM vrr_curated.pattern_vrr WHERE grain = 'monthly'")
    facts["patterns"] = _scalar("SELECT count(*) FROM vrr_raw.pattern")
    facts["safety_limits"] = _scalar("SELECT count(*) FROM vrr_agent.safety_limits")

    facts["chat_turns"] = _scalar("SELECT count(*) FROM vrr_agent.chat_history")
    # The gate's verdict is prose, not an enum, so this matches on the two outcomes that
    # mean it intervened: a repair attempt, and an outright refusal of the model's draft.
    #
    # `strpos` rather than the obvious ILIKE: `db.query` passes `params or {}`, so psycopg
    # always receives a mapping and therefore always parses the SQL for placeholders —
    # which turns the `%` in a LIKE pattern into a broken placeholder and raises. The
    # first version of this line did use ILIKE, and the box rendered blank rather than
    # 500ing, because `_scalar` swallows the error. That is the behaviour we want from an
    # unreadable probe, but it also means a bug here is silent: worth knowing.
    facts["gate_repaired"] = _scalar(
        "SELECT count(*) FROM vrr_agent.chat_history "
        "WHERE strpos(lower(gate), 'repair') > 0 OR strpos(gate, 'REJECTED') > 0")
    facts["llm_turns"] = _scalar(
        "SELECT count(*) FROM vrr_agent.chat_history WHERE llm_used")

    facts["chunks"] = _scalar("SELECT count(*) FROM vrr_agent.reservoir_knowledge")
    facts["docs_reservoir"] = _scalar(
        "SELECT count(DISTINCT doc_id) FROM vrr_agent.reservoir_knowledge "
        "WHERE doc_kind = 'reservoir'")
    facts["docs_help"] = _scalar(
        "SELECT count(DISTINCT doc_id) FROM vrr_agent.reservoir_knowledge "
        "WHERE doc_kind = 'app_help'")
    facts["pending_review"] = _scalar(
        "SELECT count(*) FROM vrr_agent.knowledge_registry WHERE status = 'pending_review'")
    facts["approved_docs"] = _scalar(
        "SELECT count(*) FROM vrr_agent.knowledge_registry WHERE status = 'approved'")
    facts["retrieval_floor"] = CFG.retrieval_min_score

    facts["users"] = _scalar("SELECT count(*) FROM vrr_agent.app_user WHERE active")
    facts.update(_stage_counts())
    facts.update(_scorer_counts())

    # Registry lengths rather than literals: rename or remove a tool and the box follows.
    facts["tools"] = len(TOOLS.TOOL_SPECS)
    try:
        from ..agent.chat import INTENTS, PRE_TABLE_INTENTS

        facts["intents"] = len(INTENTS) + len(PRE_TABLE_INTENTS)
    except Exception:
        pass

    try:
        facts["tracing"] = "traced" if TRACING.enabled() else "not traced"
    except Exception:
        pass

    try:
        available = LLM.available()
        provider = LLM.provider()
        facts["llm"] = (f"{provider} · {LLM.pick_model()}" if available
                        else f"{provider} · unavailable")
    except Exception:
        pass

    return facts


@router.get("/architecture")
def architecture() -> dict:
    """The bands, boxes, edges and the live figure on each box.

    No host names or connection strings are returned, so this stays safe to serve in
    share mode without a redaction step of its own.
    """
    return ARCH.build(collect_facts())
