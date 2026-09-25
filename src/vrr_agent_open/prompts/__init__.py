"""Prompt templates, kept in one place so they can be versioned independently of code.

Every instruction the LLM ever receives lives in :mod:`vrr_agent_open.prompts.templates`.
Two reasons this is worth a module of its own rather than string constants next to the
call sites:

  * the reservoir-domain primer changes far more often than the loop that uses it, and
  * an evaluation run is only meaningful if you can say which prompt version produced it
    (``scripts/register_prompt.py`` pushes these into the MLflow Prompt Registry).
"""
from .templates import (DOMAIN, GENERAL_SYSTEM, KNOWLEDGE_SYSTEM, NARRATOR_SYSTEM,
                        PROMPTS, prompt_version)

__all__ = ["DOMAIN", "GENERAL_SYSTEM", "KNOWLEDGE_SYSTEM", "NARRATOR_SYSTEM", "PROMPTS",
           "prompt_version"]
