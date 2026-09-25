"""MLflow tracing, made optional and quiet.

The agent must run on a box with no MLflow installed and no tracking server up — so
tracing is probed once at import and degrades to a no-op decorator. When a server IS
reachable, every span lands in one experiment and the whole question becomes an
inspectable tree:

    chat.respond                    (AGENT)
      └ analyst.analyze             (CHAIN)      or  graph.run (agentic)
          ├ tool: VRR_AUDIT         (TOOL)           ├ llm.chat        (LLM)
          ├ tool: VRR_DECOMPOSE     (TOOL)           ├ tool: …         (TOOL)
          └ tool: RECOMMEND_CHANGE  (TOOL)           └ gate            (CHAIN)

Enable/point it with env vars — nothing here has side effects beyond the probe:
    MLFLOW_TRACKING_URI=http://localhost:5001   (5000 is taken by AirPlay on macOS)
    VRR_TRACING=0                               to force it off
"""
from __future__ import annotations

import os
from functools import wraps

from ..config import load_config

CFG = load_config()
EXPERIMENT = os.environ.get("VRR_MLFLOW_EXPERIMENT", "vrr-agent-open")

_mlflow = None
_enabled = False


def _probe() -> bool:
    """Is a tracking server actually listening? Keeps a dead URI from costing latency."""
    if os.environ.get("VRR_TRACING", "1") == "0":
        return False
    if CFG.mlflow_uri == "databricks":
        # Managed MLflow: there is no /health to poll; the experiment call below is the
        # probe, and a permission error there leaves tracing OFF rather than crashing.
        return True
    try:
        import httpx

        return httpx.get(f"{CFG.mlflow_uri}/health", timeout=0.7).status_code == 200
    except Exception:
        return False


try:                                                    # pragma: no cover - env dependent
    if _probe():
        import mlflow as _mlflow                        # noqa: F811

        _mlflow.set_tracking_uri(CFG.mlflow_uri)
        _mlflow.set_experiment(EXPERIMENT)
        _enabled = True
except Exception:
    _mlflow, _enabled = None, False


def enabled() -> bool:
    return _enabled


def last_trace_id() -> str | None:
    """The id of the trace just finished, so the API can hand the UI a deep link.

    An answer you cannot find in MLflow is an answer you cannot audit, so the id travels
    with the response rather than being something you go hunting for by timestamp.
    """
    if not _enabled:
        return None
    try:
        return _mlflow.get_last_active_trace_id()
    except Exception:
        return None


def trace_url(trace_id: str | None) -> str | None:
    if not (_enabled and trace_id):
        return None
    if CFG.mlflow_uri == "databricks":
        host = os.environ.get("DATABRICKS_HOST", "").rstrip("/")
        host = host if host.startswith("http") else f"https://{host}"
        return f"{host}/ml/experiments/{_experiment_id()}/traces?selectedTraceId={trace_id}"
    return f"{CFG.mlflow_uri}/#/experiments/{_experiment_id()}/traces?selectedTraceId={trace_id}"


def _experiment_id() -> str:
    try:
        exp = _mlflow.get_experiment_by_name(EXPERIMENT)
        return exp.experiment_id if exp else "0"
    except Exception:
        return "0"


def recheck() -> bool:
    """Re-probe MLflow. Tracing is meant to be on ALWAYS; when the server was down at
    import we must be able to notice it came back without restarting the API."""
    global _enabled
    _probe()
    return _enabled


def status() -> str:
    return (f"🟢 tracing → {CFG.mlflow_uri} ({EXPERIMENT})" if _enabled
            else "⚪ tracing off (no MLflow server at " + CFG.mlflow_uri + ")")


def retriever_span(name: str | None = None):
    """Decorator for a retrieval function: records a RETRIEVER span with its documents.

    MLflow's retrieval scorers read `mlflow.entities.Document` objects off spans typed
    RETRIEVER. Our search returns dicts, so we translate here rather than contorting the
    tool's own return shape — the tool stays convenient for the app, the trace stays
    scoreable.
    """
    def deco(fn):
        if not _enabled:
            return fn

        @wraps(fn)
        def wrapper(*a, **kw):
            from mlflow.entities import Document

            with _mlflow.start_span(name=name or fn.__qualname__,
                                    span_type="RETRIEVER") as span:
                span.set_inputs({"query": str(a[0]) if a else kw.get("query", "")})
                out = fn(*a, **kw)
                hits = (out or {}).get("hits") or []
                span.set_outputs([
                    Document(page_content=h.get("text", ""),
                             metadata={k: v for k, v in h.items() if k != "text"},
                             id=f"{h.get('file_name')}#p{h.get('page')}")
                    for h in hits])
                return out
        return wrapper
    return deco


def trace(name: str | None = None, span_type: str = "CHAIN"):
    """Decorator: record the call as a span when tracing is live, else pass through."""
    def deco(fn):
        if not _enabled:
            return fn

        @wraps(fn)
        def wrapper(*a, **kw):
            with _mlflow.start_span(name=name or fn.__qualname__,
                                    span_type=span_type) as span:
                try:
                    span.set_inputs({"args": [str(x)[:500] for x in a],
                                     "kwargs": {k: str(v)[:500] for k, v in kw.items()}})
                except Exception:
                    pass
                out = fn(*a, **kw)
                try:
                    # Record the payload STRUCTURED and untruncated: a tool span is the
                    # evidence a grounding check reads, and an answer's figures live in the
                    # tail of large payloads (the old str()[:2000] silently cut them off,
                    # which made every trace-based grounding score meaningless).
                    span.set_outputs(out if isinstance(out, (dict, list, str, int, float,
                                                             bool, type(None)))
                                     else str(out))
                except Exception:
                    try:
                        span.set_outputs({"repr": str(out)[:20000]})
                    except Exception:
                        pass
                return out
        return wrapper
    return deco
