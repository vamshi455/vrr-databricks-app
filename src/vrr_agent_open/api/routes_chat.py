"""The analyst chat — one gated answer per request, plus the shared transcript.

Plain JSON, deliberately, not token streaming: `core.faithfulness` can only verify a
FINISHED answer, so streaming tokens would mean streaming text the gate has not yet
approved and might replace. Progress-event streaming (which tool is running) is the
sane future addition; streaming the narration itself is not.

`chat.respond()` stays free of history writes — the transcript is written HERE, so that
`make traces` can run the same function over the evaluation set without pouring ten
synthetic questions into the shared drawer.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..agent import chat as CH
from ..agent import history as HIST
from ..agent import tools as T
from ..agent import tracing as TRACING
from . import ratelimit as RL
from .auth import CurrentUser, OptionalUser
from .db import execute, query
from .schemas import PATTERN_ID_RE, ChatRequest

router = APIRouter(prefix="/api", tags=["chat"])


@router.post("/chat")
def ask(body: ChatRequest, user: CurrentUser) -> dict:
    """Answer one question. `agentic=true` lets the model drive the tool loop itself.

    Authenticated because it spends real compute — an open endpoint here is an open
    invitation to drive someone else's GPU. `asked_by` in the shared transcript comes
    from the token, so the drawer shows who actually asked.

    Rate-limited on top of that, because auth answers *who* and never *how much*: a
    signed-in user could otherwise hold `agentic=true` loops open back to back. The
    agentic path carries its own tighter budget since it runs 1-2 minutes per call.
    """
    RL.hit("chat", user["username"])
    if body.agentic:
        RL.hit("chat_agentic", user["username"])
    try:
        result = CH.respond(body.question, pattern=body.pattern, date=body.date,
                            agentic=body.agentic)
    except Exception as exc:                    # a tool/DB failure is a 502, not a crash
        raise HTTPException(502, f"agent failed: {exc}") from exc

    # Tracing is meant to be ON at all times, so the id travels back with the answer and
    # the UI can link straight to it. `recheck()` picks MLflow back up if it was down
    # when this process started — otherwise a restarted MLflow stays invisible until the
    # API restarts too, which is how a run goes silently untraced.
    if not TRACING.enabled():
        TRACING.recheck()
    trace_id = TRACING.last_trace_id()

    saved = False
    if body.persist and body.pattern:
        try:
            HIST.ensure_table()
            ctx = T.pattern_context(body.pattern) or {}
            HIST.log_turn(pattern_id=body.pattern, pattern_name=ctx.get("pattern_name"),
                          date=body.date, question=body.question, result=result,
                          asked_by=user["username"], agentic=body.agentic)
            saved = True
        except Exception:
            saved = False                       # the answer still stands; only durability is lost
    return {**result, "persisted": saved, "trace_id": trace_id,
            "trace_url": TRACING.trace_url(trace_id),
            "traced": bool(trace_id)}


_CLEAR_DDL = """
CREATE TABLE IF NOT EXISTS vrr_agent.chat_clear (
  username text NOT NULL, id_pattern text NOT NULL,
  cleared_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (username, id_pattern))
"""


def _cleared_before(username: str, pattern: str):
    """This user's personal cutoff for this pattern, or None."""
    try:
        execute(_CLEAR_DDL, {})
        rows = query("SELECT cleared_at FROM vrr_agent.chat_clear"
                     " WHERE username=%(u)s AND id_pattern=%(p)s",
                     {"u": username, "p": pattern})
        return rows[0]["cleared_at"] if rows else None
    except Exception:
        return None


@router.get("/chat/history")
def history(viewer: OptionalUser, pattern: str = Query(..., max_length=64),
            limit: int = Query(50, ge=1, le=200)) -> list[dict]:
    """This pattern's transcript, oldest first — shared, and it survives a refresh.

    Filtered by the caller's personal `cleared_at` cutoff when they have one. The rows
    themselves are never touched: the transcript is an audit record and the traces behind
    it are the evidence, so "clear" means "stop showing me this", not "delete it".

    Stays readable signed-out, but WHOSE cutoff to apply now comes from the bearer token
    rather than a `?user=` query parameter. It used to be the latter, which let any caller
    ask for the view another account had configured — harmless here, since the transcript
    is shared by design and nothing extra was exposed, but a client-asserted identity in
    a file whose every sibling takes identity from the signature.
    """
    if not PATTERN_ID_RE.match(pattern):
        raise HTTPException(400, "pattern must be 1-64 chars of A-Z, a-z, 0-9, _ or -")
    try:
        HIST.ensure_table()
        since = _cleared_before(viewer["username"], pattern) if viewer else None
        return HIST.recent(pattern, limit=limit, since=since)
    except Exception:
        return []                               # no table yet is empty, not an error


@router.post("/chat/clear")
def clear(user: CurrentUser, pattern: str = Query(..., max_length=64)) -> dict:
    """Hide this pattern's transcript FOR THIS USER from now on.

    Records a cutoff; deletes nothing. Everyone else still sees the full history, the
    rows stay in `vrr_agent.chat_history`, and every turn remains in MLflow as a trace.
    """
    execute(_CLEAR_DDL, {})
    execute("INSERT INTO vrr_agent.chat_clear (username, id_pattern) VALUES (%(u)s, %(p)s)"
            " ON CONFLICT (username, id_pattern)"
            " DO UPDATE SET cleared_at = now()",
            {"u": user["username"], "p": pattern})
    return {"cleared_for": user["username"], "pattern": pattern,
            "note": "hidden for you only — rows and traces are retained"}
