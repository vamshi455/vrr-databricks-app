"""The upload endpoint's controls, exercised through HTTP rather than by inspection.

`test_upload_validation.py` proves the pure rules. This file proves they are actually
WIRED — that the role gate, the rate limit, the quota, the dedupe and above all the human
approval gate hold when a real request arrives. The database is stubbed (these run in
`make test` with no stack), so what is under test is the route logic, not psycopg.

The case that matters most is `test_upload_does_not_embed`: the entire design rests on an
uploaded document being inert until a person approves it, and that is exactly the property
a future refactor would quietly break while every other test still passed.
"""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from vrr_agent_open.api import auth as AUTH
from vrr_agent_open.api import main as API
from vrr_agent_open.api import ratelimit as RL
from vrr_agent_open.api import routes_knowledge as RK

PDF = b"%PDF-1.7\n" + b"reservoir procedure text " * 200


@pytest.fixture(autouse=True)
def _clean_limits():
    RL.reset()
    yield
    RL.reset()


@pytest.fixture
def client():
    return TestClient(API.app)


def token(role: str, user: str = "sam") -> dict:
    tok, _ = AUTH.create_access_token(user, role)
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def db(monkeypatch, tmp_path):
    """An in-memory stand-in for the registry, plus a temp quarantine directory."""
    rows: list[dict] = []

    def fake_query(sql: str, params: dict | None = None):
        params = params or {}
        if "count(*) FROM vrr_agent.knowledge_registry" in sql or "AS docs" in sql:
            return [{"docs": len([r for r in rows if r["status"] != "rejected"]),
                     "chunks": sum(r.get("n_chunks") or 0 for r in rows)}]
        if "WHERE sha256" in sql:
            return [r for r in rows if r.get("sha256") == params.get("h")]
        if "WHERE doc_id" in sql:
            return [r for r in rows if r["doc_id"] == params.get("d")]
        return rows

    def fake_execute(sql: str, params: dict):
        if "INSERT INTO vrr_agent.knowledge_registry" in sql:
            rows.append({"doc_id": params["d"], "file_name": params["f"],
                         "status": "pending_review", "source": "upload",
                         "uploaded_by": params["u"], "stored_name": params["s"],
                         "content_kind": params["k"], "size_bytes": params["z"],
                         "sha256": params["h"], "n_chunks": None, "reviewed_by": None,
                         "ingest_error": None, "review_note": None})
        elif "SET status='approved'" in sql:
            for r in rows:
                if r["doc_id"] == params["d"]:
                    r.update(status="approved", reviewed_by=params["u"])
        elif "SET status='rejected'" in sql:
            for r in rows:
                if r["doc_id"] == params["d"]:
                    r.update(status="rejected", reviewed_by=params["u"],
                             review_note=params.get("n"))
        elif "SET status='pending_review'" in sql:
            for r in rows:
                if r["doc_id"] == params["d"]:
                    r.update(status="pending_review", reviewed_by=None,
                             ingest_error=params.get("e"))

    monkeypatch.setattr(RK, "query", fake_query)
    monkeypatch.setattr(RK, "execute", fake_execute)
    monkeypatch.setattr(RK, "UPLOAD_DIR", tmp_path)
    return rows


def upload(client, headers, name="procedure.pdf", data=PDF, ctype="application/pdf"):
    return client.post("/api/knowledge/upload", headers=headers,
                       files={"file": (name, io.BytesIO(data), ctype)})


# --------------------------------------------------------------------- role ----
def test_anonymous_upload_is_refused(client, db):
    assert upload(client, {}).status_code == 401


@pytest.mark.parametrize("role", ["analyst", "rm", "site"])
def test_non_steward_roles_cannot_upload(client, db, role):
    """The role is a signed claim, so this cannot be worked around from the client."""
    r = upload(client, token(role))
    assert r.status_code == 403
    assert "data_steward" in r.json()["detail"]


@pytest.mark.parametrize("role", ["data_steward", "admin"])
def test_steward_and_admin_may_upload(client, db, role):
    assert upload(client, token(role)).status_code == 201


def test_a_forged_token_cannot_upload(client, db):
    r = upload(client, {"Authorization": "Bearer not.a.token"})
    assert r.status_code == 401


# ------------------------------------------------------------------- the gate ----
def test_upload_does_not_embed(client, db, monkeypatch):
    """THE invariant. An uploaded document is inert until a human approves it.

    Asserted by making the ingest path explode: if the upload route ever calls it, this
    test fails loudly rather than the guardrail eroding silently.
    """
    from vrr_agent_open.pipeline import knowledge_ingest as KI

    monkeypatch.setattr(KI, "ingest_document",
                        lambda *a, **k: pytest.fail("upload must not embed"))
    r = upload(client, token("data_steward"))
    assert r.status_code == 201
    assert r.json()["status"] == "pending_review"
    assert db[0]["n_chunks"] is None


def test_approval_embeds_and_records_who(client, db, monkeypatch):
    from vrr_agent_open.pipeline import knowledge_ingest as KI

    monkeypatch.setattr(KI, "ingest_document", lambda doc_id, fname, **k: {
        "doc_id": doc_id, "file_name": fname, "n_chunks": 7, "pages": 2, "pii_kinds": []})
    doc_id = upload(client, token("data_steward")).json()["doc_id"]
    r = client.post(f"/api/knowledge/documents/{doc_id}/approve",
                    headers=token("data_steward", "dana"))
    assert r.status_code == 200
    body = r.json()
    assert body["n_chunks"] == 7 and body["searchable"] is True
    # From the token, never the body — same rule as the approval chain.
    assert body["reviewed_by"] == "dana"


def test_analyst_cannot_approve(client, db, monkeypatch):
    doc_id = upload(client, token("data_steward")).json()["doc_id"]
    r = client.post(f"/api/knowledge/documents/{doc_id}/approve", headers=token("analyst"))
    assert r.status_code == 403


def test_failed_embedding_rolls_the_approval_back(client, db, monkeypatch):
    """A row left 'approved' with no chunks tells the reviewer a document is searchable
    when it is not — and `make knowledge` would then retry it forever."""
    from vrr_agent_open.pipeline import knowledge_ingest as KI

    def boom(*a, **k):
        raise RuntimeError("ollama is down")

    monkeypatch.setattr(KI, "ingest_document", boom)
    doc_id = upload(client, token("data_steward")).json()["doc_id"]
    r = client.post(f"/api/knowledge/documents/{doc_id}/approve", headers=token("admin"))
    assert r.status_code == 502
    assert db[0]["status"] == "pending_review"
    assert "ollama is down" in db[0]["ingest_error"]


def test_rejection_keeps_the_reason(client, db):
    doc_id = upload(client, token("data_steward")).json()["doc_id"]
    r = client.post(f"/api/knowledge/documents/{doc_id}/reject?note=not+VRR+related",
                    headers=token("data_steward", "dana"))
    assert r.status_code == 200
    assert db[0]["status"] == "rejected"
    assert db[0]["review_note"] == "not VRR related"


# ------------------------------------------------------------------ content ----
def test_a_disguised_executable_is_refused_with_reasons(client, db):
    r = upload(client, token("data_steward"), name="payload.pdf",
               data=b"MZ" + b"\x00" * 900)
    assert r.status_code == 422
    assert r.json()["detail"]["rejected"]
    assert not db                                  # nothing registered


def test_disallowed_extension_never_reaches_disk(client, db, tmp_path):
    r = upload(client, token("data_steward"), name="x.exe", data=b"MZ" + b"\x00" * 900)
    assert r.status_code == 422
    assert list(RK.UPLOAD_DIR.iterdir()) == []


def test_identical_content_is_refused_as_a_duplicate(client, db):
    assert upload(client, token("data_steward"), name="a.pdf").status_code == 201
    r = upload(client, token("data_steward"), name="different-name.pdf")
    assert r.status_code == 409
    assert "already registered" in r.json()["detail"]


def test_oversize_upload_is_cut_off(client, db):
    r = upload(client, token("data_steward"), name="huge.pdf",
               data=b"%PDF-1.7\n" + b"x" * (26 * 1024 * 1024))
    assert r.status_code == 413


# -------------------------------------------------------------------- quota ----
def test_document_quota_is_enforced(client, db, monkeypatch):
    monkeypatch.setattr(RK, "MAX_DOCUMENTS", 1)
    assert upload(client, token("data_steward"), name="one.pdf").status_code == 201
    r = upload(client, token("data_steward"), name="two.pdf", data=PDF + b"different")
    assert r.status_code == 409
    assert "full" in r.json()["detail"]


# --------------------------------------------------------------- rate limit ----
def test_upload_is_rate_limited_per_user(client, db, monkeypatch):
    """Auth answers who, not how much."""
    monkeypatch.setattr(RL, "LIMITS", {**RL.LIMITS, "upload": RL.Limit(calls=2, window=60)})
    for i in range(2):
        upload(client, token("data_steward"), name=f"f{i}.pdf", data=PDF + bytes([i]))
    r = upload(client, token("data_steward"), name="third.pdf", data=PDF + b"3")
    assert r.status_code == 429
    assert r.headers["Retry-After"]


def test_the_limit_is_per_user_not_global(client, db, monkeypatch):
    monkeypatch.setattr(RL, "LIMITS", {**RL.LIMITS, "upload": RL.Limit(calls=1, window=60)})
    upload(client, token("data_steward", "alice"), name="a.pdf", data=PDF + b"a")
    # Bob has his own budget; one noisy uploader must not lock out the team.
    r = upload(client, token("data_steward", "bob"), name="b.pdf", data=PDF + b"b")
    assert r.status_code == 201
