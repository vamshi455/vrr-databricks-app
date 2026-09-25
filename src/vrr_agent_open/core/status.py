"""Runtime status — answered from live probes, never generated.

"Are you connected to an LLM?" used to fall through to `explain`, which hands the
question to the narrator. A 7B model guessing whether Ollama is up, which model is
configured, or how many patterns are loaded is a worse failure than a wrong VRR:
`core.faithfulness` catches a fabricated number, but a fabricated configuration makes
no numeric claim and passes every check this project has.

So these questions are routed here first (`agent/chat.py::_status_answer`), the same
shape as `help`. The facts themselves are gathered in `agent/runtime.py` — the same
probes `/api/health` uses — and this module only formats them. Pure: no I/O, no model,
no database. Tests pin the wording to the dict it was given, so a missing model name
cannot be filled in from training data.

Abbreviations: LLM (large language model), VRR (voidage replacement ratio).
"""
from __future__ import annotations

# Phrases that are about THIS PROCESS, not about the reservoir and not about using a
# screen. Deliberately long: a bare "model"/"trace"/"pattern" would steal `help`,
# `lineage` ("trace" is a substring of "tracing"), and `list`. Scored by the TOTAL
# length of every match, same rule as `help_topics.match`, so a question that hits
# two short phrases still beats one that hits a generic leftover.
STATUS_PHRASES: tuple[str, ...] = (
    "are you connected",
    "connected to an llm",
    "connected to a llm",
    "connected to the llm",
    "connected to ollama",
    "connected to a large language model",
    "connected to the large language model",
    "which model",
    "what model",
    "which llm",
    "what llm",
    "what provider",
    "which provider",
    "how many patterns?",
    "how many patterns do",
    "how many patterns have",
    "how many patterns loaded",
    "how many patterns are loaded",
    "how many patterns are there",
    "how many patterns in",
    "how many patterns currently",
    "number of patterns",
    "pattern count",
    "count of patterns",
    "are you tracing",
    "is tracing",
    "tracing on",
    "tracing enabled",
    "tracing off",
    "is mlflow",
    "mlflow up",
    "mlflow running",
    "ollama up",
    "ollama running",
    "is ollama",
    "llm available",
    "is the llm",
    "is the model up",
    "is the model running",
    "is the narrator",
    "system status",
    "your status",
    "runtime status",
    "health check",
)

# "how many patterns are off target / furthest from target" is a PORTFOLIO question.
# The prefix "how many patterns" would otherwise steal it because `in` matches a
# prefix. These markers send it back to the keyword table.
_PORTFOLIO_NOT_STATUS = (
    "off target", "furthest", "worst", "needs attention", "ranked",
    "from target",
)


def _matched(question: str) -> list[str]:
    q = (question or "").lower()
    hits = [k for k in STATUS_PHRASES if k in q]
    # Bare "how many patterns" at the end ("how many patterns?" already listed) —
    # strip trailing punctuation so "How many patterns" still matches without
    # opening the door to "how many patterns are off target".
    stripped = q.rstrip("?.! ")
    if stripped.endswith("how many patterns") and "how many patterns" not in hits:
        hits.append("how many patterns")
    return hits


def is_status_question(question: str) -> bool:
    """Is this about the running process rather than the reservoir or the UI?

    Conservative on purpose. A question needs a status phrase; "how many patterns are
    off target" is portfolio even though it starts with a status-looking prefix.
    """
    q = (question or "").lower()
    hits = _matched(question)
    if not hits:
        return False
    pattern_count = any("pattern" in h for h in hits)
    if pattern_count and any(m in q for m in _PORTFOLIO_NOT_STATUS):
        return False
    return match_score(question) > 0


def match_score(question: str) -> int:
    """Total length of every matched phrase — same proxy `help_topics.match` uses."""
    return sum(len(k) for k in _matched(question))


def format_status(facts: dict) -> str:
    """Written status from a facts dict. Missing keys are stated as unknown, never invented.

    `facts` is the shape `agent.runtime.probe` returns (and `/api/health` wraps):
    `llm.{available,model,provider}`, `tracing.enabled`, `postgres.n_patterns`.
    """
    llm = facts.get("llm") or {}
    tracing = facts.get("tracing") or {}
    postgres = facts.get("postgres") or {}

    provider = llm.get("provider") or "unknown"
    model = llm.get("model")
    available = bool(llm.get("available"))
    if available and model:
        narrator = f"**{provider}** · `{model}` — available"
    elif available:
        narrator = f"**{provider}** — available (no model name reported)"
    else:
        narrator = (f"**{provider}** — unavailable. Answers still compute from the "
                    "deterministic tools; they are not rephrased by a model.")

    traced = tracing.get("enabled")
    if traced is True:
        tracing_line = "on"
    elif traced is False:
        tracing_line = "off"
    else:
        tracing_line = "unknown (probe did not return a value)"

    n = postgres.get("n_patterns")
    if n is None:
        patterns_line = ("could not be read — the database did not answer. "
                         "That is not the same as zero patterns.")
    else:
        patterns_line = (f"**{n}** loaded in `vrr_curated.pattern_vrr` "
                         "(the same listing the Pattern dropdown uses)")

    return (
        "**Runtime status** — measured from the same probes `/api/health` uses, "
        "not generated.\n\n"
        f"- **Narrator (large language model, LLM):** {narrator}\n"
        f"- **Tracing:** {tracing_line}\n"
        f"- **Patterns:** {patterns_line}\n\n"
        "A model is never asked to describe its own configuration."
    )
