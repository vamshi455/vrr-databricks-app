"""The agent as an MLflow model — logged from code, versioned, alias-promoted.

Registering buys three things the loose functions don't:

  * **attribution** — traces and evaluation runs carry a ``model_id``, so a score belongs
    to a specific version of the agent rather than to "whatever was on disk that day";
  * **promotion** — a version can be aliased ``champion`` only after an evaluation run,
    which is the gate the parent design asks for;
  * **a stable contract** — ``ResponsesAgent`` fixes the request/response shape, so the
    thing being evaluated is the thing that would be served.

It is a thin wrapper on purpose: all behaviour stays in :mod:`vrr_agent_open.agent.chat`
so the API, the CLI and the logged model cannot drift apart. Logged with
``python_model`` pointing at this file's path (log-model-from-code), so the artifact
records source rather than a pickled object.

Registration is **not** required to evaluate traces — ``mlflow.genai.evaluate`` scores
traces that already exist. It is required to say *which* agent produced them.
"""
from __future__ import annotations

from typing import Any

import mlflow
from mlflow.pyfunc import ResponsesAgent
from mlflow.types.responses import ResponsesAgentRequest, ResponsesAgentResponse

# Absolute imports, not relative: `log_model(python_model=<this file>)` imports the file
# as a standalone module with no parent package, so `from ..prompts import ...` fails there
# while working fine in-package. Absolute form works in both.
from vrr_agent_open.agent import chat as CH
from vrr_agent_open.prompts import PROMPTS, prompt_version


def _last_user_text(request: ResponsesAgentRequest) -> str:
    """The question, from the Responses-API input list."""
    for item in reversed(list(request.input or [])):
        data = item if isinstance(item, dict) else item.model_dump()
        if data.get("role") in (None, "user"):
            content = data.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):          # [{type: input_text, text: ...}]
                for part in reversed(content):
                    text = (part if isinstance(part, dict) else part.model_dump()).get("text")
                    if text:
                        return text
    return ""


def _context(request: ResponsesAgentRequest) -> dict[str, Any]:
    """Sidebar context (pattern / period / agentic mode) passed as request metadata."""
    meta = dict(request.custom_inputs or {}) if hasattr(request, "custom_inputs") else {}
    meta.update(dict(request.metadata or {}))
    return meta


class VRRAgent(ResponsesAgent):
    """Answers one analyst question about VRR, deterministically where it matters."""

    def predict(self, request: ResponsesAgentRequest) -> ResponsesAgentResponse:
        question = _last_user_text(request)
        ctx = _context(request)
        result = CH.respond(
            question,
            pattern=ctx.get("pattern"),
            date=ctx.get("date"),
            use_llm=str(ctx.get("use_llm", "true")).lower() != "false",
            agentic=str(ctx.get("agentic", "false")).lower() == "true",
        )
        return ResponsesAgentResponse(
            output=[self.create_text_output_item(text=result["text"], id="answer")],
            # Everything a reviewer needs to judge the answer travels with it: the route
            # taken, whether the LLM was involved, the gate verdict, and which prompts.
            custom_outputs={
                "intent": result.get("intent"),
                "meta": result.get("meta"),
                "prompt_versions": {name: prompt_version(name) for name in PROMPTS},
            },
        )


# `log_model(python_model=<this file>)` imports the module and looks for the model set here.
AGENT = VRRAgent()
mlflow.models.set_model(AGENT)
