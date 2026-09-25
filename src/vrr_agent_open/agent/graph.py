"""The agent tool loop, as a real LangGraph ``StateGraph``.

    START ─▶ plan ──(tool_calls)──▶ tools ─┐
               │                           │
               │ (no tool_calls)           └──▶ plan   (loop, bounded by max_steps)
               ▼
             gate ──(rejected, first try)──▶ repair ──▶ gate ──▶ END
               │                                                  ▲
               └──(passed, or already repaired)───────────────────┘

Nodes
  plan    — the LLM chooses a tool (or answers). The ONLY node that may speak.
  tools   — executes the chosen tools deterministically over Postgres (`tools.py`),
            harvesting every returned number into the `facts` whitelist.
  gate    — `core.faithfulness`: the narration may only name drivers the decomposition
            supports, in the direction it computed, using tool-sourced numbers.
  repair  — one rewrite attempt with the violation fed back, tools withheld.
  budget  — terminal node when the loop hits `max_steps` without an answer.

Why a graph and not a while-loop: the state schema below is the contract (reducers make
`messages`/`trace`/`facts` append-only, so no node can quietly drop evidence), the gate is
an edge the answer cannot route around, and the checkpointer makes a run resumable —
`run(..., thread_id=...)` continues an existing conversation instead of restarting it.

The LLM plans and narrates; it never computes. Every number comes from `tools.py` or
`core/`, and the gate verifies the narration against the tool output before an analyst
sees it. A rejected answer is retried once, then REPLACED by the computed attribution.

Run: `python -m vrr_agent_open.agent.graph "Why is UNITY's VRR high in April 2026?"`
"""
from __future__ import annotations

import json
import operator
import sys
from typing import Annotated, Any, Literal, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph

from ..config import load_config
from ..core import faithfulness as FA
from ..prompts import DOMAIN
from . import llm, tracing
from . import tools as T

CFG = load_config()


class State(TypedDict, total=False):
    """What flows between nodes. The `Annotated[..., operator.add]` fields are the
    append-only evidence trail: a node returns only what it adds, never the whole list,
    so nothing that was already computed can be overwritten by a later step."""
    messages: Annotated[list[dict], operator.add]
    trace: Annotated[list[dict], operator.add]        # {tool, args, result} per call
    facts: Annotated[list[float], operator.add]       # numbers the answer may cite
    last_decompose: dict | None                       # newest VRR_DECOMPOSE result
    answer: str
    gate: dict
    steps: int                                        # plan turns taken
    max_steps: int
    repaired: bool
    model: str | None


# --------------------------------------------------------------------- helpers ----
def _numbers_in(obj: Any, out: list[float]) -> list[float]:
    """Every numeric value a tool returned — the whitelist for check_numbers."""
    if isinstance(obj, bool) or obj is None:
        return out
    if isinstance(obj, (int, float)):
        out.append(float(obj))
        # also allow the same figure expressed as a percentage or rounded
        out.extend([float(obj) * 100, round(float(obj), 2), round(float(obj), 3)])
    elif isinstance(obj, dict):
        for v in obj.values():
            _numbers_in(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _numbers_in(v, out)
    return out


def _repair_prompt(verdict: dict) -> str:
    bits = [v["detail"] for v in verdict.get("violations", [])]
    if verdict.get("uncited_numbers"):
        bits.append("These figures appear in your answer but no tool produced them: "
                    f"{verdict['uncited_numbers']}. Use only numbers from tool results.")
    return ("Your answer was rejected by the faithfulness gate: " + " ".join(bits)
            + " Rewrite it using only the tool results, without new tool calls.")


def _verdict(answer: str, decompose: dict | None, facts: list[float]) -> dict:
    faith = FA.check_faithfulness(answer, decompose)
    nums = FA.check_numbers(answer, facts) if facts else {"ok": True, "uncited": []}
    return {"ok": faith["ok"] and nums["ok"], "violations": faith["violations"],
            "uncited_numbers": nums.get("uncited", []), "supported": faith["supported"]}


def _computed_fallback(decompose: dict | None) -> str | None:
    """The answer we show instead of rejected narration: terse and right beats fluent
    and wrong. None when there is no decomposition to fall back to."""
    if not decompose or not decompose.get("ok"):
        return None
    lines = ["⚠️ The narration was rejected by the faithfulness gate.", "",
             "Computed attribution:"]
    for d in decompose["drivers"]:
        lines.append(f"- {d['label']}: {d['contribution']:+.4f} VRR "
                     f"({d['share']*100:.1f}% of the move)")
    return "\n".join(lines)


# ----------------------------------------------------------------------- nodes ----
@tracing.trace("node.plan", span_type="LLM")
def plan(state: State) -> dict:
    """The model's turn: call a tool, or answer. Never both — Ollama returns one or
    the other, and an answer is what routes us to the gate."""
    msg = llm.chat(state["messages"], tools=T.TOOL_SPECS, model=state.get("model"))
    return {"messages": [msg], "steps": state.get("steps", 0) + 1}


@tracing.trace("node.tools", span_type="CHAIN")
def call_tools(state: State) -> dict:
    """Execute every tool the model asked for, deterministically, over Postgres."""
    calls = (state["messages"][-1].get("tool_calls") or [])
    added, trace, facts = [], [], []
    decompose = state.get("last_decompose")
    for tc in calls:
        fn = tc.get("function", {})
        args = fn.get("arguments") or {}
        if isinstance(args, str):
            args = json.loads(args or "{}")
        name = fn.get("name", "")
        result = T.call_tool(name, args)
        if name == "VRR_DECOMPOSE" and result.get("ok"):
            decompose = result                  # what the gate checks drivers against
        _numbers_in(result, facts)
        trace.append({"tool": name, "args": args, "result": result})
        added.append({"role": "tool", "name": name,
                      "content": json.dumps(result, default=str)[:8000]})
    return {"messages": added, "trace": trace, "facts": facts,
            "last_decompose": decompose}


@tracing.trace("node.gate", span_type="CHAIN")
def gate(state: State) -> dict:
    """Faithfulness gate. Sets `answer` to the model's text when it verifies, and to the
    computed attribution when it does not and there is one to fall back to."""
    answer = state["messages"][-1].get("content") or ""
    verdict = _verdict(answer, state.get("last_decompose"), state.get("facts") or [])
    if state.get("repaired"):
        verdict["retried"] = True
    if verdict["ok"]:
        return {"answer": answer, "gate": verdict}
    # Failing narration is replaced immediately, so whatever leaves the graph is always
    # safe to show. `repair` rewrites from `messages`, not from `answer`, so a repair
    # attempt still sees the model's own words.
    return {"answer": _computed_fallback(state.get("last_decompose")) or answer,
            "gate": verdict}


@tracing.trace("node.repair", span_type="LLM")
def repair(state: State) -> dict:
    """One rewrite, with the violation fed back and tools withheld so the model cannot
    go fishing for new numbers to justify the old claim."""
    msg = llm.chat(state["messages"] + [{"role": "user",
                                         "content": _repair_prompt(state["gate"])}],
                   tools=None, model=state.get("model"))
    return {"messages": [msg], "repaired": True}


def budget(state: State) -> dict:
    """Terminal: the loop spent its tool budget without producing an answer."""
    return {"answer": "Could not complete within the tool budget — narrow the question.",
            "gate": {"ok": False, "reason": "step budget exhausted"}}


# ----------------------------------------------------------------------- edges ----
def after_plan(state: State) -> Literal["tools", "gate", "budget"]:
    if state["messages"][-1].get("tool_calls"):
        return "tools" if state.get("steps", 0) < state.get("max_steps", 6) else "budget"
    return "gate"


def after_gate(state: State) -> Literal["repair", "__end__"]:
    """The one edge that matters: an answer leaves the graph only once the gate has
    cleared it, or once it has already had its single repair attempt."""
    if state["gate"].get("ok") or state.get("repaired"):
        return END
    if not llm.available():                     # nothing to repair with
        return END
    return "repair"


def build() -> Any:
    """Compile the graph. Separate from `run` so tests and notebooks can inspect it
    (`build().get_graph().draw_mermaid()`)."""
    g = StateGraph(State)
    g.add_node("plan", plan)
    g.add_node("tools", call_tools)
    g.add_node("gate", gate)
    g.add_node("repair", repair)
    g.add_node("budget", budget)

    g.add_edge(START, "plan")
    g.add_conditional_edges("plan", after_plan,
                            {"tools": "tools", "gate": "gate", "budget": "budget"})
    g.add_edge("tools", "plan")
    g.add_conditional_edges("gate", after_gate, {"repair": "repair", END: END})
    g.add_edge("repair", "gate")                # the repaired text is gated too
    g.add_edge("budget", END)
    return g.compile(checkpointer=InMemorySaver())


# Compiled once per process: the checkpointer lives in it, so a `thread_id` reused
# across calls continues that conversation instead of starting a new one.
GRAPH = build()
_THREADS = 0


@tracing.trace("agent.tool_loop", span_type="AGENT")
def run(question: str, *, pattern: str | None = None, date: str | None = None,
        max_steps: int = 6, model: str | None = None,
        thread_id: str | None = None) -> dict:
    """Run the graph. Returns text, the tool trace, and the gate verdict.

    Pass a stable `thread_id` to continue an earlier run (the checkpointer keeps its
    messages and evidence); omit it for a fresh conversation.
    """
    global _THREADS
    resuming = thread_id is not None
    if not resuming:
        _THREADS += 1
        thread_id = f"vrr-{_THREADS}"

    hint = ""
    if pattern or date:
        hint = (f"\n\nThe analyst is currently looking at "
                f"{'pattern ' + pattern if pattern else ''}"
                f"{' for period ' + date if date else ''}. Use that unless the question "
                "names another pattern or period.")
    opening: list[dict] = ([] if resuming
                           else [{"role": "system", "content": DOMAIN + hint}])
    opening.append({"role": "user", "content": question})

    # Each plan turn can fan out to tools and back, so the graph takes more super-steps
    # than the model takes turns; `max_steps` stays the budget the analyst reasons about
    # and the recursion limit is just the backstop under it.
    config = {"configurable": {"thread_id": thread_id},
              "recursion_limit": max_steps * 3 + 10}
    seed: State = {"messages": opening, "steps": 0, "max_steps": max_steps,
                   "model": model, "repaired": False}
    if not resuming:
        seed.update({"trace": [], "facts": [], "last_decompose": None})

    try:
        final = GRAPH.invoke(seed, config=config)
    except GraphRecursionError:
        return {"text": "Could not complete within the tool budget — narrow the question.",
                "trace": [], "gate": {"ok": False, "reason": "recursion limit"},
                "decompose": None, "thread_id": thread_id}

    return {"text": final.get("answer", ""), "trace": final.get("trace", []),
            "gate": final.get("gate", {"ok": False, "reason": "no verdict"}),
            "decompose": final.get("last_decompose"), "thread_id": thread_id}


def ask(question: str, pattern: str | None = None, date: str | None = None) -> dict:
    """Entry point the app uses — routes through `agent.chat` (deterministic first,
    LLM tool loop when a local model is up)."""
    from . import chat
    return chat.respond(question, pattern=pattern, date=date)


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "List the patterns and their latest VRR."
    if llm.available():
        out = run(q)
        print(out["text"])
        print(f"\n[tools: {', '.join(t['tool'] for t in out['trace']) or 'none'} | "
              f"gate: {'passed' if out['gate']['ok'] else out['gate']}]")
    else:                       # no Ollama → deterministic path, still fully answerable
        print(ask(q)["text"])
