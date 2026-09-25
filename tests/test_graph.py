"""LangGraph tool loop — topology and the paths through it, with no LLM and no Postgres.

`agent/graph.py` is the only module where the model is allowed to steer, so the things
worth pinning are the edges, not the prose: a tool call must route back to `plan`, an
answer must route through `gate`, a rejected answer gets exactly one repair, and a model
that never stops calling tools must hit the budget node instead of looping forever.

Both `llm.chat` and `tools.call_tool` are stubbed, so these run in the pure-unit tier
(`pytest -q`, no stack) alongside `core/`.
"""
from __future__ import annotations

import pytest

from vrr_agent_open.agent import graph as G

# A decomposition the gate can check narration against: water production dominates.
DECOMPOSE = {
    "ok": True,
    "drivers": [
        {"term": "water_res", "label": "water production",
         "contribution": 0.0294, "share": 0.60},
        {"term": "oil_res", "label": "oil production",
         "contribution": 0.0130, "share": 0.26},
        {"term": "water_inj_res", "label": "water injection",
         "contribution": -0.0069, "share": 0.14},
    ],
}
FAITHFUL = "The move was driven by water production, which contributed +0.0294 VRR."
# Names a term the numbers rank at 14%, and cites a figure no tool produced.
UNFAITHFUL = "The rise was caused by water injection, which added 3.36 VRR."


class FakeLLM:
    """Replays a scripted sequence of assistant messages, recording what it was asked."""

    def __init__(self, *replies: dict):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def __call__(self, messages, tools=None, model=None, **kw):
        self.calls.append({"messages": list(messages), "tools_offered": bool(tools)})
        return self.replies.pop(0) if self.replies else {"content": "done"}


def tool_call(name: str, **args) -> dict:
    return {"content": "", "tool_calls": [{"function": {"name": name, "arguments": args}}]}


@pytest.fixture
def wire(monkeypatch):
    """Install a fake model and a fake tool layer; return the fake model."""
    def _wire(*replies, tool_result=None):
        fake = FakeLLM(*replies)
        monkeypatch.setattr(G.llm, "chat", fake)
        monkeypatch.setattr(G.llm, "available", lambda *a, **k: True)
        monkeypatch.setattr(G.T, "call_tool",
                            lambda name, args: tool_result or {"ok": True, "vrr": 1.046})
        return fake
    return _wire


def test_graph_has_the_edges_the_design_claims():
    """The gate must be a node on the path to END, not advice the answer can skip."""
    drawn = G.build().get_graph()
    nodes = {n for n in drawn.nodes}
    assert {"plan", "tools", "gate", "repair", "budget"} <= nodes
    edges = {(e.source, e.target) for e in drawn.edges}
    assert ("tools", "plan") in edges            # a tool result goes back to the model
    assert ("repair", "gate") in edges           # repaired text is gated too
    assert ("gate", "__end__") in edges


def test_tool_result_feeds_the_next_turn_and_lands_in_the_trace(wire):
    fake = wire(tool_call("VRR_DECOMPOSE", pattern_id="P1"),
                {"content": FAITHFUL},
                tool_result=DECOMPOSE)
    out = G.run("why did it move?", max_steps=6)

    assert out["gate"]["ok"] is True
    assert out["text"] == FAITHFUL
    assert [t["tool"] for t in out["trace"]] == ["VRR_DECOMPOSE"]
    # the model's second turn saw the tool output — that is what "grounded" means here
    second_turn = fake.calls[1]["messages"]
    assert any(m.get("role") == "tool" for m in second_turn)


def test_rejected_narration_is_repaired_once_then_replaced(wire):
    fake = wire(tool_call("VRR_DECOMPOSE", pattern_id="P1"),
                {"content": UNFAITHFUL},           # first answer: wrong driver + made-up number
                {"content": UNFAITHFUL},           # repair attempt: no better
                tool_result=DECOMPOSE)
    out = G.run("why did it move?")

    assert out["gate"]["ok"] is False
    assert out["gate"]["retried"] is True
    assert 3.36 in out["gate"]["uncited_numbers"]
    assert "rejected by the faithfulness gate" in out["text"]
    assert "water production: +0.0294 VRR" in out["text"]     # computed attribution shown
    assert len(fake.calls) == 3                               # plan, plan, repair — one repair
    assert fake.calls[-1]["tools_offered"] is False           # repair may not call tools


def test_repair_that_succeeds_is_kept(wire):
    wire(tool_call("VRR_DECOMPOSE", pattern_id="P1"),
         {"content": UNFAITHFUL},
         {"content": FAITHFUL},                    # the model fixes it when told what broke
         tool_result=DECOMPOSE)
    out = G.run("why did it move?")

    assert out["text"] == FAITHFUL
    assert out["gate"]["ok"] is True
    assert out["gate"]["retried"] is True


def test_a_model_that_only_calls_tools_hits_the_budget(wire):
    wire(*[tool_call("VRR_GET", pattern_id="P1") for _ in range(10)])
    out = G.run("loop forever", max_steps=3)

    assert out["gate"]["ok"] is False
    assert out["gate"]["reason"] == "step budget exhausted"
    # max_steps counts the model's turns: it got 3, and the third one is spent finding
    # the budget node rather than running more tools. The recursion cap never fires.
    assert len(out["trace"]) == 2


def test_thread_id_resumes_the_same_conversation(wire):
    wire({"content": FAITHFUL}, {"content": FAITHFUL})
    first = G.run("why did it move?", thread_id="t-resume")
    G.run("and the month before?", thread_id="t-resume")

    kept = G.GRAPH.get_state({"configurable": {"thread_id": "t-resume"}}).values
    # one system + two user turns + two answers: the checkpointer kept the first exchange
    assert sum(1 for m in kept["messages"] if m.get("role") == "user") == 2
    assert first["thread_id"] == "t-resume"
