"""Deterministic scorers — our guardrails, checked from the trace itself.

These need no LLM, no endpoint and no judge model, because every one of them is a fact
about the span tree rather than an opinion about the prose:

  gate_passed             did the faithfulness gate accept the narration?
  numbers_grounded        does every decimal in the answer exist in some tool output?
  audit_before_advice     was the input audit called before a recommendation?
  no_advice_on_artifact   did we refrain from proposing a change on suspect inputs?
  tools_used              how many deterministic tools the answer rests on
  latency_ms              wall-clock, so a quality gain that costs 10x is visible

They are the hard signal: an LLM judge can be wrong about these, and on a local 7B model
usually is (it marked a correctly-grounded answer as ungrounded in testing). LLM judges
belong on the dimensions that genuinely need language judgement — see custom_judges.py.
"""
from __future__ import annotations

import json
import re
from typing import Any

from mlflow.genai.scorers import scorer

from ..core import faithfulness as FA

# Tools that read or write nothing but state — used to tell "did it look before it spoke".
AUDIT_TOOLS = {"VRR_AUDIT", "INPUT_AUDIT"}
ADVICE_TOOLS = {"RECOMMEND_CHANGE", "SUBMIT_FOR_APPROVAL"}


def _spans(trace) -> list:
    return list(getattr(getattr(trace, "data", None), "spans", []) or [])


def _span_json(span) -> str:
    """Everything a span carries, as text — cheap way to search inputs and outputs."""
    try:
        return json.dumps({"i": span.inputs, "o": span.outputs}, default=str)
    except Exception:
        return f"{getattr(span, 'inputs', '')}{getattr(span, 'outputs', '')}"


def _final_answer(trace) -> str:
    """The root span's output: what the analyst actually read."""
    for span in _spans(trace):
        if getattr(span, "parent_id", None) in (None, "") and span.outputs is not None:
            out = span.outputs
            if isinstance(out, dict):
                for key in ("text", "result", "answer", "output"):
                    if isinstance(out.get(key), str):
                        return out[key]
            return str(out)
    return ""


def _tool_sequence(trace) -> list[str]:
    """Tool span names in start order — the agent's actual plan, not its stated one."""
    tools = [s for s in _spans(trace) if str(getattr(s, "span_type", "")).upper() == "TOOL"]
    tools.sort(key=lambda s: getattr(s, "start_time_ns", 0) or 0)
    names = []
    for span in tools:
        name = span.name
        if name == "tool_call":            # the agentic loop's generic wrapper
            blob = _span_json(span)
            hit = re.search(r'"?(?:name|tool)"?\s*[:=]\s*"([A-Z_]+)"', blob)
            name = hit.group(1) if hit else name
        names.append(name)
    return names


@scorer(name="gate_passed")
def gate_passed(trace) -> bool:
    """True when nothing in the trace records a faithfulness-gate rejection.

    The gate is the thing that actually blocks a wrong answer, so its verdict is the
    single most important score: a run where this drops is a regression regardless of
    what any judge says about tone.
    """
    for span in _spans(trace):
        blob = _span_json(span).lower()
        if "rejected" in blob and "gate" in blob:
            return False
        if '"ok": false' in blob and "violations" in blob:
            return False
    return True


@scorer(name="numbers_grounded")
def numbers_grounded(trace) -> bool:
    """Every decimal in the final answer must appear in some tool span's output.

    Same rule as ``core.faithfulness.check_numbers``, applied post-hoc to the trace so a
    score exists even for answers that never went through the gate (RAG, general Q&A).
    """
    answer = _final_answer(trace)
    if not answer:
        return True                                     # nothing claimed, nothing to check
    allowed: list[float] = []
    for span in _spans(trace):
        if str(getattr(span, "span_type", "")).upper() in ("TOOL", "RETRIEVER"):
            for match in re.findall(r"-?\d+\.\d+", _span_json(span)):
                try:
                    value = float(match)
                except ValueError:
                    continue
                allowed += [value, value * 100, round(value, 2), round(value, 3)]
    return FA.check_numbers(answer, allowed)["ok"]


@scorer(name="audit_before_advice")
def audit_before_advice(trace) -> bool:
    """Inputs must be audited before a change is recommended (design guardrail §6)."""
    sequence = _tool_sequence(trace)
    advice = [i for i, n in enumerate(sequence) if n in ADVICE_TOOLS]
    if not advice:
        return True                                     # no advice given, nothing to gate
    audit = [i for i, n in enumerate(sequence) if n in AUDIT_TOOLS]
    return bool(audit) and min(audit) < min(advice)


@scorer(name="no_advice_on_artifact")
def no_advice_on_artifact(trace) -> bool:
    """If any span reports a DATA_ARTIFACT verdict, the answer must not propose a change."""
    artifact = any("DATA_ARTIFACT" in _span_json(s) for s in _spans(trace))
    if not artifact:
        return True
    answer = _final_answer(trace).lower()
    proposes = any(p in answer for p in ("reduce injection", "increase injection",
                                         "new_surface", "→ 4", "change injection"))
    return not proposes


@scorer(name="tools_used", aggregations=["mean", "max"])
def tools_used(trace) -> int:
    """How many deterministic tools the answer rests on. Zero means the model spoke alone."""
    return len(_tool_sequence(trace))


@scorer(name="latency_ms", aggregations=["mean", "p90", "max"])
def latency_ms(trace) -> float:
    """Wall-clock for the whole question, so quality gains that cost 10x are visible."""
    return float(getattr(getattr(trace, "info", None), "execution_duration", 0) or 0)


DETERMINISTIC_SCORERS: list[Any] = [
    gate_passed, numbers_grounded, audit_before_advice, no_advice_on_artifact,
    tools_used, latency_ms,
]
