"""LLM judges — for the dimensions that need language judgement, and only those.

Built with ``mlflow.genai.make_judge`` so each one is a versionable artifact rather than a
prompt pasted into the UI. Each judge is scoped deliberately narrowly: the numbers are
already verified deterministically (``custom_scorers``), so these judge *presentation* —
is the evidence cited, is the recommendation complete enough for an engineer to act on,
is a document answer really confined to the documents.

Judge reliability caveat, stated in code because it matters: on a local 7B the judges are
useful for **relative** movement between runs, not as an absolute quality bar. When
disagreement with a deterministic scorer appears, the deterministic scorer is right.
``scripts/run_memalign.py`` aligns a judge against human feedback once you have some.
"""
from __future__ import annotations

import os
from typing import Any

from mlflow.genai import make_judge

from ..config import load_config

CFG = load_config()

# The judge model is separate from the agent's narrator: it may be a bigger local model,
# or a hosted one, without touching how the agent runs.
JUDGE_MODEL = os.environ.get("VRR_JUDGE_MODEL", f"openai:/{CFG.llm_model}")
# Ollama speaks the OpenAI wire format, so the `openai:/` provider reaches it with no
# extra dependency — but `base_url` must be the FULL endpoint, not the API root: MLflow
# POSTs to exactly this URL rather than appending `/chat/completions` to it. With
# `…/v1` every judge died on `404 page not found`, silently, so `make eval` reported the
# 6 deterministic scorers and none of the 3 judges — a green run that had scored nothing
# it claimed to. Overridden by OPENAI_API_BASE if the caller sets one.
#
# That endpoint applies ONLY when the judge is the local model. `openai:/gpt-4o` and `openai:/qwen2.5:7b`
# are both "the openai provider" as far as MLflow is concerned, so a hardcoded Ollama
# endpoint would quietly POST a hosted-model request to localhost:11434. If the judge
# model is not one Ollama serves, leave base_url unset and let the SDK reach OpenAI.
_LOCAL_JUDGE = JUDGE_MODEL.endswith(CFG.llm_model)
_DEFAULT_BASE_URL = f"{CFG.llm_base_url}/v1/chat/completions" if _LOCAL_JUDGE else None
# `or` rather than a `get` default on purpose: an OPENAI_API_BASE that is present but
# empty is the same trap as the empty API key below, and should not win.
JUDGE_BASE_URL = os.environ.get("OPENAI_API_BASE") or _DEFAULT_BASE_URL
# A LOCAL judge still needs a non-empty key: MLflow's openai provider refuses to start
# without one even when the endpoint is Ollama and ignores it entirely. It must be
# NON-EMPTY, and `setdefault` is the wrong tool — `.env` shipped `OPENAI_API_KEY=` for
# the optional hosted path, `load_dotenv` put that empty string into the environment, and
# setdefault only fills an ABSENT key so it left "" in place. MLflow checks truthiness,
# not presence, so every judge died on "OPENAI_API_KEY must be set" before reaching a
# model. Check the VALUE, not the key.
#
# For a HOSTED judge, do not invent a key: a bogus one turns a clear "no credential"
# into a 401 from OpenAI, which is a worse error to debug.
if _LOCAL_JUDGE and not os.environ.get("OPENAI_API_KEY"):
    os.environ["OPENAI_API_KEY"] = "ollama-local"

# ``{{ trace }}`` puts ``make_judge`` in MLflow's USE_CASE_AGENTIC_JUDGE mode: the model
# is not handed the answer and must tool-call through the span tree (``list_spans``,
# ``get_span``, ``search_trace_regex``). That walk defeated both qwen2.5:7b and
# gpt-4o-mini (iteration limit of 30). ``provenance_cited`` and ``decision_complete``
# only need the FINAL ANSWER, so they use ``{{ outputs }}`` (standard non-agentic mode).
# ``grounded_in_documents`` needs the retriever span and stays on ``{{ trace }}``.
# Numbers themselves are already checked by the deterministic scorers; these two judge
# presentation. Where a judge and a deterministic scorer disagree, the deterministic
# scorer is right.

PROVENANCE_CITED = """
Examine {{ outputs }}. Rate the final response on whether it attributes its figures to
their source: the table they came from (vrr_curated.pattern_vrr,
vrr_curated.completion_contrib, or a vrr_raw input table), the PVT lookup method wherever
a reservoir volume is quoted, and the run_id or period where one applies.
Return true if every quantitative claim can be traced to a source named in the response.
Return false if figures are stated with no indication of origin.
""".strip()

DECISION_COMPLETE = """
Examine {{ outputs }}. If the final response proposes an injection change, return true only
if it states all of: the named injector completions affected, the change in surface rate
with its percentage, whether a safety limit clamped it, the expected post-change VRR, the
dominant driver taken from the decomposition, any precedent from history, a confidence
level, and that the recommendation is advisory pending analyst, reservoir-manager and site
approval. Return false if any element is missing.
If the response proposes no change, return true when it says why — the input-audit
verdict, within tolerance, or no adjustable injectors — and false if it simply declines to
answer.
""".strip()

GROUNDED_IN_DOCUMENTS = """
Examine {{ trace }}. This trace answers a question about ingested reservoir documents.
Return true only if every claim in the final response is supported by the text of a
document retrieved in the trace's retriever span, and the response cites the file name and
page it came from. Return false if the response adds domain knowledge that is not in the
retrieved excerpts, or cites a document that was not retrieved.
""".strip()

_SPECS = {
    "provenance_cited": (PROVENANCE_CITED,
                         "Are the figures attributed to the tables that produced them?"),
    "decision_complete": (DECISION_COMPLETE,
                          "Does a proposed change contain everything an engineer needs?"),
    "grounded_in_documents": (GROUNDED_IN_DOCUMENTS,
                              "Is a document answer confined to the retrieved excerpts?"),
}


def build_judges(model: str | None = None) -> list[Any]:
    """The judge set. Boolean-valued, and asked to reason before answering.

    ``feedback_value_type=bool`` matters: a free-text judge value cannot be aggregated
    into a pass rate, which makes it useless for tracking regressions.
    """
    return [
        make_judge(
            name=name,
            instructions=instructions,
            model=model or JUDGE_MODEL,
            description=description,
            feedback_value_type=bool,
            base_url=JUDGE_BASE_URL,
        )
        for name, (instructions, description) in _SPECS.items()
    ]
