"""Share mode is the difference between a laptop tool and a public URL, so its two
promises are tested rather than assumed: reads close unless explicitly opened, and
`/api/health` stops naming internal hosts.

`share.py` reads its flags at import, which is the same trap documented in CLAUDE.md
rule 2 — so these tests set the module attributes directly rather than the environment,
which is what a running process would actually have resolved.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from vrr_agent_open.api import main as API
from vrr_agent_open.api import share as SHARE


@pytest.fixture
def client():
    return TestClient(API.app)


@pytest.fixture
def shared(monkeypatch):
    """The app as it behaves behind a tunnel with reads closed."""
    monkeypatch.setattr(SHARE, "SHARE_MODE", True)
    monkeypatch.setattr(SHARE, "PUBLIC_READS", False)


# ---- reads --------------------------------------------------------------

def test_reads_are_open_on_a_laptop(client, monkeypatch):
    monkeypatch.setattr(SHARE, "SHARE_MODE", False)
    monkeypatch.setattr(API.routes_patterns.T, "list_patterns", list)
    assert client.get("/api/patterns").status_code == 200


def test_share_mode_closes_reads_by_default(client, shared, monkeypatch):
    """The whole point. Without this an ngrok URL hands over every pattern, trend,
    lineage and audit to anyone who has the link."""
    monkeypatch.setattr(API.routes_patterns.T, "list_patterns", list)
    r = client.get("/api/patterns")
    assert r.status_code == 401
    assert "sign-in" in r.json()["detail"]


def test_a_forged_bearer_token_does_not_open_reads(client, shared, monkeypatch):
    monkeypatch.setattr(API.routes_patterns.T, "list_patterns", list)
    r = client.get("/api/patterns", headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 401


def test_public_reads_must_be_opted_into_explicitly(client, monkeypatch):
    """Turning on sharing must never loosen anything by itself — opening reads is a
    second, separate decision."""
    monkeypatch.setattr(SHARE, "SHARE_MODE", True)
    monkeypatch.setattr(SHARE, "PUBLIC_READS", True)
    monkeypatch.setattr(API.routes_patterns.T, "list_patterns", list)
    assert client.get("/api/patterns").status_code == 200


# ---- health -------------------------------------------------------------

def test_health_names_internal_hosts_on_a_laptop(client, monkeypatch):
    monkeypatch.setattr(SHARE, "SHARE_MODE", False)
    body = client.get("/api/health").json()
    assert "host" in body["postgres"]
    assert "uri" in body["tracing"]


def test_health_redacts_internal_hosts_when_shared(client, shared):
    body = client.get("/api/health").json()
    assert "host" not in body["postgres"]
    assert "uri" not in body["tracing"]
    # The useful parts survive — the sidebar still has something to show.
    assert "monthly_rows" in body["postgres"]
    assert "enabled" in body["tracing"]
    assert body["share_mode"] == {"public_reads": False}


# ---- preflight ----------------------------------------------------------

def test_an_ephemeral_signing_key_is_fatal_in_share_mode(monkeypatch):
    """On a laptop a per-process key is a warning. On a shared URL it means every
    restart silently signs out every visitor, which reads as a broken login."""
    monkeypatch.setattr(SHARE, "SHARE_MODE", True)
    monkeypatch.setattr(SHARE.AUTH, "SECRET_IS_EPHEMERAL", True)
    problems = SHARE.preflight()
    assert len(problems) == 1
    assert "VRR_JWT_SECRET" in problems[0]


def test_preflight_is_silent_when_not_sharing(monkeypatch):
    monkeypatch.setattr(SHARE, "SHARE_MODE", False)
    monkeypatch.setattr(SHARE.AUTH, "SECRET_IS_EPHEMERAL", True)
    assert SHARE.preflight() == []


def test_a_configured_secret_passes_preflight(monkeypatch):
    monkeypatch.setattr(SHARE, "SHARE_MODE", True)
    monkeypatch.setattr(SHARE.AUTH, "SECRET_IS_EPHEMERAL", False)
    assert SHARE.preflight() == []


def test_the_banner_says_which_posture_is_active(monkeypatch):
    monkeypatch.setattr(SHARE, "PUBLIC_READS", True)
    assert "PUBLIC" in SHARE.banner()
    monkeypatch.setattr(SHARE, "PUBLIC_READS", False)
    assert "bearer token" in SHARE.banner()
