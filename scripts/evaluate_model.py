"""Score recent traces with the deterministic scorers and the LLM judges.

    python scripts/evaluate_model.py                 # last 50 traces
    python scripts/evaluate_model.py --eval-only     # only traces from create_traces.py
    python scripts/evaluate_model.py --no-judges     # deterministic only, no model needed

Produces an MLflow evaluation run: per-trace scores plus aggregates, so two runs can be
compared after a prompt or code change.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import mlflow                                              # noqa: E402
import mlflow.genai                                        # noqa: E402

from vrr_agent_open.agent.tracing import EXPERIMENT        # noqa: E402
from vrr_agent_open.config import load_config              # noqa: E402
from vrr_agent_open.evaluation import describe, get_scorers  # noqa: E402
from vrr_agent_open.prompts import PROMPTS, prompt_version   # noqa: E402

CFG = load_config()


def main(limit: int = 50, eval_only: bool = False, judges: bool = True) -> None:
    mlflow.set_tracking_uri(CFG.mlflow_uri)
    experiment = mlflow.get_experiment_by_name(EXPERIMENT)
    if experiment is None:
        print(f"no experiment named {EXPERIMENT!r} — run scripts/create_traces.py first.")
        return

    traces = mlflow.search_traces(
        locations=[experiment.experiment_id], max_results=limit, return_type="list",
        filter_string="tags.eval_case != ''" if eval_only else None)
    if not traces:
        print("no traces to score.")
        return

    scorers = get_scorers(include_judges=judges)
    print(f"scoring {len(traces)} trace(s)\n{describe(scorers)}")

    # Attribute the scores: every trace carries the model_id of the agent version that
    # produced it, so `mlflow.genai.evaluate` can tie this run to that version.
    model_ids = {t.info.trace_metadata.get("mlflow.modelId") for t in traces
                 if getattr(t.info, "trace_metadata", None)}
    model_id = next((m for m in model_ids if m), None)

    with mlflow.start_run(run_name="vrr-eval") as run:
        mlflow.log_params({f"prompt.{k}": prompt_version(k) for k in PROMPTS})
        if model_id:
            mlflow.log_param("model_id", model_id)
        result = mlflow.genai.evaluate(data=traces, scorers=scorers, model_id=model_id)
    print(f"\nrun: {run.info.run_id}  model_id: {model_id or 'unattributed'}")
    for metric, value in sorted((result.metrics or {}).items()):
        print(f"  {metric:<44} {value}")
    print(f"\nopen {CFG.mlflow_uri} → {EXPERIMENT} → Evaluations")


if __name__ == "__main__":
    main(eval_only="--eval-only" in sys.argv, judges="--no-judges" not in sys.argv)
