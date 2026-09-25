"""OAuth2 + JWT — the tests are the security claim, written as things an attacker tries.

Postgres is stubbed, so these run in the off-DB `pytest -q` tier. What they pin is one
sentence: **the role is a signed claim, not something the caller can assert.** Before
this module the approval chain read `role` out of the request body, so a curl with
`{"role": "site"}` executed a valve change; the state machine was enforced against a
role the attacker chose.
"""
from __future__ import annotations

import time

import jwt
import pytest
from fastapi.testclient import TestClient

from vrr_agent_open.api import auth as A
from vrr_agent_open.api import main as API
from vrr_agent_open.api import routes_approvals as RA
from vrr_agent_open.api import routes_auth as RAuth
from vrr_agent_open.api import routes_chat as RC

DRAFT = {"action_id": "ACT-1", "stage": "draft", "id_pattern": "P1",
         "pattern_name": "UNITY", "vrr_date": "2026-04-01", "severity": "high",
         "action_type": "reduce_injection", "recommendation": {"injector_changes": []}}


@pytest.fixture
def client():
    return TestClient(API.app)


@pytest.fixture
def users(monkeypatch):
    """A fake user store: analyst.demo / rm.demo / site.demo, password 'pw'."""
    table = {name: {"username": name, "role": role, "active": True,
                    "password_hash": A.hash_password("pw"), "full_name": None}
             for name, role in [("analyst.demo", "analyst"), ("rm.demo", "rm"),
                                ("site.demo", "site"), ("gone.demo", "analyst")]}
    table["gone.demo"]["active"] = False
    monkeypatch.setattr(A, "get_user", lambda u: table.get(u))
    monkeypatch.setattr(A, "touch_last_login", lambda u: None)
    return table


def login(client, username: str, password: str = "pw") -> str | None:
    r = client.post("/api/auth/token", data={"username": username, "password": password})
    return r.json().get("access_token") if r.status_code == 200 else None


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------- tokens ----
def test_login_returns_a_bearer_token_carrying_the_role(client, users):
    body = client.post("/api/auth/token",
                       data={"username": "site.demo", "password": "pw"}).json()

    assert body["token_type"] == "bearer"
    claims = jwt.decode(body["access_token"], A.SECRET, algorithms=[A.ALGORITHM])
    assert claims["sub"] == "site.demo"
    assert claims["role"] == "site"          # the role is IN the signature
    assert claims["exp"] > time.time()


def test_wrong_password_and_unknown_user_are_indistinguishable(client, users):
    """A login endpoint that tells them apart is an account-enumeration oracle."""
    wrong_pw = client.post("/api/auth/token",
                           data={"username": "site.demo", "password": "nope"})
    no_user = client.post("/api/auth/token",
                          data={"username": "nobody", "password": "nope"})

    assert wrong_pw.status_code == no_user.status_code == 401
    assert wrong_pw.json()["detail"] == no_user.json()["detail"]


def test_deactivated_user_cannot_sign_in(client, users):
    assert client.post("/api/auth/token",
                       data={"username": "gone.demo", "password": "pw"}).status_code == 401


def test_password_is_never_stored_in_the_clear():
    hashed = A.hash_password("hunter2")

    assert "hunter2" not in hashed
    assert A.verify_password("hunter2", hashed) is True
    assert A.verify_password("Hunter2", hashed) is False


def test_the_same_password_hashes_differently_every_time():
    """Per-password salt: identical passwords must not produce identical rows."""
    assert A.hash_password("same") != A.hash_password("same")


# --------------------------------------------------------- rejecting tokens ----
def test_no_token_is_401_with_a_www_authenticate_header(client):
    r = client.post("/api/queue/ACT-1/advance")

    assert r.status_code == 401
    assert r.headers.get("WWW-Authenticate") == "Bearer"


def test_a_token_signed_with_another_key_is_refused(client, users):
    """The attack the signature exists to stop: mint your own claims."""
    forged = jwt.encode({"sub": "attacker", "role": "site", "exp": time.time() + 999},
                        "not-the-server-secret", algorithm="HS256")

    r = client.post("/api/queue/ACT-1/advance", headers=bearer(forged))

    assert r.status_code == 401
    assert "invalid token" in r.json()["detail"]


def test_an_expired_token_is_refused_and_says_so(client, users):
    stale = jwt.encode({"sub": "site.demo", "role": "site", "exp": time.time() - 60},
                       A.SECRET, algorithm=A.ALGORITHM)

    r = client.post("/api/queue/ACT-1/advance", headers=bearer(stale))

    assert r.status_code == 401
    assert "expired" in r.json()["detail"]


def test_a_token_without_a_role_claim_is_refused(client):
    thin = jwt.encode({"sub": "someone", "exp": time.time() + 999},
                      A.SECRET, algorithm=A.ALGORITHM)

    assert client.post("/api/queue/ACT-1/advance",
                       headers=bearer(thin)).status_code == 401


# ------------------------------------------- the role cannot be self-asserted ----
def test_the_body_can_no_longer_choose_a_role(client, users, monkeypatch):
    """THE regression test. The old API took {"role": "site"} from the body and this
    call succeeded. Now the body is ignored entirely and the analyst's token loses."""
    monkeypatch.setattr(RA, "query", lambda sql, p=None: [{**DRAFT, "stage": "site"}])
    wrote = []
    monkeypatch.setattr(RA, "execute", lambda sql, p: wrote.append(sql))
    token = login(client, "analyst.demo")

    r = client.post("/api/queue/ACT-1/advance", headers=bearer(token),
                    json={"role": "site", "user": "analyst.demo"})   # <- the old attack

    assert r.status_code == 403
    assert "signed in as 'analyst.demo' (analyst)" in r.json()["detail"]
    assert wrote == []                       # nothing executed


def test_the_right_role_still_advances_and_is_recorded_as_the_token_subject(
        client, users, monkeypatch):
    monkeypatch.setattr(RA, "query", lambda sql, p=None: [DRAFT])
    monkeypatch.setattr(RA, "execute", lambda sql, p: None)
    token = login(client, "analyst.demo")

    body = client.post("/api/queue/ACT-1/advance", headers=bearer(token)).json()

    assert (body["from"], body["to"]) == ("draft", "analyst")
    assert body["by"] == "analyst.demo"      # from the token, not from a form field


def test_only_the_site_role_can_execute(client, users, monkeypatch):
    monkeypatch.setattr(RA, "query", lambda sql, p=None: [{**DRAFT, "stage": "site"}])
    statements = []
    monkeypatch.setattr(RA, "execute", lambda sql, p: statements.append(sql))
    monkeypatch.setattr(RA.AP, "build_adjustment_row", lambda *a, **k: {})

    refused = client.post("/api/queue/ACT-1/advance",
                          headers=bearer(login(client, "rm.demo")))
    allowed = client.post("/api/queue/ACT-1/advance",
                          headers=bearer(login(client, "site.demo")))

    assert refused.status_code == 403
    assert allowed.json()["to"] == "executed"
    assert "INSERT INTO vrr_agent.adjustment_history" in statements[0]


def test_rejecting_also_requires_the_stages_role(client, users, monkeypatch):
    monkeypatch.setattr(RA, "query", lambda sql, p=None: [DRAFT])
    monkeypatch.setattr(RA, "execute", lambda sql, p: None)

    assert client.post("/api/queue/ACT-1/reject",
                       headers=bearer(login(client, "rm.demo"))).status_code == 403
    assert client.post("/api/queue/ACT-1/reject",
                       headers=bearer(login(client, "analyst.demo"))).status_code == 200


# ------------------------------------------------------------ what is public ----
def test_reads_stay_open_so_the_workbench_loads_without_signing_in(client, monkeypatch):
    monkeypatch.setattr("vrr_agent_open.api.routes_patterns.T.list_patterns", lambda: [])
    monkeypatch.setattr("vrr_agent_open.api.routes_patterns.T.vrr_overview",
                        lambda a: {"n_patterns": 0, "patterns": [], "off_target": []})

    assert client.get("/api/patterns").status_code == 200
    assert client.get("/api/overview").status_code == 200
    assert client.get("/api/health").status_code == 200


def test_chat_requires_a_token_because_it_spends_compute(client, users, monkeypatch):
    monkeypatch.setattr(RC.CH, "respond", lambda *a, **k: {"text": "ok", "meta": {}})

    anon = client.post("/api/chat", json={"question": "why?", "persist": False})
    signed = client.post("/api/chat", headers=bearer(login(client, "analyst.demo")),
                         json={"question": "why?", "persist": False})

    assert anon.status_code == 401
    assert signed.status_code == 200


def test_me_returns_the_claims_the_ui_displays(client, users):
    body = client.get("/api/auth/me",
                      headers=bearer(login(client, "rm.demo"))).json()

    assert body == {"username": "rm.demo", "role": "rm", "expires_at": body["expires_at"]}


def test_the_signing_key_is_never_a_hard_coded_default():
    """A well-known key in a public repo is worse than no auth — it looks like security."""
    assert A.SECRET
    assert len(A.SECRET) >= 32
    assert A.SECRET not in ("secret", "change-me", "vrr", "supersecret")


def test_require_role_rejects_a_role_outside_the_allowed_set(users):
    from fastapi import HTTPException

    guard = A.require_role("admin")
    with pytest.raises(HTTPException) as caught:
        guard({"username": "analyst.demo", "role": "analyst"})

    assert caught.value.status_code == 403
    assert "requires one of admin" in caught.value.detail
