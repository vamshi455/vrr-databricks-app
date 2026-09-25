"""Log the agent as an MLflow model and register a version.

Log-model-from-code: the artifact records the source file plus its dependencies, not a
pickle, so what gets evaluated is what is in git.

    python scripts/register_model.py                     # log + register a new version
    python scripts/register_model.py --champion          # also move the champion alias

The champion alias is deliberately opt-in: promotion should follow an evaluation run
(`make eval`), not merely a successful log.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import mlflow                                              # noqa: E402
from mlflow import MlflowClient                            # noqa: E402

from vrr_agent_open import __version__                     # noqa: E402
from vrr_agent_open.agent.tracing import EXPERIMENT        # noqa: E402
from vrr_agent_open.config import load_config              # noqa: E402
from vrr_agent_open.prompts import PROMPTS, prompt_version  # noqa: E402

MODEL_NAME = "vrr_agent_open"
CODE_PATH = ROOT / "src" / "vrr_agent_open" / "agent" / "agent_model.py"

# The Responses-API request shape, so the logged signature matches what a caller sends.
INPUT_EXAMPLE = {
    "input": [{"role": "user", "content": "Why is UNITY's VRR high in the latest period?"}],
    "metadata": {"pattern": "UNITY", "use_llm": "false"},
}


def main(promote: bool = False) -> None:
    cfg = load_config()
    mlflow.set_tracking_uri(cfg.mlflow_uri)
    mlflow.set_experiment(EXPERIMENT)

    with mlflow.start_run(run_name="register-agent") as run:
        mlflow.log_params({
            "package_version": __version__,
            "llm_model": cfg.llm_model,
            **{f"prompt.{name}": prompt_version(name) for name in PROMPTS},
        })
        info = mlflow.pyfunc.log_model(
            name=MODEL_NAME,
            python_model=str(CODE_PATH),
            code_paths=[str(ROOT / "src" / "vrr_agent_open")],
            input_example=INPUT_EXAMPLE,
            registered_model_name=MODEL_NAME,
            pip_requirements=["psycopg[binary]>=3.2", "pgvector>=0.3", "httpx>=0.27",
                              f"mlflow>={mlflow.__version__}"],
            metadata={"trust_model": "deterministic tools; LLM narrates behind a gate",
                      "package_version": __version__},
        )
    print(f"  logged   {info.model_uri}  (run {run.info.run_id})")

    client = MlflowClient()
    version = max(client.search_model_versions(f"name='{MODEL_NAME}'"),
                  key=lambda v: int(v.version))
    print(f"  version  {MODEL_NAME} v{version.version}")
    client.set_registered_model_alias(MODEL_NAME, "candidate", version.version)
    print(f"  alias    candidate → v{version.version}")
    if promote:
        client.set_registered_model_alias(MODEL_NAME, "champion", version.version)
        print(f"  alias    champion  → v{version.version}")
    else:
        print("\n  champion NOT moved. Run `make eval` first, then re-run with --champion.")
    print(f"\n  models:  {cfg.mlflow_uri}/#/models/{MODEL_NAME}")


if __name__ == "__main__":
    main(promote="--champion" in sys.argv)
