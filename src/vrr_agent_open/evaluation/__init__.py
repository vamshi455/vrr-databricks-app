"""Evaluation: deterministic scorers over traces, plus LLM judges where language matters.

    from vrr_agent_open.evaluation import get_scorers
    mlflow.genai.evaluate(data=traces, scorers=get_scorers())

Layout mirrors the split that matters: `custom_scorers` are facts about the span tree,
`custom_judges` are opinions about prose, `get_scorers` assembles them.
"""
from .custom_judges import build_judges
from .custom_scorers import DETERMINISTIC_SCORERS
from .get_scorers import describe, get_scorers

__all__ = ["DETERMINISTIC_SCORERS", "build_judges", "get_scorers", "describe"]
