"""The status path: routing, and staying true to the live probes.

Same two jobs as `test_help_topics.py`:

1. **Routing.** "Are you connected to an LLM?" must reach `status`, and a reservoir
   question — or an app-help question — must NOT. The costly direction is swallowing
   `lineage` ("trace" is inside "tracing") or `portfolio` ("how many patterns are off
   target").
2. **Truth.** The sentence is assembled from a facts dict. Tests pass a stubbed model
   name and assert it appears, so the path cannot start inventing a Settings screen
   when the probe is silent.
"""
from __future__ import annotations

import pytest

from vrr_agent_open.agent import chat as CH
from vrr_agent_open.core import status as STATUS


# ------------------------------------------------------------------ routing ----
@pytest.mark.parametrize("q", [
    "are you connected to an LLM?",
    "Are you connected to a large language model?",
    "which model?",
    "what model are you using?",
    "how many patterns?",
    "How many patterns are loaded?",
    "are you tracing?",
    "is tracing enabled?",
    "is ollama up?",
    "is the narrator available?",
    "system status",
])
def test_status_questions_route_to_status(q):
    assert STATUS.is_status_question(q), f"{q!r} was not recognised as a status question"
    assert CH.detect_intent(q) == "status"


@pytest.mark.parametrize("q,intent", [
    ("How is VRR calculated?", "lineage"),
    ("Which patterns are furthest from target?", "portfolio"),
    ("how many patterns are off target?", "portfolio"),
    ("How do I use this app?", "help"),
    ("how do I use the chat drawer?", "help"),
    ("Why is UNITY's VRR high in April 2026?", "explain"),
    ("Is the April number actually correct?", "audit"),
    ("What do the documents say about changing injection rates?", "knowledge"),
    ("list the completions in this pattern", "completions"),
])
def test_status_does_not_steal_other_intents(q, intent):
    assert not STATUS.is_status_question(q), f"{q!r} was claimed as status"
    assert CH.detect_intent(q) == intent


def test_tracing_beats_lineage_substring():
    """`lineage` keys on `trace`, which is a substring of `tracing`. Status must win."""
    assert CH.detect_intent("are you tracing?") == "status"
    assert CH.detect_intent("where does this number's lineage come from?") == "lineage"


def test_which_model_beats_help_when_the_question_names_chat():
    """'which model is the chat using?' has an app noun, so help could claim it.
    Status is checked first because the answer is a live probe, not a screen tour."""
    assert CH.detect_intent("which model is the chat using?") == "status"


# -------------------------------------------------------------------- answers ----
def test_format_status_quotes_the_facts_it_was_given():
    """The wording is a function of the dict. A missing model name stays missing —
    it must not be filled in from training data, and it must not invent a Settings
    button (the failure mode `help` exists to close, applied to configuration)."""
    text = STATUS.format_status({
        "llm": {"available": True, "model": "qwen2.5:7b", "provider": "ollama"},
        "tracing": {"enabled": True, "uri": "http://localhost:5001"},
        "postgres": {"n_patterns": 12, "monthly_rows": 1440, "host": "localhost:5432"},
    })
    assert "qwen2.5:7b" in text
    assert "ollama" in text
    assert "available" in text
    assert "**12**" in text
    assert "Tracing:** on" in text
    assert "large language model" in text
    assert "/api/health" in text
    assert "Export" not in text
    assert "Settings" not in text
    assert "click" not in text.lower()
    # Share-mode reconnaissance: even if a caller hands the full health payload in,
    # the sentence must not quote hosts or URIs.
    assert "localhost" not in text
    assert "5432" not in text
    assert "5001" not in text


def test_unavailable_narrator_is_stated_not_guessed():
    text = STATUS.format_status({
        "llm": {"available": False, "model": None, "provider": "ollama"},
        "tracing": {"enabled": False},
        "postgres": {"n_patterns": None},
    })
    assert "unavailable" in text
    assert "qwen" not in text.lower()
    assert "Tracing:** off" in text
    assert "could not be read" in text
    assert "not the same as zero" in text


def test_status_answer_uses_the_same_probe_as_health(monkeypatch):
    """`_status_answer` must not grow a second source of truth. Stub the shared
    probe and the sentence has to quote those values — including a model name that
    is not the configured default, so we know it is not being invented."""
    snap = {
        "llm": {"available": True, "model": "stub-model:9b", "provider": "ollama"},
        "tracing": {"enabled": True},
        "postgres": {"n_patterns": 7, "monthly_rows": 84},
        "knowledge": {"docs": 0, "chunks": 0, "pending_review": 0},
        "retrieval_min_score": 0.62,
    }
    monkeypatch.setattr(CH.RT, "probe", lambda: snap)
    out = CH._status_answer()
    assert out["intent"] == "status"
    assert out["meta"]["llm"] is False
    assert "stub-model:9b" in out["text"]
    assert "**7**" in out["text"]
    assert out["data"] is snap


def test_respond_does_not_resolve_a_pattern_first(monkeypatch):
    """Status is what you ask when Postgres may be down. `list_patterns` must not
    run before the status short-circuit, or the question that diagnoses a down
    database would 500 trying to list patterns."""
    def boom():
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(CH.T, "list_patterns", boom)
    monkeypatch.setattr(CH.RT, "probe", lambda: {
        "llm": {"available": False, "model": None, "provider": "ollama"},
        "tracing": {"enabled": False},
        "postgres": {"n_patterns": None},
    })
    out = CH.respond("are you connected to an LLM?")
    assert out["intent"] == "status"
    assert "unavailable" in out["text"]


def test_help_topic_still_describes_the_status_path():
    """If the chat topic stops mentioning status, the agent describes an app that
    no longer routes those questions."""
    from vrr_agent_open.core import help_topics as HELP

    body = HELP.BY_ID["chat"].body
    assert "status" in body
    assert "which model?" in body


def test_architecture_counts_pre_table_intents():
    """The diagram's intent count used to be `len(INTENTS)` and silently omitted
    `help`. Adding `status` without counting both would leave the box a lie."""
    from vrr_agent_open.agent.chat import INTENTS, PRE_TABLE_INTENTS

    assert "status" in PRE_TABLE_INTENTS
    assert "help" in PRE_TABLE_INTENTS
    assert len(INTENTS) + len(PRE_TABLE_INTENTS) == 12
