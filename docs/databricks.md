# Running on Databricks Apps

The same workbench, hosted. Nothing in `core/` changes; what changes is where the three
external things come from.

| Local (`docker-compose`)            | Databricks App                                              |
|-------------------------------------|-------------------------------------------------------------|
| PostgreSQL + pgvector on :5432      | **Lakebase** instance `vrr-db` (PG 16, pgvector 0.8), OAuth credential minted per connection |
| Ollama `qwen2.5:7b` narrator        | Model Serving `databricks-claude-sonnet-4-5` via the OpenAI-compatible API |
| Ollama `nomic-embed-text` (768-dim) | Model Serving `databricks-gte-large-en` (1024-dim; `VRR_EMBED_DIM=1024`) |
| MLflow OSS on :5001                 | Managed MLflow, experiment `/Shared/vrr-agent-open`         |
| `.env` secrets                      | Secret scope `vrr` (`jwt_secret`, `demo_password`) as app resources |
| `make seed / queue / users / knowledge / guide` | `pipeline/bootstrap.py`, run once in a background thread at startup (`VRR_BOOTSTRAP=1`), skipping every step whose rows already exist |

Code that knows about the host: `src/vrr_agent_open/databricks_env.py` (DSN + tokens),
the `databricks` branch in `agent/providers.py`, `pipeline/knowledge_ingest.embed`, and
the `MLFLOW_TRACKING_URI=databricks` branch in `agent/tracing.py`. Everything else reads
`CFG.pg_dsn`, which is now a property so the Lakebase credential is refreshed before the
hour it lives for.

## Deploy / redeploy

```bash
cd web && npm ci && npm run build && cd ..          # the app does not build the SPA; dist/ ships
databricks sync . /Workspace/Users/<you>/vrr-app --full
databricks apps deploy vrr --source-code-path /Workspace/Users/<you>/vrr-app
```

Resources (attached once with `databricks apps update vrr --json …`): the Lakebase
database (`CAN_CONNECT_AND_CREATE`), the two serving endpoints (`CAN_QUERY`), and the two
secrets (`READ`). `app.yaml` holds every non-secret env var.

## Things to know

- **Bootstrap runs as the app's service principal**, so it owns every table. Watch it in
  `GET /api/health` → `bootstrap.state` / `bootstrap.steps`, or `databricks apps logs vrr`
  (needs an OAuth CLI login, not a PAT).
- **Tracing needs a permission**: grant the app service principal `CAN_MANAGE` on the
  `/Shared/vrr-agent-open` experiment (Experiments → Permissions). Until then
  `/api/health` reports tracing off and answers carry no trace link — nothing else breaks.
- **`VRR_RETRIEVAL_MIN_SCORE=0.55` is unmeasured** for `gte-large-en`; the 0.62 in the repo
  was measured for nomic. Run `scripts/calibrate_floor.py` against the live index and set it.
- **Sign-in is unchanged**: `analyst.demo` / `rm.demo` / `site.demo` / `steward.demo` with
  the password in the `vrr/demo_password` secret. Databricks SSO gates *reaching* the app;
  the app's own JWT still decides the approval role.
- Lakebase is billed while running. `databricks database update-database-instance vrr-db
  --stopped` pauses it; the app then shows an empty portfolio until it is started again.
