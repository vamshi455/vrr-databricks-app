"""Read endpoints — portfolio, trend, attribution, audit, lineage, analysis.

Every one of these is a thin wrapper over `agent/tools.py`. That is deliberate: the
browser and the LLM go through the SAME deterministic tools, so a figure on screen and
a figure in an answer cannot disagree. If a number needs computing, it is computed in
`core/`, called through a tool, and returned here — never assembled in this layer and
never assembled in React.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..agent import analyst as AZ
from ..agent import tools as T
from .auth import CurrentUser
from .schemas import SubmitRequest

router = APIRouter(prefix="/api", tags=["patterns"])


@router.get("/patterns")
def list_patterns() -> list[dict]:
    """Every pattern with its latest VRR — what the sidebar picker is built from."""
    return T.list_patterns()


@router.get("/overview")
def overview(asset: str | None = Query(None)) -> dict:
    """Portfolio triage: every pattern vs target, ranked by absolute drift."""
    return T.vrr_overview(asset)


@router.get("/data-quality")
def data_quality(pattern: str | None = Query(None)) -> dict:
    """Ingestion-quality checks over the raw layer (allocation, pressure, PVT)."""
    return T.data_quality(pattern)


@router.get("/input-audit")
def input_audit(pattern: str | None = Query(None)) -> dict:
    """Recorded DATA_ARTIFACT / REAL_SIGNAL verdicts — the guardrail the UI displays."""
    return T.input_audit(pattern)


@router.get("/patterns/{pattern_id}/context")
def context(pattern_id: str) -> dict:
    """Target, learned band, asset, completion counts."""
    out = T.pattern_context(pattern_id)
    if not out or out.get("error"):
        raise HTTPException(404, f"unknown pattern {pattern_id}")
    return out


@router.get("/patterns/{pattern_id}/trend")
def trend(pattern_id: str) -> dict:
    """Full monthly VRR history — the chart series."""
    return T.vrr_trend(pattern_id)


@router.get("/patterns/{pattern_id}/decompose")
def decompose(pattern_id: str, date_from: str = Query(alias="from"),
              date_to: str = Query(alias="to")) -> dict:
    """Exact LMDI attribution of the move between two periods."""
    return T.vrr_decompose(pattern_id, date_from, date_to)


@router.get("/patterns/{pattern_id}/audit")
def audit(pattern_id: str, date: str) -> dict:
    """Recompute the period from raw rows and diff it against the stored value.

    The point of the endpoint is that it does NOT read the curated number back — it
    rebuilds it through `core.physics` in this request, which is what makes the Lineage
    view evidence rather than a restatement.
    """
    return T.vrr_audit(pattern_id, date)


@router.get("/patterns/{pattern_id}/lineage")
def lineage(pattern_id: str, date: str) -> dict:
    """Per-completion derivation: root inputs → pressure → PVT method → derived terms."""
    return T.vrr_lineage(pattern_id, date)


@router.get("/patterns/{pattern_id}/completions")
def completions(pattern_id: str, date: str | None = None) -> dict:
    return T.list_completions(pattern_id, date)


@router.get("/patterns/{pattern_id}/layout")
def layout(pattern_id: str, date: str | None = None) -> dict:
    """The pattern schematic: well roles, contribution factors, and the shape they make.

    Positions come from `core.pattern_layout` rather than from React, for the same reason
    every other number does — one placement rule, unit-tested off-DB, so a figure in the
    README and a figure in the browser cannot disagree. There are no coordinates in this
    database, so the payload says so (`is_schematic`) and the view must label it.
    """
    return T.pattern_layout(pattern_id, date)


@router.get("/patterns/{pattern_id}/analysis")
def analysis(pattern_id: str, date: str) -> dict:
    """The five-step analyst pipeline: verify → attribute → classify → propose → draft.

    Returns the case file including `draft` when an anomaly fired. A period whose inputs
    audit as DATA_ARTIFACT comes back with an `investigate_inputs` draft instead of a
    valve change — that veto happens in `core.audit`, not here.
    """
    return AZ.analyze(pattern_id, date)


@router.post("/patterns/{pattern_id}/submit")
def submit(pattern_id: str, body: SubmitRequest, user: CurrentUser) -> dict:
    """Put the computed draft into the approval queue at stage `draft`.

    A WRITE, so it needs a token — and `submitted_by` is the authenticated username, not
    a string the caller supplies. The queue row is the first link in an audit chain that
    ends at an executed valve change; a self-declared name there is worth nothing.
    """
    case = AZ.analyze(pattern_id, body.date)
    if not case.get("ok"):
        raise HTTPException(400, case.get("reason", "no analysis for this period"))
    if not case.get("draft"):
        raise HTTPException(400, "no anomaly fired for this period — nothing to draft")
    return T.submit_for_approval(pattern_id, body.date, draft=case["draft"],
                                 submitted_by=user["username"])
