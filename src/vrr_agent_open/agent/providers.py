"""Model providers behind one wire format — Ollama (default), OpenAI, Anthropic.

`agent/llm.py` calls exactly one shape, the OpenAI-style one Ollama already speaks:

    messages: [{role: system|user|assistant|tool, content: str, tool_calls?: [...]}]
    tools:    [{type: "function", function: {name, description, parameters}}]
    returns:  {content: str, tool_calls?: [{function: {name, arguments: dict}}]}

OpenAI is that format natively. Anthropic is not — it takes `system` out of the message
list, expresses a tool call as a `tool_use` content block and its result as a
`tool_result` block that must be matched BY ID to the call. `_to_anthropic` does that
translation, pairing our id-less tool messages positionally with the calls that preceded
them (the graph appends them in call order, which is what makes this sound).

Nothing about the trust model changes with the provider: the model still only picks
tools and phrases results, `tools.py` still computes every number, and
`core.faithfulness` still gates the narration. A bigger model fails the gate less often;
it is not trusted more.

Hosted providers are BILLABLE and off by default — `VRR_LLM_PROVIDER` stays `ollama`
unless you change it, and a provider with no API key reports itself unavailable rather
than raising.
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx

from ..config import load_config

CFG = load_config()
PROVIDERS = ("ollama", "openai", "anthropic", "databricks")
MAX_TOKENS = 2048          # Anthropic requires this explicitly; OpenAI defaults fine


def key_for(provider: str) -> str:
    return {"anthropic": CFG.anthropic_api_key, "openai": CFG.openai_api_key}.get(
        provider, "")


def available(provider: str, timeout: float = 1.5) -> bool:
    """Can we actually call this provider right now?

    Local means "is the server up"; hosted means "is a key configured" — deliberately
    NOT a live ping, because a network call per availability check would show up on a
    bill and in the latency of every /api/health poll.
    """
    if provider == "ollama":
        try:
            return httpx.get(f"{CFG.llm_base_url}/api/tags",
                             timeout=timeout).status_code == 200
        except Exception:
            return False
    if provider in ("openai", "anthropic"):
        return bool(key_for(provider))
    if provider == "databricks":
        # The app's service principal (or the CLI login on a laptop) is the credential;
        # a workspace host in the env is what makes the endpoint addressable.
        return bool(os.environ.get("DATABRICKS_HOST"))
    return False


# ------------------------------------------------------------------ ollama ----
def _ollama(messages: list[dict], tools: list[dict] | None, model: str,
            temperature: float, timeout: float) -> dict:
    payload: dict[str, Any] = {"model": model, "messages": messages, "stream": False,
                               "options": {"temperature": temperature}}
    if tools:
        payload["tools"] = tools
    r = httpx.post(f"{CFG.llm_base_url}/api/chat", json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json().get("message", {}) or {}


# ------------------------------------------------------------------ openai ----
def _openai(messages: list[dict], tools: list[dict] | None, model: str,
            temperature: float, timeout: float) -> dict:
    from openai import OpenAI

    return _openai_compatible(OpenAI(api_key=CFG.openai_api_key, timeout=timeout),
                              messages, tools, model, temperature)


def _databricks(messages: list[dict], tools: list[dict] | None, model: str,
                temperature: float, timeout: float) -> dict:
    """Databricks Model Serving speaks the OpenAI chat-completions dialect (tool calls
    included) at `/serving-endpoints`; only the base URL and the credential differ."""
    from openai import OpenAI

    from .. import databricks_env as DBX

    client = OpenAI(base_url=DBX.serving_base_url(), api_key=DBX.bearer_token(),
                    timeout=timeout)
    return _openai_compatible(client, messages, tools, model, temperature)


def _openai_compatible(client, messages: list[dict], tools: list[dict] | None,
                       model: str, temperature: float) -> dict:
    # Our tool messages carry `name` but no `tool_call_id`; the Chat Completions API
    # wants the id, so re-derive it from the call that came just before.
    sent, pending = [], []
    for m in messages:
        if m.get("role") == "tool":
            call_id = pending.pop(0) if pending else f"call_{m.get('name')}"
            sent.append({"role": "tool", "tool_call_id": call_id,
                         "content": m.get("content", "")})
            continue
        if m.get("role") == "assistant" and m.get("tool_calls"):
            calls = []
            for i, tc in enumerate(m["tool_calls"]):
                fn = tc.get("function", {})
                call_id = tc.get("id") or f"call_{len(sent)}_{i}"
                pending.append(call_id)
                args = fn.get("arguments")
                calls.append({"id": call_id, "type": "function",
                              "function": {"name": fn.get("name", ""),
                                           "arguments": args if isinstance(args, str)
                                           else json.dumps(args or {})}})
            sent.append({"role": "assistant", "content": m.get("content") or None,
                         "tool_calls": calls})
            continue
        sent.append({"role": m.get("role", "user"), "content": m.get("content", "")})

    kw: dict[str, Any] = {"model": model, "messages": sent, "temperature": temperature}
    if tools:
        kw["tools"] = tools
    choice = client.chat.completions.create(**kw).choices[0].message
    out: dict[str, Any] = {"content": choice.content or ""}
    if choice.tool_calls:
        out["tool_calls"] = [
            {"id": tc.id,
             "function": {"name": tc.function.name,
                          "arguments": json.loads(tc.function.arguments or "{}")}}
            for tc in choice.tool_calls]
    return out


# --------------------------------------------------------------- anthropic ----
def _to_anthropic(messages: list[dict]) -> tuple[str, list[dict]]:
    """Our OpenAI-shaped history → (system prompt, Anthropic messages).

    Tool results must reference the id of the `tool_use` block that requested them, so
    calls are queued as they are seen and popped as their results arrive.
    """
    system_bits, out, pending = [], [], []
    for m in messages:
        role = m.get("role")
        if role == "system":
            system_bits.append(m.get("content", ""))
        elif role == "tool":
            use_id = pending.pop(0) if pending else "toolu_0"
            block = {"type": "tool_result", "tool_use_id": use_id,
                     "content": m.get("content", "")}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)       # batch parallel results in one turn
            else:
                out.append({"role": "user", "content": [block]})
        elif role == "assistant" and m.get("tool_calls"):
            blocks: list[dict] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for i, tc in enumerate(m["tool_calls"]):
                fn = tc.get("function", {})
                use_id = tc.get("id") or f"toolu_{len(out)}_{i}"
                pending.append(use_id)
                args = fn.get("arguments") or {}
                blocks.append({"type": "tool_use", "id": use_id,
                               "name": fn.get("name", ""),
                               "input": json.loads(args) if isinstance(args, str) else args})
            out.append({"role": "assistant", "content": blocks})
        else:
            out.append({"role": role or "user", "content": m.get("content", "")})
    return "\n\n".join(system_bits), out


def _anthropic(messages: list[dict], tools: list[dict] | None, model: str,
               temperature: float, timeout: float) -> dict:
    import anthropic

    client = anthropic.Anthropic(api_key=CFG.anthropic_api_key, timeout=timeout)
    system, msgs = _to_anthropic(messages)
    kw: dict[str, Any] = {"model": model, "messages": msgs, "max_tokens": MAX_TOKENS,
                          "temperature": temperature}
    if system:
        kw["system"] = system
    if tools:                       # {type, function:{...}} → Anthropic's flat schema
        kw["tools"] = [{"name": t["function"]["name"],
                        "description": t["function"].get("description", ""),
                        "input_schema": t["function"].get(
                            "parameters", {"type": "object", "properties": {}})}
                       for t in tools]

    reply = client.messages.create(**kw)
    text = "".join(b.text for b in reply.content if b.type == "text")
    calls = [{"id": b.id, "function": {"name": b.name, "arguments": b.input}}
             for b in reply.content if b.type == "tool_use"]
    out: dict[str, Any] = {"content": text}
    if calls:
        out["tool_calls"] = calls
    return out


DISPATCH = {"ollama": _ollama, "openai": _openai, "anthropic": _anthropic,
            "databricks": _databricks}


def complete(provider: str, messages: list[dict], tools: list[dict] | None,
             model: str, temperature: float, timeout: float) -> dict:
    fn = DISPATCH.get(provider)
    if not fn:
        raise ValueError(f"unknown VRR_LLM_PROVIDER {provider!r}; "
                         f"expected one of {', '.join(PROVIDERS)}")
    if provider in ("openai", "anthropic") and not key_for(provider):
        raise RuntimeError(f"{provider} selected but no API key — set "
                           f"{'ANTHROPIC_API_KEY' if provider == 'anthropic' else 'OPENAI_API_KEY'} "
                           "in .env (see .env.example)")
    return fn(messages, tools, model, temperature, timeout)
