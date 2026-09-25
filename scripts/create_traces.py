"""Run the agent over the evaluation question set, logging traces + ground truth.

One trace per question, with the authored expectations attached as MLflow expectations
tagged to their human source — so scorers and judges have reference data to compare
against instead of only judging the answer on its own terms.

    python scripts/create_traces.py                # all questions
    python scripts/create_traces.py --no-llm       # deterministic answers only (fast)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mlflow                                                       # noqa: E402
from mlflow.entities import AssessmentSource, AssessmentSourceType   # noqa: E402

from data.evaluation.vrr_questions import QUESTIONS, expectations_for  # noqa: E402
from vrr_agent_open.agent import chat as CH                            # noqa: E402
from vrr_agent_open.agent import tracing                               # noqa: E402
from vrr_agent_open.prompts import PROMPTS, prompt_version             # noqa: E402

SME = AssessmentSource(source_type=AssessmentSourceType.HUMAN, source_id="reservoir-sme")


MODEL_NAME = "vrr_agent_open"


def _bind_active_model() -> str | None:
    """Attach traces to the registered agent version, so a score has a subject.

    Without this, traces float free of any version: you can see that a run scored 0.8 but
    not what produced it. Silently skipped when the model has not been registered yet.
    """
    try:
        model = mlflow.set_active_model(name=MODEL_NAME)
        return getattr(model, "model_id", None)
    except Exception as exc:
        print(f"  (no active model bound: {exc}; run scripts/register_model.py)")
        return None


def main(use_llm: bool = True) -> None:
    if not tracing.enabled():
        print(f"! tracing is off ({tracing.status()}) — traces would not be recorded.")
        print("  Start MLflow and set MLFLOW_TRACKING_URI, then re-run.")
        return
    print(tracing.status())
    print("prompt versions:", {k: prompt_version(k) for k in PROMPTS})
    model_id = _bind_active_model()
    print("active model_id:", model_id)

    pending: list[tuple[str, dict, str]] = []
    for entry in QUESTIONS:
        with mlflow.start_span(name=f"eval:{entry['id']}", span_type="EVALUATOR") as span:
            span.set_inputs({"question": entry["question"], "case": entry["id"]})
            result = CH.respond(entry["question"], pattern=entry.get("pattern"),
                                date=entry.get("date"), use_llm=use_llm)
            span.set_outputs({"text": result["text"], "intent": result["intent"]})
            trace_id = span.trace_id

        pending.append((trace_id, expectations_for(entry), entry["id"]))
        print(f"  {entry['id']:<26} intent={result['intent']:<12} "
              f"gate={(result.get('meta') or {}).get('gate', 'n/a')}")

    # Spans export asynchronously, so the trace does not exist server-side the instant the
    # context manager closes. Flush first, then attach ground truth.
    mlflow.flush_trace_async_logging()
    attached = 0
    for trace_id, expectations, case in pending:
        for name, value in expectations.items():
            mlflow.log_expectation(trace_id=trace_id, name=name, value=value, source=SME,
                                   metadata={"case": case})
            attached += 1
        mlflow.set_trace_tag(trace_id, "eval_case", case)
    print(f"\nlogged {len(pending)} traces with {attached} expectation(s).")


if __name__ == "__main__":
    main(use_llm="--no-llm" not in sys.argv)
