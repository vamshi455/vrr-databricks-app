"""First-run bootstrap — everything `docs/running.md §A` does by hand, made idempotent.

On a laptop the operator runs `psql -f schema.sql`, `make seed`, `make queue`,
`make users`, `make knowledge` (twice, with a human approval in between) and
`make guide`. A Databricks App has no operator at a shell and no persistent local disk,
so the API runs this once at startup, in a background thread, and skips each step whose
result is already in the database. Every step is the SAME function the make target
calls; nothing here computes anything of its own.

The human approval gate on uploaded knowledge is untouched: the four seeded demo PDFs are
approved here because they are first-party fixtures generated from this repository
(`scripts/make_sample_pdfs.py`), exactly as `make guide` already reasons for the user
guide. Anything uploaded through the browser still waits for a data steward.

    python -m vrr_agent_open.pipeline.bootstrap        # run it by hand
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import threading
import time
from typing import Any

import psycopg

from ..config import load_config

CFG = load_config()
ROOT = pathlib.Path(__file__).resolve().parents[3]
SCHEMA = pathlib.Path(__file__).with_name("schema.sql")

STATUS: dict[str, Any] = {"state": "idle", "steps": [], "error": None,
                          "started": None, "finished": None}
_LOCK = threading.Lock()


def _log(msg: str) -> None:
    print(f"[bootstrap] {msg}", flush=True)
    STATUS["steps"].append(msg)


def _scalar(sql: str) -> Any:
    with psycopg.connect(CFG.pg_dsn) as c:
        return c.execute(sql).fetchone()[0]


def _exists(rel: str) -> bool:
    return bool(_scalar(f"SELECT to_regclass('{rel}') IS NOT NULL"))


def _count(rel: str) -> int:
    return int(_scalar(f"SELECT count(*) FROM {rel}")) if _exists(rel) else 0


# ------------------------------------------------------------------ steps ----
def apply_schema() -> None:
    """`schema.sql`, with the vector width substituted from config (see `embed_dim`)."""
    sql = SCHEMA.read_text().replace("vector(768)", f"vector({CFG.embed_dim})")
    with psycopg.connect(CFG.pg_dsn) as c:
        c.execute(sql)
        c.commit()
    _log(f"schema applied (embedding vector({CFG.embed_dim}))")


def seed_if_empty() -> None:
    if _count("vrr_curated.pattern_vrr") > 0:
        _log("seed: curated rows present — skipped")
        return
    from . import seed
    t0 = time.time()
    seed.main()
    _log(f"seed: {_count('vrr_curated.pattern_vrr')} pattern_vrr rows in {time.time()-t0:.0f}s")


def queue_if_empty() -> None:
    if _count("vrr_agent.action_queue") > 0:
        _log("queue: drafts present — skipped")
        return
    from . import anomaly_to_queue
    n = len(anomaly_to_queue.run())
    _log(f"queue: {n} drafts queued")


def users() -> None:
    """Demo accounts, password from `VRR_DEMO_PASSWORD` (a secret on Databricks)."""
    from ..api import auth as A
    pw = os.environ.get("VRR_DEMO_PASSWORD", "vrr-demo")
    A.ensure_table()
    for username, role, full_name in (
        ("analyst.demo", "analyst", "Ana Lyst — reviews drafts, first sign-off"),
        ("rm.demo", "rm", "Reservoir Manager — second sign-off"),
        ("site.demo", "site", "Site Engineer — the ONLY role that may execute"),
        ("steward.demo", "data_steward", "Data Steward — owns DATA_ARTIFACT items"),
    ):
        A.upsert_user(username, pw, role, full_name)
    _log("users: 4 demo accounts upserted")


def knowledge_if_empty() -> None:
    """Seeded demo PDFs → registry → (first-party approval) → chunk/redact/embed."""
    from . import knowledge_ingest as KI
    if _scalar("SELECT count(*) FROM vrr_agent.reservoir_knowledge "
               "WHERE coalesce(doc_kind,'reservoir')='reservoir'") > 0:
        _log("knowledge: reservoir corpus present — skipped")
        return
    pathlib.Path(KI.UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, str(ROOT / "scripts" / "make_sample_pdfs.py")],
                   check=True, cwd=str(ROOT),
                   env={**os.environ, "VRR_KNOWLEDGE_DIR": KI.UPLOAD_DIR})
    n = KI.register_new()
    with psycopg.connect(CFG.pg_dsn) as c:
        c.execute("UPDATE vrr_agent.knowledge_registry SET status='approved', "
                  "reviewed_by='bootstrap (first-party fixture)', reviewed_at=now() "
                  "WHERE status='pending_review' AND source IS DISTINCT FROM 'upload'")
        c.commit()
    done = KI.ingest_approved()
    _log(f"knowledge: {n} registered, {done} ingested, "
         f"{_count('vrr_agent.reservoir_knowledge')} chunks")


def guide_if_empty() -> None:
    if _scalar("SELECT count(*) FROM vrr_agent.reservoir_knowledge "
               "WHERE doc_kind='app_help'") > 0:
        _log("guide: app_help corpus present — skipped")
        return
    sys.path.insert(0, str(ROOT / "scripts"))
    import build_app_guide as G           # noqa: E402  (scripts/ is not a package)
    G.GUIDE_DIR = ROOT / "docs" / "app-guide"
    res = G.ingest(G.generate())
    _log(f"guide: {len(res['files'])} files, {res['chunks']} chunks")


def input_audit() -> None:
    from . import input_audit as IA
    if _count("vrr_agent.input_audit") > 0:
        _log("input_audit: present — skipped")
        return
    try:
        rows = IA.run(latest_only=True)
        _log(f"input_audit: {len(rows)} periods audited")
    except Exception as exc:                # optional: the tools recompute verdicts live
        _log(f"input_audit: skipped ({exc})")


STEPS = [apply_schema, seed_if_empty, input_audit, queue_if_empty, users,
         knowledge_if_empty, guide_if_empty]


def run() -> dict:
    with _LOCK:
        if STATUS["state"] == "running":
            return STATUS
        STATUS.update(state="running", started=time.time(), error=None, steps=[])
    try:
        for step in STEPS:
            try:
                step()
            except Exception as exc:
                # Knowledge/guide need the embedding endpoint; the workbench does not.
                # Record the failure and keep going so a missing endpoint permission
                # never hides the portfolio.
                _log(f"{step.__name__}: FAILED — {exc}")
                if step in (apply_schema, seed_if_empty):
                    raise
        STATUS["state"] = "done"
    except Exception as exc:
        STATUS.update(state="failed", error=str(exc))
    finally:
        STATUS["finished"] = time.time()
    return STATUS


def start_in_background() -> threading.Thread:
    t = threading.Thread(target=run, name="vrr-bootstrap", daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    out = run()
    print(out["state"], "| error:", out["error"])
    for s in out["steps"]:
        print("  -", s)
    sys.exit(0 if out["state"] == "done" else 1)
