"""What `POST /chat` accepts — the input surface of the only endpoint that spends a GPU.

Before this, the whole of it was `question: str = Field(min_length=1, max_length=2000)`.
`pattern` and `date` were free strings that reached the tools unchecked, whose failure
surfaced as a generic 502 from somewhere deep in a query, and there was no budget at all:
authentication established WHO was asking and nothing established HOW MUCH.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from vrr_agent_open.api import auth as AUTH
from vrr_agent_open.api import main as API
from vrr_agent_open.api import ratelimit as RL
from vrr_agent_open.api import routes_chat as RC


@pytest.fixture(autouse=True)
def _clean_limits():
    RL.reset()
    yield
    RL.reset()


@pytest.fixture
def client():
    return TestClient(API.app)


@pytest.fixture
def headers():
    tok, _ = AUTH.create_access_token("sam", "analyst")
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def stub(monkeypatch):
    """A chat that answers instantly, so these tests exercise validation, not the model."""
    seen: list[dict] = []

    def respond(question, pattern=None, date=None, agentic=False):
        seen.append({"question": question, "pattern": pattern, "date": date})
        return {"intent": "explain", "text": "ok", "meta": {}, "data": None}

    monkeypatch.setattr(RC.CH, "respond", respond)
    monkeypatch.setattr(RC.HIST, "ensure_table", lambda: None)
    monkeypatch.setattr(RC.HIST, "log_turn", lambda **k: None)
    monkeypatch.setattr(RC.T, "pattern_context", lambda p: {})
    return seen


def ask(client, headers, **body):
    return client.post("/api/chat", headers=headers, json={"question": "why?", **body})


# --------------------------------------------------------------------- auth ----
def test_chat_still_requires_a_token(client):
    assert client.post("/api/chat", json={"question": "why?"}).status_code == 401


# ---------------------------------------------------------------- question ----
def test_empty_question_is_refused(client, headers, stub):
    assert ask(client, headers, question="").status_code == 422


def test_whitespace_only_question_is_refused(client, headers, stub):
    """`min_length=1` alone accepted "   ", which reached the intent router as nothing."""
    assert ask(client, headers, question="   \n\t ").status_code == 422


def test_overlong_question_is_refused(client, headers, stub):
    assert ask(client, headers, question="x" * 2001).status_code == 422


def test_control_characters_are_stripped_not_stored(client, headers, stub):
    """A NUL or an ANSI escape in a question ends up in a log line and in the shared
    transcript the next reviewer reads."""
    r = ask(client, headers, question="why is VRR\x00 high\x1b[31m?")
    assert r.status_code == 200
    assert "\x00" not in stub[0]["question"] and "\x1b" not in stub[0]["question"]


def test_unicode_is_normalised_so_routing_cannot_be_dodged(client, headers, stub):
    """The intent router keys on WORDS (CLAUDE.md rule 8), so a full-width variant would
    otherwise route to a different code path than the same question typed normally."""
    ask(client, headers, question="ｗｈｙ ｉｓ ＶＲＲ ｈｉｇｈ")
    assert stub[0]["question"] == "why is VRR high"


# ------------------------------------------------------------ pattern/date ----
@pytest.mark.parametrize("bad", ["'; DROP TABLE x--", "a b", "../etc", "x" * 65, "p@t"])
def test_malformed_pattern_is_a_422_naming_the_field(client, headers, stub, bad):
    """These were never an injection risk — the queries are parameterised. The point is
    that the caller is told WHICH input was wrong instead of getting a 502 from a tool."""
    r = ask(client, headers, pattern=bad)
    assert r.status_code == 422
    assert "pattern" in str(r.json()["detail"])


@pytest.mark.parametrize("good", ["UNITY", "pattern-1", "a_b-9", "X"])
def test_well_formed_pattern_ids_pass(client, headers, stub, good):
    assert ask(client, headers, pattern=good).status_code == 200


@pytest.mark.parametrize("bad", ["2026", "01-08-2026", "yesterday", "2026/08/01"])
def test_malformed_date_is_refused(client, headers, stub, bad):
    r = ask(client, headers, date=bad)
    assert r.status_code == 422
    assert "date" in str(r.json()["detail"])


def test_iso_date_passes(client, headers, stub):
    assert ask(client, headers, date="2026-08-01").status_code == 200


def test_empty_context_strings_become_none(client, headers, stub):
    """The UI sends "" before a pattern is selected; that must mean 'no context', not a
    lookup for a pattern named empty-string."""
    ask(client, headers, pattern="", date="")
    assert stub[0]["pattern"] is None and stub[0]["date"] is None


# --------------------------------------------------------------- rate limit ----
def test_chat_is_rate_limited(client, headers, stub, monkeypatch):
    monkeypatch.setattr(RL, "LIMITS", {**RL.LIMITS, "chat": RL.Limit(calls=3, window=60)})
    for _ in range(3):
        assert ask(client, headers).status_code == 200
    r = ask(client, headers)
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) > 0


def test_the_agentic_path_has_its_own_tighter_budget(client, headers, stub, monkeypatch):
    """It runs 1-2 minutes per call, so it must exhaust well before the plain chat one."""
    monkeypatch.setattr(RL, "LIMITS", {**RL.LIMITS,
                                       "chat": RL.Limit(calls=50, window=60),
                                       "chat_agentic": RL.Limit(calls=1, window=60)})
    assert ask(client, headers, agentic=True).status_code == 200
    assert ask(client, headers, agentic=True).status_code == 429
    # ...and the cheap path still works after the expensive one is spent.
    assert ask(client, headers, agentic=False).status_code == 200


def test_budgets_are_per_user(client, stub, monkeypatch):
    monkeypatch.setattr(RL, "LIMITS", {**RL.LIMITS, "chat": RL.Limit(calls=1, window=60)})
    for name in ("alice", "bob"):
        tok, _ = AUTH.create_access_token(name, "analyst")
        r = ask(client, {"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200, f"{name} was charged for someone else's usage"


# ------------------------------------------------------------------ history ----
def test_history_no_longer_takes_a_client_supplied_identity(client, monkeypatch):
    """Whose 'cleared' cutoff to apply is a fact about the caller, so it comes from the
    signature. Passing ?user=someone-else is now simply ignored."""
    seen: list = []
    monkeypatch.setattr(RC.HIST, "ensure_table", lambda: None)
    monkeypatch.setattr(RC.HIST, "recent",
                        lambda p, limit=50, since=None: seen.append(since) or [])
    monkeypatch.setattr(RC, "_cleared_before",
                        lambda u, p: pytest.fail(f"used a client identity: {u}"))
    r = client.get("/api/chat/history?pattern=UNITY&user=someone-else")
    assert r.status_code == 200
    assert seen == [None]


def test_history_rejects_a_malformed_pattern(client):
    assert client.get("/api/chat/history?pattern=" + "x" * 100).status_code == 422
