"""Does inference actually work — for every provider that is configured?

Answers three questions per provider, in the order they break:
  1. is it reachable / keyed?
  2. does a plain completion come back?
  3. does TOOL CALLING work — the one capability the agent graph depends on?

A provider that passes (1) and (2) but fails (3) can narrate but cannot drive the
agentic loop, which is worth knowing before you switch `VRR_LLM_PROVIDER` and watch
every question fall back to the deterministic answer.

Run: `make llm-check`   (add a provider name to test just one: `make llm-check p=openai`)
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, "src")

from vrr_agent_open.agent import llm, providers
from vrr_agent_open.config import load_config

CFG = load_config()
PING = [{"role": "user", "content": "Reply with exactly: VRR OK"}]
TOOL_PROBE = [{"role": "user",
               "content": "List the patterns and their latest VRR. Use the tool."}]
TOOL_SPEC = [{"type": "function", "function": {
    "name": "LIST_PATTERNS", "description": "List patterns and their latest VRR.",
    "parameters": {"type": "object", "properties": {}, "required": []}}}]


def check(provider: str) -> dict:
    out: dict = {"provider": provider, "model": CFG.model_for(provider)}
    if not providers.available(provider):
        out["status"] = ("no API key — set it in .env" if provider != "ollama"
                         else f"not reachable at {CFG.llm_base_url}")
        return out

    started = time.time()
    try:
        reply = llm.chat(PING, provider_name=provider, timeout=60)
        out["completion"] = (reply.get("content") or "").strip()[:40]
    except Exception as e:
        out["status"] = f"completion failed: {type(e).__name__}: {str(e)[:120]}"
        return out

    try:
        reply = llm.chat(TOOL_PROBE, tools=TOOL_SPEC, provider_name=provider, timeout=90)
        calls = [c["function"]["name"] for c in reply.get("tool_calls") or []]
        out["tool_calling"] = ", ".join(calls) if calls else "NOT USED (answered directly)"
    except Exception as e:
        out["tool_calling"] = f"failed: {type(e).__name__}: {str(e)[:120]}"

    out["status"] = "ok"
    out["seconds"] = round(time.time() - started, 1)
    return out


if __name__ == "__main__":
    wanted = sys.argv[1:] or list(providers.PROVIDERS)
    print(f"configured provider: {llm.provider()}   (VRR_LLM_PROVIDER)\n")
    for name in wanted:
        r = check(name)
        mark = "🟢" if r.get("status") == "ok" else "⚪"
        print(f"{mark} {r['provider']:<10} model={r['model']}")
        print(f"     status: {r['status']}")
        if r.get("status") == "ok":
            print(f"     completion: {r['completion']!r}  ({r['seconds']}s)")
            print(f"     tool calling: {r['tool_calling']}")
        print()
