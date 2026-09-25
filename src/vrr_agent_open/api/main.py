"""FastAPI application — the workbench backend, and the agent as a callable service.

    React (web/)  ──HTTP──▶  FastAPI  ──▶  agent/tools.py  ──▶  core/ + PostgreSQL
                                     └──▶  agent/chat.py   ──▶  the gated answer

Two properties this layer exists to preserve:

1. **The browser and the LLM go through the same tools.** A figure rendered in a chart
   and a figure quoted in an answer come from one code path, so they cannot disagree.
   No endpoint here computes anything; if a number needs deriving, it is derived in
   `core/` behind a tool.
2. **Guardrails are server-side.** Role checks on the approval chain live in
   `routes_approvals.py`, not in React — hiding a button is UX, refusing the POST is
   the control.

Run: `make api`  (docs at http://localhost:8000/docs)
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from ..agent import llm as LLM
from ..agent import runtime as RT
from ..config import load_config
from . import auth as AUTH
from . import (routes_approvals, routes_architecture, routes_auth, routes_chat,
               routes_knowledge, routes_patterns)
from . import share as SHARE

CFG = load_config()

# The Vite dev server runs on 5173 and calls the API on 8000, so CORS is needed in dev.
# In production `web/dist` is served by this same app, same origin, and CORS is moot.
DEV_ORIGINS = os.environ.get(
    "VRR_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
# A tunnel serves the built SPA from its own origin, so the browser calls /api on that
# same host and CORS never fires. The entry is here for the case where someone points a
# local Vite dev server at a shared backend.
if SHARE.SHARE_MODE and os.environ.get("VRR_PUBLIC_ORIGIN"):
    DEV_ORIGINS.append(os.environ["VRR_PUBLIC_ORIGIN"].rstrip("/"))

app = FastAPI(
    title="VRR Agent API",
    version="0.1.0",
    description=("Deterministic VRR tools + the gated reasoning agent, over HTTP. "
                 "Every number comes from core/ via agent/tools.py; the LLM only "
                 "chooses tools and phrases results."),
)
app.add_middleware(CORSMiddleware, allow_origins=DEV_ORIGINS, allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

_SHARE_PROBLEMS = SHARE.preflight()
if _SHARE_PROBLEMS:
    # Fail at startup, not when a stranger clicks the link.
    raise RuntimeError("VRR_SHARE=1 refused:\n  - " + "\n  - ".join(_SHARE_PROBLEMS))
if SHARE.SHARE_MODE:
    print(SHARE.banner())

if AUTH.SECRET_IS_EPHEMERAL:
    # Loud, once, at import: without VRR_JWT_SECRET every restart silently signs with a
    # new key, so yesterday's token 401s and it looks like a bug rather than a setting.
    print("⚠️  VRR_JWT_SECRET not set — signing tokens with a random per-process key; "
          "they will not survive a restart. Set one in .env for stable sessions.")

# On Databricks there is no operator at a shell to run `make seed` and friends, so the
# first process to start against an empty Lakebase does it — in a thread, so the port is
# bound immediately and the platform's health check passes while the seed loads.
# `VRR_BOOTSTRAP=1` opts in; a laptop keeps the make targets.
if os.environ.get("VRR_BOOTSTRAP") == "1":
    from ..pipeline import bootstrap as BOOT

    @app.on_event("startup")
    def _bootstrap() -> None:
        BOOT.start_in_background()


app.include_router(routes_auth.router)
app.include_router(routes_patterns.router, dependencies=SHARE.READ_GUARD)
# A read, so it inherits the same guard: in share mode the system's own map is behind the
# token like every other read. It carries counts only — no hosts, no connection strings.
app.include_router(routes_architecture.router, dependencies=SHARE.READ_GUARD)
app.include_router(routes_approvals.router)
app.include_router(routes_chat.router)
# No SHARE.READ_GUARD: every route in this one already requires a token of its own, and
# the upload/review routes require a data_steward or admin role on top of that.
app.include_router(routes_knowledge.router)


@app.get("/api/health", tags=["system"])
def health() -> dict:
    """What the sidebar shows: is a model up, is tracing on, is anything ingested.

    Never raises — a workbench that will not load because MLflow is down would be a
    worse failure than the one it is reporting. Connectivity facts come from
    `agent.runtime.probe` so `/api/health` and the chat `status` intent cannot disagree.
    """
    try:
        snap = RT.probe()
    except Exception:
        snap = {}

    pg = dict(snap.get("postgres") or {})
    pg.setdefault("monthly_rows", 0)
    pg["host"] = CFG.pg_dsn.split("@")[-1]
    tr = dict(snap.get("tracing") or {})
    tr.setdefault("enabled", False)
    tr["uri"] = CFG.mlflow_uri
    llm_info = dict(snap.get("llm") or {})
    llm_info.setdefault("available", False)
    llm_info.setdefault("model", None)
    try:
        llm_info.setdefault("provider", LLM.provider())
    except Exception:
        llm_info.setdefault("provider", None)

    # Redacted when the app is publicly reachable: the Postgres host and the MLflow URI
    # are sidebar detail on a laptop and reconnaissance from a stranger's browser.
    bootstrap = None
    if os.environ.get("VRR_BOOTSTRAP") == "1":
        from ..pipeline import bootstrap as BOOT
        bootstrap = {"state": BOOT.STATUS["state"], "error": BOOT.STATUS["error"],
                     "steps": BOOT.STATUS["steps"][-8:]}

    return SHARE.redact_health({
        "bootstrap": bootstrap,
        "auth": {"required_for": ["writes", "chat"], "scheme": "OAuth2 password → JWT bearer",
                 "token_ttl_minutes": AUTH.TOKEN_TTL_MINUTES,
                 "ephemeral_secret": AUTH.SECRET_IS_EPHEMERAL},
        "llm": llm_info,
        "tracing": tr,
        "postgres": pg,
        "knowledge": snap.get("knowledge") or {"docs": 0, "chunks": 0, "pending_review": 0},
        "retrieval_min_score": snap.get("retrieval_min_score", CFG.retrieval_min_score),
    })


# Serve the built React app when it exists, so one process runs the whole workbench.
# Mounted LAST so it never shadows /api/*. Absent in dev — Vite serves the UI then.
_DIST = Path(__file__).resolve().parents[3] / "web" / "dist"
if _DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="web")
