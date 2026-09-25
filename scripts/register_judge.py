"""Register the LLM judges with the MLflow server so the UI can show and schedule them.

Judges live in code (`vrr_agent_open.evaluation.custom_judges`); this pushes them to the
server. Automatic trace scoring stays OFF unless --start is passed, because online scoring
runs inside the MLflow server process — it needs the judge model reachable from there.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import mlflow                                              # noqa: E402
from mlflow.genai.scorers import ScorerSamplingConfig      # noqa: E402

from vrr_agent_open.agent.tracing import EXPERIMENT        # noqa: E402
from vrr_agent_open.config import load_config              # noqa: E402
from vrr_agent_open.evaluation import build_judges         # noqa: E402


def main(start: bool = False, sample_rate: float = 1.0) -> None:
    mlflow.set_tracking_uri(load_config().mlflow_uri)
    mlflow.set_experiment(EXPERIMENT)
    for judge in build_judges():
        registered = judge.register(name=judge.name)
        state = "registered"
        if start:
            registered.start(sampling_config=ScorerSamplingConfig(sample_rate=sample_rate))
            state = f"started (sample_rate={sample_rate})"
        print(f"  {judge.name:<24} {state}")
    if not start:
        print("\nautomatic scoring left OFF. To enable it the MLflow *server* needs the "
              "judge model reachable:\n"
              "  OPENAI_API_BASE=http://localhost:11434/v1 OPENAI_API_KEY=ollama \\\n"
              "  python -m mlflow server --backend-store-uri sqlite:///mlflow.db --port 5001\n"
              "then re-run with --start")


if __name__ == "__main__":
    main(start="--start" in sys.argv)
