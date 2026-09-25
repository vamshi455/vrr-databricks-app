"""Local LLM client (Ollama by default) — the only place the agent talks to a model.

Kept deliberately thin: one ``chat()`` that speaks the OpenAI-style
``messages``/``tools`` shape Ollama implements at ``/api/chat``. Point
``VRR_LLM_BASE_URL`` at any other OpenAI-compatible endpoint and nothing else changes.

The model is never trusted with arithmetic — see ``agent/graph.py`` for the tool loop
and ``core/faithfulness.py`` for the gate that checks what comes back.
"""
from __future__ import annotations

import httpx

from ..config import load_config
from . import providers, tracing

CFG = load_config()


def provider() -> str:
    """Which backend is configured (`VRR_LLM_PROVIDER`): ollama | openai | anthropic."""
    return (CFG.llm_provider or "ollama").lower()


def available(timeout: float = 1.5) -> bool:
    """Is the configured model backend usable right now?

    Local: is Ollama up. Hosted: is an API key configured. Callers use this to decide
    whether to take the LLM path at all, so it must stay cheap and never raise.
    """
    return providers.available(provider(), timeout)


def models() -> list[str]:
    if provider() != "ollama":
        return [CFG.model_for(provider())]
    try:
        r = httpx.get(f"{CFG.llm_base_url}/api/tags", timeout=3)
        return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return []


def pick_model(preferred: str | None = None) -> str | None:
    """Resolve the configured model against what is actually pulled locally.

    Falls back to any installed chat model (skipping embedding-only models) so a fresh
    machine with a different pull still works instead of erroring on model-not-found.
    Hosted providers have no local inventory to reconcile, so the configured name stands.
    """
    if provider() != "ollama":
        return preferred or CFG.model_for(provider())
    have = models()
    want = preferred or CFG.llm_model
    if not have:
        return None
    for name in have:                                  # exact, then tag-insensitive
        if name == want or name.split(":")[0] == want.split(":")[0]:
            return name
    chat_models = [m for m in have if "embed" not in m]
    return chat_models[0] if chat_models else None


@tracing.trace("llm.chat", span_type="LLM")
def chat(messages: list[dict], tools: list[dict] | None = None,
         model: str | None = None, temperature: float = 0.0,
         timeout: float = 180, provider_name: str | None = None) -> dict:
    """One turn. Returns the assistant message dict ``{content, tool_calls?}``.

    The same call shape for every backend — `providers.py` translates it for whichever
    one is configured, so the graph, the narrator and the RAG path never branch on it.

    ``temperature=0`` by default: this agent narrates computed results, so sampling
    variety is a liability, not a feature.
    """
    who = (provider_name or provider()).lower()
    return providers.complete(who, messages, tools,
                              model or pick_model() or CFG.model_for(who),
                              temperature, timeout)
