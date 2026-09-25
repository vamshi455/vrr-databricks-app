"""Environment config for vrr_agent_open (all-OSS, all-local).

Single source of truth for object names + connection settings. Nothing here has
side effects. The SAME code runs against a local docker-compose stack or any
Postgres/Unity-Catalog/MLflow endpoints — only env vars differ.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# A local `.env` (gitignored) is the only place API keys live — never a literal in code
# and never a committed file. Real environment variables always win over the file, so a
# CI or shell export still overrides it.
try:
    from dotenv import load_dotenv

    load_dotenv(override=False)
except ImportError:                       # dotenv is optional; env vars still work
    pass

# Governance / VRR domain constants (ported from the Databricks version).
DEFAULT_TARGET_VRR = 1.0
TARGET_BAND = (0.9, 1.1)          # green "on target" band (anomaly.py imports this)

# Catalog naming — mirrors the three-schema layout, now as Postgres schemas
# registered in Unity Catalog OSS as the governance catalog-of-record.
CATALOG = os.environ.get("VRR_CATALOG", "vrr")          # UC catalog name
RAW_SCHEMA = "vrr_raw"
CURATED_SCHEMA = "vrr_curated"
AGENT_SCHEMA = "vrr_agent"


@dataclass(frozen=True)
class Config:
    # --- PostgreSQL (data + compute + pgvector knowledge index) ---
    # Locally a static DSN. On Databricks the `database` app resource sets PGHOST/PGUSER/
    # PGDATABASE and the password is a short-lived OAuth credential, so the DSN is built
    # on demand by `databricks_env.pg_dsn()` — see the `pg_dsn` property below.
    pg_dsn_static: str = os.environ.get(
        "VRR_PG_DSN", "postgresql://vrr:vrr@localhost:5432/vrr")
    on_databricks: bool = bool(os.environ.get("PGHOST") and os.environ.get("DATABRICKS_HOST"))
    # --- Unity Catalog OSS (governance catalog-of-record) ---
    uc_url: str = os.environ.get("VRR_UC_URL", "http://localhost:8080")
    uc_catalog: str = CATALOG
    # --- Kafka (streaming simulator + ingestion; optional, off by default) ---
    # Nothing in the core pipeline reads these. The seed, the build, the workbench and
    # the agent all run exactly as before with no broker installed.
    kafka_bootstrap: str = os.environ.get("VRR_KAFKA_BOOTSTRAP", "localhost:9092")
    kafka_topic: str = os.environ.get("VRR_KAFKA_TOPIC", "vrr.volumes.daily.v1")
    # --- MLflow OSS (tracing / eval / registry) ---
    mlflow_uri: str = os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5000")
    # --- LLM narrator (pluggable; Ollama by default, fully local) ---
    # `databricks` = a Model Serving endpoint behind the OpenAI-compatible API; the app's
    # service principal authenticates, so no key is configured anywhere.
    llm_provider: str = os.environ.get("VRR_LLM_PROVIDER", "ollama")
    # qwen2.5:7b is the default because it follows Ollama's tool-calling schema
    # reliably; llama3.1 also works. agent.llm.pick_model() falls back to whatever
    # chat model is actually pulled, so a different local model still works.
    llm_model: str = os.environ.get("VRR_LLM_MODEL", "qwen2.5:7b")
    llm_base_url: str = os.environ.get("VRR_LLM_BASE_URL", "http://localhost:11434")
    # --- optional hosted providers (BILLABLE — off unless a key is set) ---
    # `VRR_LLM_PROVIDER=anthropic|openai` switches the narrator; the deterministic tools,
    # the physics and the faithfulness gate are unchanged by the choice.
    anthropic_api_key: str = os.environ.get("ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.environ.get("VRR_ANTHROPIC_MODEL", "claude-sonnet-5")
    openai_api_key: str = os.environ.get("OPENAI_API_KEY", "")
    openai_model: str = os.environ.get("VRR_OPENAI_MODEL", "gpt-4o-mini")
    databricks_model: str = os.environ.get("VRR_DATABRICKS_MODEL", "databricks-claude-sonnet-4-5")
    # --- embeddings for the pgvector knowledge index ---
    # Local `nomic-embed-text` is 768-dim, which is what schema.sql declares. Changing
    # this means changing that column, so it is deliberately separate from the narrator.
    embed_provider: str = os.environ.get("VRR_EMBED_PROVIDER", "ollama")
    embed_model: str = os.environ.get("VRR_EMBED_MODEL", "nomic-embed-text")
    # The width of the `embedding vector(N)` column. nomic-embed-text is 768; the
    # Databricks `databricks-gte-large-en` / `bge-large-en` endpoints are 1024. The
    # bootstrap substitutes this into schema.sql, so the two cannot silently disagree.
    embed_dim: int = int(os.environ.get("VRR_EMBED_DIM", "768"))
    # --- retrieval (RAG) ---
    # A chunk below this cosine similarity is NOISE, not an answer: without a floor the
    # top-k always returns k rows, so an unanswerable question still hands the model four
    # confident-looking excerpts. See `pipeline/knowledge_ingest.search`.
    #
    # 0.62 is MEASURED, not guessed — `make floor` scores answerable vs off-topic
    # questions against the live index. With nomic-embed-text, unrelated text still
    # scores ~0.40-0.56 (the embedder has a high similarity baseline), so an intuitive
    # 0.35 admits everything and the abstain path never fires. Re-run `make floor` after
    # changing the embedding model or materially changing the corpus.
    retrieval_min_score: float = float(os.environ.get("VRR_RETRIEVAL_MIN_SCORE", "0.62"))

    default_target_vrr: float = DEFAULT_TARGET_VRR

    @property
    def pg_dsn(self) -> str:
        """The connection string. On Databricks this carries a freshly minted (cached)
        Lakebase credential, so read it per connection rather than once at import."""
        if self.on_databricks:
            from . import databricks_env
            return databricks_env.pg_dsn()
        return self.pg_dsn_static

    def model_for(self, provider: str | None = None) -> str:
        """The model name this provider should use."""
        return {"anthropic": self.anthropic_model,
                "openai": self.openai_model,
                "databricks": self.databricks_model}.get(provider or self.llm_provider,
                                                         self.llm_model)

    def table(self, schema: str, name: str) -> str:
        return f"{schema}.{name}"          # Postgres schema-qualified


def load_config() -> Config:
    return Config()
