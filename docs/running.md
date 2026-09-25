# Running vrr_agent_open — every step, commented

Two tiers: **(A) logic only** — instant, no dependencies; **(B) full stack** — needs
Docker (and Ollama for the LLM). Everything is local and free.

## A. Logic only (no Docker, no LLM) — start here

```bash
cd vrr-agent-open

# Install the project as an EDITABLE package + dev tools (pytest, ruff).
#   -e     editable: links to the source, so edits apply instantly (no reinstall)
#   "."    install THIS folder's package (read from pyproject.toml)
#   [dev]  also install the optional dev extras
# After this, `import vrr_agent_open` works anywhere — no PYTHONPATH needed.
pip install -e ".[dev]"

# Run the unit tests. These cover ONLY the pure logic in core/ (physics, recommend,
# anomaly, knowledge, approval) — no DB, no LLM, no Docker. Fast, free, your inner loop.
pytest -q
```

## B. Full local stack

```bash
# Start 3 background containers (needs Docker Desktop running):
#   postgres     — PostgreSQL 16 + pgvector (VRR data + knowledge vector index)
#   unitycatalog — Unity Catalog OSS (governance catalog) on :8080
#   mlflow       — Machine Learning flow (MLflow) tracking server on host :5001
#                  (container still listens on 5000; published 5001:5000 because
#                  macOS AirPlay Receiver holds 5000)
# First run auto-applies schema.sql (creates vrr_raw / vrr_curated / vrr_agent schemas).
docker compose up -d
docker compose ps                 # confirm all three are "running"/healthy

# Load synthetic VRR data: generates raw data and computes the curated tables
# (completion_contrib → pattern_vrr_monthly) with core/physics. Postgres now has VRR.
# Idempotent (truncates + reloads), deterministic (fixed RNG seed), and loaded with COPY.
# Defaults to 40 patterns × 225 completions × 36 months (~247k volume rows, ~300k
# contribution rows, ~5 s). Scale it with VRR_SEED_PATTERNS / VRR_SEED_MONTHS.
# IDs are 16-char uppercase hex, like real source keys; patterns also carry a name.
# ~30% of producers feed 2–3 patterns (many-to-many allocation), including mid-life
# split changes and one migrating wholly between patterns. Three scripted scenarios:
#   UNITY    behaves for 2 years, then over-injects → VRR 1.00 → 1.36 (out_of_band + drift)
#   HORIZON  healthy — stays inside the [0.90, 1.10] band for its whole life (control)
#   MERIDIAN depletes past the bottom of its PVT range → any_extrapolated (suspect inputs)
make seed

# Rebuild ONLY vrr_curated from whatever is in vrr_raw (same core/physics path, new
# run_id). Use after editing raw data or the builder; no reseed, no agent-table writes.
make build

# See what the agent did: MLflow traces (tools called, LLM calls, gate verdict).
# Host port is always 5001 — compose publishes 5001:5000, and a local `mlflow server`
# should bind 5001 too, because macOS AirPlay Receiver holds 5000 and answers 403.
#   export MLFLOW_TRACKING_URI=http://localhost:5001
#   # if you are not using compose:
#   mlflow server --backend-store-uri sqlite:///mlflow.db --host 127.0.0.1 --port 5001
# Then open http://localhost:5001 → experiment "vrr-agent-open" → Traces.
# Tracing is optional: with no server reachable the agent runs identically, untraced.

# Register the vrr_* schemas/tables in Unity Catalog OSS as the catalog-of-record
# (governance metadata + RBAC + lineage). Postgres stays the engine.
make register

# Build the React UI and serve it from FastAPI at http://localhost:8000
#   🗺️ Portfolio tab   — every pattern's latest VRR vs target, ranked by drift
#   📈 Report tab       — VRR chart + date filter + target band + ΔVRR attribution,
#                         and the draft valve change for this period
#   🔎 Lineage tab      — raw → PVT → contrib → monthly, plus an independent recompute
#   ✅ Approval tab     — move drafts draft→analyst→rm→site→executed (core/approval)
#   💬 Analyst chat     — NOT a tab: a collapsible drawer docked on the RIGHT of whatever
#                         tab is open (💬/✕ to toggle). The transcript is stored in
#                         vrr_agent.chat_history, so it survives a refresh and is shared
#                         per pattern (deterministic tools; LLM phrasing when Ollama is up)
make app          # build web/ then serve it from FastAPI                          # Ctrl+C to stop

# (Optional) Ask the agent — needs a local LLM via Ollama:
ollama pull llama3.1              # narrator model (one-time)
ollama pull nomic-embed-text     # 768-dim embeddings for knowledge search
make agent                       # one question through the LangGraph tool loop + gate
                                 # traces stream to MLflow (:5001)

# (Optional) Ingest a PDF into the vector DB (pgvector):
mkdir -p knowledge_uploads && cp your_report.pdf knowledge_uploads/
python -m vrr_agent_open.pipeline.knowledge_ingest   # register → chunk+PII-redact→embed
# approve BEFORE it embeds (human relevance gate):
#   UPDATE vrr_agent.knowledge_registry SET status='approved' WHERE file_name='your_report.pdf';
```

## Teardown

```bash
docker compose down        # stop containers, KEEP data
docker compose down -v     # stop + DELETE data volumes (fresh start next time)
```

## Notes
- `make <target>` just runs the commented commands in the [Makefile](../Makefile).
- The `agent/graph.py` LLM+gate wiring and `agent/tools.py` `vrr_decompose` SQL are
  `TODO`-marked skeletons; the deterministic core and the PDF→pgvector path are complete.
- Ports in use: 5432 (Postgres), 8080 (Unity Catalog), 5001 (MLflow on the host;
  the compose container still listens on 5000), 8000 (FastAPI),
  5173 (Vite dev server, only during `make web`).
- Compose path that has been *documented*, not run end-to-end in this change:
  `make up` (or `docker compose up -d`) → `make seed` → `make queue` → `make app`.
  Set `MLFLOW_TRACKING_URI=http://localhost:5001`. Off-database unit tests run in
  GitHub Actions on pull requests and pushes to `main` (`.github/workflows/test.yml`).
