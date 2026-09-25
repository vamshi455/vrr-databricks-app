"""The judges were dead for a reason worth a regression test.

`.env` shipped `OPENAI_API_KEY=` for the optional hosted path. `load_dotenv` puts that
empty string into the environment, so the key is *present but falsy* — and
`os.environ.setdefault` only fills a key that is ABSENT. MLflow checks truthiness, so
every judge raised "OPENAI_API_KEY environment variable must be set" before it reached a
model, and `make eval` reported near-zero means that looked like bad judgement rather
than no judgement at all.

Every test here pins BOTH `VRR_JUDGE_MODEL` and the key. The first cut of this file
pinned only the key and inherited the model from whatever `.env` happened to hold — so
the suite passed or failed depending on whose machine ran it, which is the same class of
hidden coupling the module itself was guilty of.
"""
from __future__ import annotations

import importlib
import os

import pytest

LOCAL = "openai:/qwen2.5:7b"
HOSTED = "openai:/gpt-4o-mini"


@pytest.fixture
def judges(monkeypatch):
    """Reload the module under an explicit (model, key) pair."""
    def _go(model: str, key: str | None):
        monkeypatch.setenv("VRR_JUDGE_MODEL", model)
        monkeypatch.delenv("OPENAI_API_BASE", raising=False)
        if key is None:
            monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        else:
            monkeypatch.setenv("OPENAI_API_KEY", key)
        import vrr_agent_open.evaluation.custom_judges as cj
        return importlib.reload(cj)
    return _go


# ---- the bug: an empty key is not an absent key --------------------------

def test_an_empty_key_from_dotenv_is_replaced_for_a_local_judge(judges):
    """The actual failure. `setdefault` left "" in place and MLflow refused to start."""
    judges(LOCAL, "")
    assert os.environ["OPENAI_API_KEY"] == "ollama-local"


def test_a_missing_key_is_filled_for_a_local_judge(judges):
    judges(LOCAL, None)
    assert os.environ["OPENAI_API_KEY"] == "ollama-local"


def test_a_real_key_is_never_clobbered(judges):
    judges(LOCAL, "sk-a-real-key")
    assert os.environ["OPENAI_API_KEY"] == "sk-a-real-key"


def test_a_hosted_judge_gets_no_invented_key(judges):
    """A fake key would turn a clear "no credential" into a 401 from OpenAI, which is a
    worse error to debug than the one it replaces."""
    judges(HOSTED, None)
    assert os.environ.get("OPENAI_API_KEY") in (None, "")


# ---- the endpoint: local gets Ollama, hosted gets the SDK default ---------

def test_a_local_judge_posts_to_the_full_ollama_endpoint(judges):
    """MLflow POSTs to this URL verbatim instead of appending /chat/completions, so a
    bare `…/v1` 404s silently and the judge dies with no visible error."""
    cj = judges(LOCAL, "")
    assert cj.JUDGE_BASE_URL.endswith("/chat/completions")


def test_a_hosted_judge_does_not_inherit_the_ollama_endpoint(judges):
    """`openai:/gpt-4o-mini` and `openai:/qwen2.5:7b` are the same provider to MLflow, so
    a hardcoded base_url would quietly send a hosted request to localhost:11434."""
    cj = judges(HOSTED, "sk-a-real-key")
    assert cj.JUDGE_BASE_URL is None


def test_an_explicit_openai_api_base_wins_for_either(judges, monkeypatch):
    monkeypatch.setenv("OPENAI_API_BASE", "https://proxy.example/v1/chat/completions")
    import vrr_agent_open.evaluation.custom_judges as cj
    monkeypatch.setenv("VRR_JUDGE_MODEL", HOSTED)
    cj = importlib.reload(cj)
    assert cj.JUDGE_BASE_URL == "https://proxy.example/v1/chat/completions"


# ---- prompt mode: {{ outputs }} vs {{ trace }} -------------------------------
# ``{{ trace }}`` is MLflow agentic/trace-walking mode. provenance_cited and
# decision_complete only judge the final answer, so they must use ``{{ outputs }}``.
# grounded_in_documents needs the retriever span and stays on ``{{ trace }}``.


def test_provenance_cited_reads_outputs_not_the_trace(judges):
    cj = judges(LOCAL, "")
    assert "{{ outputs }}" in cj.PROVENANCE_CITED
    assert "{{ trace }}" not in cj.PROVENANCE_CITED


def test_decision_complete_reads_outputs_not_the_trace(judges):
    cj = judges(LOCAL, "")
    assert "{{ outputs }}" in cj.DECISION_COMPLETE
    assert "{{ trace }}" not in cj.DECISION_COMPLETE


def test_grounded_in_documents_still_walks_the_trace(judges):
    cj = judges(LOCAL, "")
    assert "{{ trace }}" in cj.GROUNDED_IN_DOCUMENTS
    assert "{{ outputs }}" not in cj.GROUNDED_IN_DOCUMENTS
