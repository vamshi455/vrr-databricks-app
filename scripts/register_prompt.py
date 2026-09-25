"""Push every prompt in `vrr_agent_open.prompts` into the MLflow Prompt Registry.

Versioning prompts separately from code is what lets an evaluation run be attributed to
the exact instruction text that produced it — and lets a prompt change ship without a
redeploy. Re-running is safe: MLflow creates a new version only when the text changed.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import mlflow                                            # noqa: E402
import mlflow.genai                                      # noqa: E402

from vrr_agent_open.config import load_config            # noqa: E402
from vrr_agent_open.prompts import PROMPTS, prompt_version  # noqa: E402

ALIAS = "production"


def main() -> None:
    mlflow.set_tracking_uri(load_config().mlflow_uri)
    for name, spec in PROMPTS.items():
        version = mlflow.genai.register_prompt(
            name=name, template=spec["template"],
            commit_message=spec["purpose"],
            tags={"content_hash": prompt_version(name), "project": "vrr-agent-open"})
        mlflow.genai.set_prompt_alias(name=name, alias=ALIAS, version=version.version)
        print(f"  {name:<22} v{version.version}  hash={prompt_version(name)}  alias={ALIAS}")


if __name__ == "__main__":
    main()
