"""Assemble the scorer set for an evaluation run.

Deterministic scorers always run — they need nothing but the trace. LLM judges are added
only when a judge model is actually reachable, so `make eval` still produces a meaningful
report on a machine with no model running (it just reports fewer dimensions).
"""
from __future__ import annotations

from typing import Any

from ..agent import llm
from .custom_judges import JUDGE_MODEL, build_judges
from .custom_scorers import DETERMINISTIC_SCORERS


def get_scorers(include_judges: bool = True, model: str | None = None) -> list[Any]:
    """Deterministic scorers, plus LLM judges when a judge model is available."""
    scorers: list[Any] = list(DETERMINISTIC_SCORERS)
    if include_judges and llm.available():
        scorers += build_judges(model)
    return scorers


def describe(scorers: list[Any]) -> str:
    names = ", ".join(getattr(s, "name", type(s).__name__) for s in scorers)
    judges = [s for s in scorers if type(s).__name__ not in ("Scorer", "_CustomScorer")]
    return (f"{len(scorers)} scorer(s): {names}"
            + (f"\n  judge model: {JUDGE_MODEL}" if judges else "\n  judges: skipped (no model)"))
