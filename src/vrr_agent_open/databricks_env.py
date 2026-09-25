"""Databricks runtime adapter — the only module that knows it is running on Databricks.

When this app runs as a Databricks App, three things arrive from the platform instead of
from `.env`:

  * **Lakebase (managed Postgres)** — the app's ``database`` resource sets ``PGHOST`` /
    ``PGDATABASE`` / ``PGUSER`` / ``PGPORT`` / ``PGSSLMODE``. There is no password: the
    app's service principal mints a short-lived OAuth credential for the instance, so the
    DSN is *built* here on demand and re-minted before the token expires.
  * **Model Serving** — the narrator and the embedder are serving endpoints behind an
    OpenAI-compatible API at ``https://<workspace>/serving-endpoints``, authenticated with
    the same service principal (``DATABRICKS_CLIENT_ID`` / ``_SECRET`` in the env).
  * **Managed MLflow** — ``MLFLOW_TRACKING_URI=databricks`` and an experiment path.

Off Databricks nothing here is consulted: ``is_databricks()`` is False and every caller
falls back to the local defaults. The trust model does not change with the host — the
LLM still only picks tools and phrases results, `core/` still computes every number.
"""
from __future__ import annotations

import os
import threading
import time
import uuid

_LOCK = threading.Lock()
_PG_TOKEN: dict = {"value": None, "expires": 0.0}
_WS = None

# A Lakebase credential lives ~1 h; re-mint well before that so a connection opened
# right at the edge never fails with an expired password.
_PG_TOKEN_TTL_S = 45 * 60


def is_databricks() -> bool:
    """Are we running inside a Databricks App (or pointed at Lakebase from a laptop)?"""
    return bool(os.environ.get("PGHOST") and os.environ.get("DATABRICKS_HOST"))


def host() -> str:
    h = os.environ.get("DATABRICKS_HOST", "").rstrip("/")
    return h if h.startswith("http") else f"https://{h}" if h else ""


def workspace_client():
    """One SDK client per process. Auth is whatever the environment provides: the app's
    service principal in production, the CLI's cached login on a laptop."""
    global _WS
    if _WS is None:
        from databricks.sdk import WorkspaceClient
        _WS = WorkspaceClient()
    return _WS


def bearer_token() -> str:
    """A workspace access token for Model Serving / MLflow, from the SDK's auth chain."""
    headers = workspace_client().config.authenticate()
    return headers.get("Authorization", "").split(" ", 1)[-1]


def serving_base_url() -> str:
    return f"{host()}/serving-endpoints"


def pg_password() -> str:
    """A fresh (cached) Lakebase credential for the configured instance."""
    now = time.time()
    with _LOCK:
        if _PG_TOKEN["value"] and now < _PG_TOKEN["expires"]:
            return _PG_TOKEN["value"]
        w = workspace_client()
        instance = os.environ.get("VRR_LAKEBASE_INSTANCE")
        if instance:
            tok = w.database.generate_database_credential(
                request_id=str(uuid.uuid4()), instance_names=[instance]).token
        else:                              # the workspace OAuth token also works as the password
            tok = bearer_token()
        _PG_TOKEN.update(value=tok, expires=now + _PG_TOKEN_TTL_S)
        return tok


def pg_dsn() -> str:
    """``postgresql://user:token@host:port/db?sslmode=require`` for Lakebase."""
    user = os.environ.get("PGUSER") or workspace_client().current_user.me().user_name
    host_ = os.environ["PGHOST"]
    port = os.environ.get("PGPORT", "5432")
    db = os.environ.get("PGDATABASE", "databricks_postgres")
    ssl = os.environ.get("PGSSLMODE", "require")
    from urllib.parse import quote
    return f"postgresql://{quote(user, safe='')}:{quote(pg_password(), safe='')}@{host_}:{port}/{db}?sslmode={ssl}"
