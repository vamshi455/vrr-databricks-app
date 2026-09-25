"""Provider translation — Ollama's wire format ↔ OpenAI ↔ Anthropic, with no API calls.

The agent speaks ONE shape (`{role, content, tool_calls}` + OpenAI-style tool specs).
Anthropic does not: system prompts leave the message list, a tool call is a `tool_use`
block and its result a `tool_result` block that must reference the call BY ID. If that
pairing is wrong the API rejects the whole conversation, so it is pinned here rather
than discovered against a billable endpoint.

No key required: the SDKs are never constructed — only the pure translation and the
availability/guard logic are exercised.
"""
from __future__ import annotations

import dataclasses

import pytest

from vrr_agent_open.agent import providers as P

# One full agentic turn in our format: system → question → tool call → tool result.
HISTORY = [
    {"role": "system", "content": "You are a VRR analyst."},
    {"role": "user", "content": "Why is UNITY high?"},
    {"role": "assistant", "content": "",
     "tool_calls": [{"function": {"name": "VRR_DECOMPOSE",
                                  "arguments": {"pattern": "UNITY"}}}]},
    {"role": "tool", "name": "VRR_DECOMPOSE", "content": '{"ok": true}'},
]
SPEC = [{"type": "function", "function": {
    "name": "VRR_DECOMPOSE", "description": "Attribute a VRR change.",
    "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}},
                   "required": ["pattern"]}}}]


@pytest.fixture
def keyless(monkeypatch):
    """Config is a frozen dataclass, so swap the whole instance rather than a field —
    which also keeps a real key in the developer's own .env out of these tests."""
    monkeypatch.setattr(P, "CFG", dataclasses.replace(P.CFG, anthropic_api_key="",
                                                      openai_api_key=""))


def test_system_prompt_leaves_the_message_list_for_anthropic():
    system, msgs = P._to_anthropic(HISTORY)

    assert system == "You are a VRR analyst."
    assert all(m["role"] != "system" for m in msgs)


def test_tool_result_references_the_id_of_the_call_it_answers():
    """The pairing the Anthropic API rejects a conversation over."""
    _, msgs = P._to_anthropic(HISTORY)

    use = next(b for m in msgs if m["role"] == "assistant"
               for b in m["content"] if b["type"] == "tool_use")
    result = next(b for m in msgs if m["role"] == "user" and isinstance(m["content"], list)
                  for b in m["content"] if b["type"] == "tool_result")

    assert use["name"] == "VRR_DECOMPOSE"
    assert use["input"] == {"pattern": "UNITY"}
    assert result["tool_use_id"] == use["id"]        # must match, not merely exist


def test_parallel_tool_results_batch_into_one_user_turn():
    """Anthropic wants every result for a turn in ONE user message; two turns is an
    error, and the graph does issue parallel calls."""
    history = [
        {"role": "assistant", "content": "",
         "tool_calls": [{"function": {"name": "A", "arguments": {}}},
                        {"function": {"name": "B", "arguments": {}}}]},
        {"role": "tool", "name": "A", "content": "1"},
        {"role": "tool", "name": "B", "content": "2"},
    ]
    _, msgs = P._to_anthropic(history)

    results = [m for m in msgs if m["role"] == "user"]
    assert len(results) == 1
    assert [b["type"] for b in results[0]["content"]] == ["tool_result", "tool_result"]
    uses = [b["id"] for m in msgs if m["role"] == "assistant"
            for b in m["content"] if b["type"] == "tool_use"]
    assert [b["tool_use_id"] for b in results[0]["content"]] == uses   # in call order


def test_string_arguments_are_parsed_before_they_reach_anthropic():
    """Ollama returns dict arguments, OpenAI a JSON string; Anthropic takes only a dict."""
    history = [{"role": "assistant", "content": "",
                "tool_calls": [{"function": {"name": "VRR_GET",
                                             "arguments": '{"pattern": "UNITY"}'}}]}]
    _, msgs = P._to_anthropic(history)

    assert msgs[0]["content"][0]["input"] == {"pattern": "UNITY"}


def test_a_provider_without_a_key_is_unavailable_rather_than_broken(keyless):
    """`available()` decides whether the LLM path is taken at all, so it must answer
    without raising and without a network call."""
    assert P.available("anthropic") is False
    assert P.available("openai") is False


def test_calling_a_keyless_provider_fails_loudly_with_the_fix(keyless):
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        P.complete("anthropic", HISTORY, SPEC, "claude-sonnet-5", 0.0, 30)


def test_unknown_provider_names_the_valid_ones():
    with pytest.raises(ValueError, match="ollama"):
        P.complete("gemini", HISTORY, None, "x", 0.0, 30)


def test_switching_provider_does_not_change_the_trust_model():
    """Guardrail, stated as a test: the provider list is narration only. If a provider
    ever gains a path that computes or stores, this is the assertion to revisit."""
    assert set(P.DISPATCH) == set(P.PROVIDERS)
    assert P.CFG.llm_provider == "ollama"          # local stays the default
