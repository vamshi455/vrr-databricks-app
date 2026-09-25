"""FastAPI layer — routing, contracts, and the guardrails that must be server-side.

Postgres and the model are both stubbed, so these run in the off-DB `pytest -q` tier.
What they pin is not "does the endpoint return 200" but the two properties the API
exists to hold:

  1. every read is a pass-through to `agent/tools.py` — this layer computes NOTHING,
     so a figure on screen and a figure in an answer cannot disagree;
  2. protected endpoints are wired to the auth dependency at all.

Identity itself is NOT tested here — `tests/test_auth.py` does that with real signed
tokens (forged, expired, wrong-role). This file overrides the dependency so the routing
and contract tests are not re-testing JWT decoding for the tenth time.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from vrr_agent_open.api import main as API
from vrr_agent_open.api import routes_approvals as RA
from vrr_agent_open.api.auth import current_user
from vrr_agent_open.api import routes_chat as RC
from vrr_agent_open.api import routes_patterns as RP

PATTERN = "E693E0D116730002"
DRAFT = {"action_id": "ACT-1", "stage": "draft", "id_pattern": PATTERN,
         "pattern_name": "UNITY", "vrr_date": "2026-04-01", "severity": "high",
         "action_type": "reduce_injection", "recommendation": {"injector_changes": []},
         "driver": "water_inj_res", "narrative": "…"}


@pytest.fixture
def client():
    """Signed in as the analyst. Overriding the dependency keeps these tests about
    routing and contracts; `test_auth.py` owns the question of who you are."""
    API.app.dependency_overrides[current_user] = lambda: {
        "username": "analyst.demo", "role": "analyst", "claims": {}}
    yield TestClient(API.app)
    API.app.dependency_overrides.clear()


@pytest.fixture
def anon():
    """No token at all — for asserting what stays public."""
    return TestClient(API.app)


# ------------------------------------------------------------------- reads ----
def test_reads_are_pass_throughs_not_computations(client, monkeypatch):
    """The endpoint must hand back the tool payload verbatim — provenance keys and all.

    If this layer ever reshapes a tool result, the number on screen stops being the
    number the tool produced, and the whole grounding argument weakens.
    """
    payload = {"ok": True, "vrr": 1.249, "run_id": "6a0a1f62be0b",
               "provenance": {"recomputed_from": ["vrr_raw.production_volumes_daily"]}}
    monkeypatch.setattr(RP.T, "vrr_audit", lambda p, d: payload)

    got = client.get(f"/api/patterns/{PATTERN}/audit", params={"date": "2026-04-01"})

    assert got.status_code == 200
    assert got.json() == payload            # verbatim, including provenance


def test_layout_is_a_read_and_keeps_its_schematic_disclaimer(client, monkeypatch):
    """The figure is public (reads need no token) and must arrive still labelled.

    `is_schematic` is what the view keys its "not a map" caption off. Dropping it here
    would leave a well diagram on screen with nothing saying the positions are
    allocation rather than location.
    """
    payload = {"found": True, "geometry": "five_spot", "is_schematic": True,
               "nodes": [], "links": [],
               "provenance": {"note": "schematic: wells placed by contribution factor"}}
    seen = {}
    monkeypatch.setattr(RP.T, "pattern_layout",
                        lambda p, d: seen.update(p=p, d=d) or payload)

    got = client.get(f"/api/patterns/{PATTERN}/layout", params={"date": "2026-04-01"})

    assert got.status_code == 200
    assert got.json() == payload
    assert seen == {"p": PATTERN, "d": "2026-04-01"}


def test_unknown_pattern_is_404_not_an_empty_page(client, monkeypatch):
    monkeypatch.setattr(RP.T, "pattern_context", lambda p: {})
    assert client.get("/api/patterns/NOPE/context").status_code == 404


def test_decompose_takes_from_and_to_as_query_aliases(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(RP.T, "vrr_decompose",
                        lambda p, a, b: seen.update(a=a, b=b) or {"ok": True})

    client.get(f"/api/patterns/{PATTERN}/decompose",
               params={"from": "2026-03-01", "to": "2026-04-01"})

    assert seen == {"a": "2026-03-01", "b": "2026-04-01"}


def test_health_never_raises_when_everything_is_down(client, monkeypatch):
    """A workbench that will not load because MLflow is down is a worse failure than
    the one it is reporting."""
    def boom(*a, **k):
        raise RuntimeError("connection refused")

    from vrr_agent_open.agent import runtime as RT

    monkeypatch.setattr(RT.LLM, "available", boom)
    monkeypatch.setattr(RT, "_rows", boom)
    monkeypatch.setattr(RT.T, "list_patterns", boom)

    body = client.get("/api/health").json()

    assert body["llm"]["available"] is False
    # `pending_review` joined the payload so the workbench can badge the review queue;
    # it degrades to 0 with the rest when the database is unreachable.
    assert body["knowledge"] == {"docs": 0, "chunks": 0, "pending_review": 0}


# ------------------------------------------------------- approval guardrails ----






def test_terminal_stage_cannot_be_advanced(client, monkeypatch):
    """409 before any role check — there is no next stage to have a role for."""
    monkeypatch.setattr(RA, "query", lambda sql, p=None: [{**DRAFT, "stage": "executed"}])

    r = client.post("/api/queue/ACT-1/advance")

    assert r.status_code == 409


def test_unknown_action_is_404(client, monkeypatch):
    monkeypatch.setattr(RA, "query", lambda sql, p=None: [])
    assert client.post("/api/queue/NOPE/advance").status_code == 404


def test_submit_refuses_when_no_anomaly_fired(client, monkeypatch):
    """Nothing to approve is a 400, not an empty queue row."""
    monkeypatch.setattr(RP.AZ, "analyze", lambda p, d: {"ok": True, "draft": None})

    r = client.post(f"/api/patterns/{PATTERN}/submit", json={"date": "2026-04-01"})

    assert r.status_code == 400
    assert "nothing to draft" in r.json()["detail"]


# -------------------------------------------------------------------- chat ----
def test_chat_returns_the_gated_answer_with_its_meta(client, monkeypatch):
    answer = {"intent": "explain", "text": "VRR 1.249 …",
              "meta": {"llm": True, "gate": "passed", "model": "qwen2.5:7b"}}
    monkeypatch.setattr(RC.CH, "respond", lambda *a, **k: answer)

    body = client.post("/api/chat", json={"question": "why?", "pattern": PATTERN,
                                          "persist": False}).json()

    assert body["text"] == answer["text"]
    assert body["meta"]["gate"] == "passed"          # provenance reaches the browser
    assert body["persisted"] is False


def test_chat_answer_survives_a_history_write_failure(client, monkeypatch):
    """Durability is a nice-to-have; the answer is not. A dead chat_history table must
    not turn a good answer into a 500."""
    monkeypatch.setattr(RC.CH, "respond", lambda *a, **k: {"text": "ok", "meta": {}})
    monkeypatch.setattr(RC.HIST, "ensure_table", lambda: (_ for _ in ()).throw(
        RuntimeError("no table")))

    body = client.post("/api/chat", json={"question": "why?", "pattern": PATTERN}).json()

    assert body["text"] == "ok"
    assert body["persisted"] is False


def test_agent_failure_is_a_502_not_a_stack_trace(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("postgres is down")

    monkeypatch.setattr(RC.CH, "respond", boom)

    r = client.post("/api/chat", json={"question": "why?", "persist": False})

    assert r.status_code == 502
    assert "postgres is down" in r.json()["detail"]


def test_empty_question_is_rejected_by_the_schema(client):
    assert client.post("/api/chat", json={"question": ""}).status_code == 422


def test_history_is_empty_not_an_error_before_the_table_exists(client, monkeypatch):
    monkeypatch.setattr(RC.HIST, "ensure_table", lambda: (_ for _ in ()).throw(
        RuntimeError("relation does not exist")))

    r = client.get("/api/chat/history", params={"pattern": PATTERN})

    assert r.status_code == 200
    assert r.json() == []


def test_stages_endpoint_publishes_the_state_machine(client):
    """The UI must not hard-code the chain — it reads it."""
    body = client.get("/api/stages").json()

    assert body["stages"][:4] == ["draft", "analyst", "rm", "site"]
    assert body["approver_for_stage"]["draft"] == "analyst"


def test_reads_need_no_token_but_writes_do(anon, monkeypatch):
    """The chosen coverage line: look freely, act with a token."""
    monkeypatch.setattr(RP.T, "list_patterns", lambda: [])

    assert anon.get("/api/patterns").status_code == 200
    assert anon.post("/api/queue/ACT-1/advance").status_code == 401
    assert anon.post("/api/chat", json={"question": "hi"}).status_code == 401


# --------------------------------------------------- the board + clearing chat ----
def test_board_returns_every_lane_including_the_empty_ones(client, monkeypatch):
    """The swim-lane view must render a lane per stage even when nothing sits in it —
    an absent 'rejected' column is not the same as an empty one."""
    monkeypatch.setattr(RA, "query", lambda sql, p=None: [
        {**DRAFT, "stage": "draft"}, {**DRAFT, "action_id": "ACT-2", "stage": "executed"}])

    body = client.get("/api/board").json()

    assert body["order"] == ["draft", "analyst", "rm", "site", "executed", "rejected"]
    assert set(body["lanes"]) == set(body["order"])          # every lane present
    assert body["counts"]["draft"] == 1
    assert body["counts"]["rejected"] == 0
    assert body["approver_for_stage"]["site"] == "site"


def test_clearing_chat_hides_it_without_deleting_anything(client, monkeypatch):
    """'Clear' is a per-user cutoff, not a delete: the transcript is an audit record and
    the traces behind it are the evidence."""
    statements = []
    monkeypatch.setattr(RC, "execute", lambda sql, p: statements.append(sql))

    body = client.post("/api/chat/clear", params={"pattern": PATTERN}).json()

    assert body["cleared_for"] == "analyst.demo"
    joined = " ".join(statements).lower()
    assert "insert into vrr_agent.chat_clear" in joined
    assert "delete" not in joined                            # nothing is removed


def test_history_respects_that_users_personal_cutoff(client, monkeypatch):
    """One user clearing must not blank the shared transcript for everyone else.

    The cutoff is now keyed on the BEARER TOKEN, not a `?user=` query parameter — whose
    view to render is a fact about the caller, so it comes from the signature. The
    endpoint stays readable signed-out, and an anonymous read is simply unfiltered.
    """
    from vrr_agent_open.api.auth import create_access_token

    seen = {}
    monkeypatch.setattr(RC, "execute", lambda sql, p: None)
    monkeypatch.setattr(RC, "query", lambda sql, p=None: [{"cleared_at": "2026-07-30"}])
    monkeypatch.setattr(RC.HIST, "ensure_table", lambda: None)
    monkeypatch.setattr(RC.HIST, "recent",
                        lambda p, limit, since: seen.update(since=since) or [])

    tok, _ = create_access_token("analyst.demo", "analyst")
    client.get("/api/chat/history", params={"pattern": PATTERN},
               headers={"Authorization": f"Bearer {tok}"})
    assert seen["since"] == "2026-07-30"                     # filtered for this user

    client.get("/api/chat/history", params={"pattern": PATTERN})
    assert seen["since"] is None                             # unfiltered when signed out


def test_chat_answer_carries_its_trace_id(client, monkeypatch):
    """Tracing is meant to be on always, so the id travels with the answer and the UI
    can link to the span tree instead of hunting by timestamp."""
    monkeypatch.setattr(RC.CH, "respond", lambda *a, **k: {"text": "ok", "meta": {}})
    monkeypatch.setattr(RC.TRACING, "enabled", lambda: True)
    monkeypatch.setattr(RC.TRACING, "last_trace_id", lambda: "tr-abc123")
    monkeypatch.setattr(RC.TRACING, "trace_url", lambda t: f"http://mlflow/{t}")

    body = client.post("/api/chat", json={"question": "why?", "persist": False}).json()

    assert body["traced"] is True
    assert body["trace_id"] == "tr-abc123"
    assert body["trace_url"].endswith("tr-abc123")


def test_an_untraced_answer_says_so_rather_than_pretending(client, monkeypatch):
    monkeypatch.setattr(RC.CH, "respond", lambda *a, **k: {"text": "ok", "meta": {}})
    monkeypatch.setattr(RC.TRACING, "enabled", lambda: False)
    monkeypatch.setattr(RC.TRACING, "recheck", lambda: False)
    monkeypatch.setattr(RC.TRACING, "last_trace_id", lambda: None)

    body = client.post("/api/chat", json={"question": "why?", "persist": False}).json()

    assert body["traced"] is False                           # surfaced, not swallowed
