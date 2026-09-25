# VRR Agent on Databricks — complete project walkthrough

> Written to be read by a person preparing to explain this project, and to be pasted into
> a chat assistant as context for questions. Every statement below reflects the code and
> the Databricks workspace as they were on 2026-09-25. Source: this repository.

---

## 1. The one-paragraph pitch

This is a **waterflood surveillance assistant for reservoir engineers**. It watches the
**Voidage Replacement Ratio (VRR)** of every injection pattern in a synthetic oil field,
explains why a VRR moved, recommends an injection change sized by physics, and routes
that recommendation through a **three-person human approval chain**. The rule that shapes
every design decision: **the LLM never does the arithmetic and never gets the last word.**
Every number is computed by deterministic Python with its source table attached; the
model only chooses which tool to call and phrases the result, and that phrasing is
checked against the tool output before a person sees it. The original project ran
fully local (Postgres, Ollama, MLflow OSS). What we did is port it, unchanged in its
trust model, to a **Databricks App** backed by **Lakebase**, **Model Serving** and
**managed MLflow**.

---

## 2. The domain, in plain terms

- An oil reservoir loses pressure as fluid is produced. Operators inject water to hold
  pressure up. That is a **waterflood**. A **pattern** is one injector plus the producers
  it sweeps toward.
- **VRR = reservoir barrels injected ÷ reservoir barrels produced**, both measured at
  reservoir conditions. Surface barrels shrink or swell on the way up, so a **PVT**
  conversion (formation volume factors Bo, Bw, Bg, Rs) is applied first.
- Target is 1.0, with a healthy band of 0.90 to 1.10.
  - Below 1.0: under-injecting, pressure falls, oil is lost permanently.
  - Above 1.0: over-injecting, water short-circuits to producers, you pay to lift and
    separate water you did not need.
- Why an LLM is dangerous here: the output is a **valve change on a real well**. A
  hallucinated "cut injection 12%" is a pressure decline nobody notices for a quarter.

---

## 3. Architecture at a glance

```
Browser ──SSO──▶ Databricks Apps proxy ──▶ one container: React SPA + FastAPI
                                                │
                     ┌──────────────────────────┼───────────────────────────┐
                     ▼                          ▼                           ▼
             Lakebase vrr-db          Model Serving endpoints        Managed MLflow
      (Postgres 16 + pgvector)   claude-sonnet-4-5 / gte-large-en   /Shared/vrr-agent-open
      vrr_raw · vrr_curated · vrr_agent
```

Inside the container, three layers:

| Layer | Directory | What it does | Trusts the LLM? |
|---|---|---|---|
| Deterministic core | `src/vrr_agent_open/core/` | physics, LMDI decomposition, anomaly rules, input audit, recommendation sizing, faithfulness gate, approval state machine, pattern layout, help topics | never touched by it |
| Agent | `src/vrr_agent_open/agent/` | 16 tools over Postgres, intent router, analyst pipeline, LangGraph StateGraph, provider adapters, tracing | LLM picks tools and phrases |
| API + UI | `src/vrr_agent_open/api/`, `web/` | 30+ FastAPI endpoints, JWT auth, role checks, rate limits; React + Vite + Tailwind workbench | no |

Supporting: `pipeline/` (seed, build, ingest, bootstrap), `evaluation/` (scorers, judges),
`prompts/`, `streaming/` (optional Kafka simulator, unused on Databricks).

---

## 4. Every module, what it owns

### 4.1 `core/` (pure Python, no I/O, unit-tested off-database)

| File | Responsibility | Key idea |
|---|---|---|
| `physics.py` | PVT lookup ladder (exact → interpolate → extrapolate → closest), completion contribution, aggregation to pattern VRR | the lookup **method** is recorded on every row so an extrapolated input can be flagged later |
| `decompose.py` | exact LMDI (log-mean Divisia) attribution of ΔVRR into drivers (oil, water, gas, injection) | the terms sum exactly to the change; nothing is left "unexplained" |
| `anomaly.py` | rules: out_of_band, sustained_drift, extrapolated_pvt | rules fire, the model never decides |
| `audit.py` | verdict per period: `REAL_SIGNAL` vs `DATA_ARTIFACT` | a suspect input **vetoes** any recommendation and routes to a data steward |
| `recommend.py` | change magnitude from physics and the learned response factor ρ, clamped by per-injector safety limits; EMA update of ρ from executed outcomes | the closed learning loop |
| `faithfulness.py` | **the gate**: checks the narration cites only tool-sourced numbers, names only real drivers, gets the direction right | rejected text is replaced by the computed attribution |
| `approval.py` | state machine draft → analyst → rm → site → executed, plus rejected | policy lives here, the API only enforces it |
| `knowledge.py` | chunking helpers and PII detection/redaction | PII never reaches the vector index |
| `pattern_layout.py` | computes a well-pattern schematic (five-spot, seven-spot, line drive…) from contribution factors | positions are allocation shares, not coordinates, and the UI says so |
| `architecture.py` | the system's own self-diagram with live counters | unit tests assert it matches the code |
| `help_topics.py` | written answers about the application itself | fabricated UI passes every numeric check, so app help is never generated |
| `status.py`, `ids.py`, `upload_validation.py` | status sentence, opaque IDs, upload allowlist/magic bytes/zip-bomb/traversal checks | |

### 4.2 `agent/`

| File | Responsibility |
|---|---|
| `tools.py` | 16 deterministic tools (list patterns, overview, trend, VRR_DECOMPOSE, VRR_AUDIT recompute, VRR_LINEAGE, PATTERN_LAYOUT, SEARCH_KNOWLEDGE, RECOMMEND_CHANGE, submit draft, …). Every payload carries provenance keys: `sources`, `run_id`, `pvt_methods`, `formulas`. Same code path serves the browser and the model, so a chart and an answer cannot disagree. |
| `chat.py` | keyword intent router (12 intents). Default path is the deterministic `analyst.py` pipeline (~8 s); `agentic=true` lets the model drive the tool loop. **Both paths are gated.** RAG path abstains ("I don't know") below a similarity floor without calling the model. |
| `analyst.py` | five fixed steps: verify → attribute → classify → propose → draft |
| `graph.py` | a real LangGraph `StateGraph`: nodes plan / tools / gate / repair / budget; append-only reducers on `messages`, `trace`, `facts`; gate on every path to END except budget exhaustion; one repair attempt with tools withheld |
| `llm.py` + `providers.py` | one wire format (OpenAI-style messages + tools); providers ollama, openai, anthropic, **databricks** (added by us) |
| `tracing.py` | MLflow spans, optional and quiet; RETRIEVER spans for the vector search |
| `history.py`, `runtime.py` | shared chat transcript per pattern; the health probe |

### 4.3 `api/`

| File | Responsibility |
|---|---|
| `main.py` | FastAPI app, CORS, routers, `/api/health`, serves `web/dist`, **starts the bootstrap thread on Databricks** (added by us) |
| `auth.py` | OAuth2 password grant → HS256 JWT `{sub, role, iat, exp}` (12 h). bcrypt hashes in `vrr_agent.app_user`. `current_user`, `optional_user`, `require_role` dependencies. |
| `routes_auth.py` | `POST /api/auth/token`, `GET /api/auth/me` |
| `routes_patterns.py` | reads (open) + `POST /patterns/{id}/submit` (bearer) |
| `routes_approvals.py` | `/board`, `/queue`, `/adjustments` (open); `/queue/{id}/advance` and `/reject` check the **token role** against `APPROVER_FOR_STAGE = {draft: analyst, analyst: rm, rm: site, site: site}`. Executing writes `adjustment_history` before moving the stage. |
| `routes_chat.py` | `POST /chat` (bearer, rate-limited), history, per-user clear |
| `routes_knowledge.py` | upload (data_steward/admin) → quarantine → preview → approve (embeds in that request) / reject / delete |
| `routes_architecture.py` | live self-diagram |
| `ratelimit.py` | fixed windows per user: chat 20/min, agentic 5/5 min, upload 10/10 min, review 60/10 min |
| `share.py` | optional "share mode" that closes reads behind the token (not used on Databricks) |
| `db.py`, `schemas.py` | raw SQL for UI-only queries; pydantic request models |

### 4.4 `pipeline/`

| File | Responsibility |
|---|---|
| `schema.sql` | three Postgres schemas. `vrr_raw` (field-shaped: volumes by completion, time-windowed contribution factors and pressure, PVT tests, targets), `vrr_curated` (completion_contrib, pattern_vrr daily+monthly, cumulative), `vrr_agent` (pattern_memory with ρ, input_audit, action_queue, adjustment_history, safety_limits, reservoir_knowledge with `embedding vector(N)`, knowledge_registry, chat_history, app_user) |
| `seed.py` | reproducible synthetic field (seed 20260724): 40 patterns × 36 months, three scripted patterns (UNITY over-injection, HORIZON healthy, MERIDIAN extrapolated PVT) |
| `build.py` | raw → curated using `core.physics`, streamed with COPY |
| `input_audit.py`, `anomaly_to_queue.py` | verdicts per period; drafts into the queue |
| `knowledge_ingest.py` | register → approve → load → chunk → redact → embed → insert; `search()` with a similarity floor and a `doc_kind` corpus filter |
| `document_loaders.py`, `text_splitters.py` | pdf/txt/md/html/docx/csv loaders; fixed vs recursive vs semantic splitters scored by recall@k |
| `outcome_writeback.py` | fills `actual_post_vrr` from the next month and EMA-updates ρ |
| **`bootstrap.py`** (added by us) | idempotent first-run: schema → seed → input audit → queue → users → knowledge → guide |

### 4.5 `web/`

React 18 + Vite + TypeScript + Tailwind. Views: Portfolio, Report (chart + attribution +
schematic + draft), Lineage & audit (six-column derivation DAG), Approvals (swim-lane
board with drag-and-drop), Knowledge (upload + review), Architecture (live map), plus a
docked chat drawer. `src/api.ts` is the only place the UI talks to the backend. The
built bundle in `web/dist` is committed because Databricks Apps does not run `npm`.

---

## 5. What we changed to run on Databricks

Goal: same app, same trust model, zero changes to `core/`. Six touch points.

| Concern | Local | Databricks | Where |
|---|---|---|---|
| Database | Postgres 16 + pgvector on :5432, static DSN | **Lakebase** instance `vrr-db` (managed Postgres 16, pgvector 0.8). No password: the app's service principal mints an OAuth credential per connection, cached 45 min | `databricks_env.py` (new), `config.py` (`pg_dsn` became a property) |
| Narrator | Ollama `qwen2.5:7b` | Model Serving `databricks-claude-sonnet-4-5` via the OpenAI-compatible `/serving-endpoints` API, tool calling included | `agent/providers.py` (`databricks` provider reusing the OpenAI translation) |
| Embeddings | `nomic-embed-text` 768-dim | `databricks-gte-large-en` 1024-dim; `VRR_EMBED_DIM` substituted into `vector(N)` at bootstrap; a width mismatch raises | `pipeline/knowledge_ingest.embed`, `config.embed_dim`, `bootstrap.apply_schema` |
| Tracing | MLflow OSS server on :5001 | `MLFLOW_TRACKING_URI=databricks`, experiment `/Shared/vrr-agent-open`; probe skips the HTTP health check | `agent/tracing.py` |
| Secrets | `.env` | secret scope `vrr` (`jwt_secret`, `demo_password`) injected via app resources | `app.yaml` `valueFrom` |
| First run | `make seed/queue/users/knowledge/guide` by hand | `pipeline/bootstrap.py` in a background thread at startup (`VRR_BOOTSTRAP=1`), each step skipped if its rows exist | `api/main.py` startup hook, `/api/health.bootstrap` |

Files added: `databricks_env.py`, `pipeline/bootstrap.py`, `app.yaml`, `requirements.txt`,
`docs/databricks.md`, this file. `.gitignore` changed to ship `web/dist`.

### Why these choices

- **Lakebase instead of Delta tables.** The app is pure SQL over Postgres with pgvector
  and COPY loads. Rewriting 800 lines of tool SQL for Spark SQL and replacing pgvector
  with Vector Search would have changed the thing being ported. Lakebase is Postgres, so
  the port is an adapter, not a rewrite.
- **Service principal owns the tables.** The bootstrap runs inside the app, so the app's
  identity creates every schema and table. No cross-role GRANTs, no laptop in the loop.
  The one privileged step, `CREATE EXTENSION vector`, was run once by the workspace user.
- **Bootstrap in a thread.** Databricks Apps needs the port bound quickly. Seeding
  272,880 rows takes a couple of minutes, so uvicorn starts first and health reports
  bootstrap state.
- **Keep the app's own login.** Workspace SSO proves you may reach the app; it does not
  say whether you are the site engineer allowed to execute. The approval role stays a
  signed JWT claim, exactly as in the source project.
- **OpenAI-compatible endpoint, not the Databricks SDK chat API.** The existing OpenAI
  translation (tool_call ids, tool messages) works unchanged; only base URL and token
  differ. Verified live: the model returned a `VRR_DECOMPOSE` tool call.

---

## 6. Authentication, authorization, roles

Two layers, two questions:

1. **Platform (Databricks Apps proxy).** Workspace SSO (Entra ID). OAuth only; a
   personal access token is rejected, which is why the app URL and `databricks apps logs`
   need `databricks auth login`. Identity headers `X-Forwarded-Email` are available but
   the app does not derive a role from them.
2. **Workbench (FastAPI).** `POST /api/auth/token` with username + password
   (form-encoded). bcrypt check against `vrr_agent.app_user`; same `None` for unknown
   user and wrong password so accounts cannot be enumerated. Returns a JWT signed with
   the `vrr/jwt_secret` secret. The browser keeps it in localStorage and sends
   `Authorization: Bearer`.

Roles (DB check constraint): `analyst`, `rm`, `site`, `data_steward`, `admin`.

| Action | Who |
|---|---|
| Read every view | anyone past SSO, no token |
| Ask the agent, draft a change | any signed-in role |
| Advance draft → analyst | analyst |
| Advance analyst → rm | rm |
| Advance rm → site, and site → executed | site (the only role that executes) |
| Reject a card | the role that owns its current stage |
| Upload, preview, approve, reject, delete knowledge | data_steward, admin |

Failure codes: 401 missing/expired/tampered token, 403 wrong role for the stage, 409
terminal stage, 429 over budget with a truthful `Retry-After`.

The LLM's own "permissions": may pick tools, phrase results, answer general theory
(labelled). May never produce a figure, choose a magnitude, judge an input trustworthy,
or advance an approval. The approval routes are not tools; they import `core.approval`
directly.

Platform identity: service principal `app-68vha4 vrr` (client id `0bf8a4dc-…`) with
exactly five resources: Lakebase (`CAN_CONNECT_AND_CREATE`), two endpoints
(`CAN_QUERY`), two secrets (`READ`). The experiment grant (`CAN_MANAGE`) is still to be
done by hand; tracing reports off until then.

Demo accounts: `analyst.demo`, `rm.demo`, `site.demo`, `steward.demo`; one shared
password stored only in the `vrr/demo_password` secret.

---

## 7. How a question is answered (request walkthrough)

1. Browser `POST /api/chat {question, pattern, date, agentic}` with bearer token.
2. `ratelimit.hit("chat", user)`; agentic adds a second budget.
3. `chat.respond()` routes intent: status and help are answered from code without a
   model; knowledge questions embed the query, run `embedding <=> query` in pgvector,
   and **abstain** if the best score is below the floor; analysis questions run
   `analyst.analyze()` (default) or `graph.run()` (agentic).
4. Tools read `vrr_curated` (and `vrr_raw` for the audit recompute); every returned
   number is harvested into `facts`.
5. The model phrases the result. `core.faithfulness.check` verifies drivers, direction
   and numbers against the tool output; a failure triggers one repair with tools
   withheld, then the computed attribution is shown instead.
6. The turn is logged to `vrr_agent.chat_history` with `asked_by` from the token and the
   MLflow trace id, and returned with a `meta` block (model, gate result, tools called,
   violations) that the drawer renders as provenance.

---

## 8. How a VRR number is built

1. For the period and completion, look up PVT at the pattern pressure: exact match,
   else interpolate between tests, else extrapolate, else closest. Record the method.
2. `oil_res = FACTOR · OIL · Bo`, `water_res = FACTOR · WATER · Bw`,
   `free_gas_res = FACTOR · (GAS·1000 − Rs·OIL) · Bg`, `water_inj_res = FACTOR · WATER_INJ · Bw_inj`,
   `gas_inj_res = FACTOR · GAS_INJ·1000 · Bg_inj`.
3. Sum per pattern × period. `VRR = Σ injection ÷ Σ production`.
4. Cumulative VRR is a running sum of reservoir volumes, never the average of ratios.
5. The Lineage view re-runs this on raw rows at request time and diffs against the
   stored curated figure.

---

## 9. Deployment steps we actually ran

```bash
# 1. clone the reference project, python 3.12 venv, npm build
git clone https://github.com/vamshi455/vrr-agent-open
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]" databricks-sdk openai
cd web && npm ci && npm run build && cd ..

# 2. platform resources
databricks database create-database-instance vrr-db --capacity CU_1
databricks secrets create-scope vrr
databricks secrets put-secret vrr jwt_secret --string-value <random 48 bytes>
databricks secrets put-secret vrr demo_password --string-value <random>
databricks experiments create-experiment /Shared/vrr-agent-open
# as the workspace user, once:  CREATE EXTENSION IF NOT EXISTS vector;

# 3. attach resources to the app
databricks apps update vrr --json '{"resources":[ database, llm, embedder, jwt-secret, demo-password ]}'

# 4. ship
databricks sync . /Workspace/Users/<me>/vrr-app --full
databricks apps deploy vrr --source-code-path /Workspace/Users/<me>/vrr-app

# 5. pause when idle
databricks apps stop vrr
databricks database update-database-instance vrr-db stopped --stopped
```

Verification after deploy (read from Lakebase): 40 patterns, 220,296 raw volume rows,
272,880 contribution rows, 45,280 pattern VRR rows, 40 input audits, 10 queued drafts,
4 accounts, 3 reservoir PDFs (28 chunks), 6 guide pages (43 chunks), embedding column
width 1024. Test suite: 425 passed, 1 skipped.

---

## 10. Things that went wrong or are still open

- **Experiment permission grant blocked.** The automated grant of `CAN_MANAGE` to the
  service principal was refused by the tooling's permission policy. Do it in the UI.
  Effect: tracing off, no trace links, nothing else affected.
- **Could not open the app URL from the CLI.** PAT auth is rejected by the Apps proxy.
  Verified through database state instead.
- **Retrieval floor unmeasured.** 0.62 was measured for nomic; 0.55 is a placeholder for
  gte-large-en. `scripts/calibrate_floor.py` measures it.
- **Identity not unified.** SSO and the workbench login are separate. Mapping
  `X-Forwarded-Email` to `app_user` would remove the second sign-in.
- **Rate limits are per process** and the **JWT lives in localStorage**; both are
  acceptable for a single-container demo and documented as upgrade points.
- **Data is synthetic.** Meridian Petroleum is fictional; nothing real is in the repo.

---

## 11. Likely interview questions, with the short answer

- *Why not let the model compute?* Because the output is a valve change; a wrong number
  is discovered a quarter later. The model's value is choosing what to look at and
  explaining it; the arithmetic is cheap to make deterministic.
- *How do you stop hallucinated numbers?* Every tool result's numbers are harvested; the
  narration is checked for uncited decimals, unsupported drivers and wrong direction.
  Failures are repaired once with tools withheld, then replaced by the computed text.
  Limits are stated: integers and numbers in words are not decimal-checked.
- *Why LangGraph?* Reducers make the evidence trail append-only, the gate is an edge on
  every path to END, the checkpointer allows resume, and `max_steps` plus
  `recursion_limit` stop runaway loops.
- *Why Lakebase and not Delta?* Postgres semantics (COPY, pgvector, row-level updates in
  the approval flow) are what the app needs; Lakebase gives that as a managed Databricks
  service with OAuth credentials and no password.
- *How is the role enforced?* As a signed JWT claim checked server-side against the
  item's current stage. An earlier version read the role from the request body, which
  let the caller pick it; that is the hole this closes.
- *What runs as what identity?* People: SSO then app login. Container: one service
  principal with five explicit resource grants.
- *What is ρ?* The learned per-pattern response factor, EMA-updated from executed
  adjustments' actual vs predicted outcomes, feeding the next recommendation's size.
- *What does "abstain" mean in RAG?* Below a measured cosine similarity floor the top-k
  is discarded and the answer is "I don't know" without calling the model.
- *How is knowledge kept safe?* Upload is quarantined and never embedded; a data
  steward reads PII-redacted extracted text and approves; only then is it chunked,
  redacted and embedded. Corpora (`reservoir` vs `app_help`) are never searched together.
- *What would you do next?* Grant the experiment permission, calibrate the floor, map
  SSO identity to roles, move rate limits to a shared store, consider syncing curated
  tables to Unity Catalog for governance.

---

## 12. Glossary

VRR (voidage replacement ratio) · PVT (pressure-volume-temperature; Bo/Bw/Bg formation
volume factors, Rs solution gas) · FACTOR (completion-to-pattern allocation share,
time-windowed) · LMDI (log-mean Divisia index decomposition) · ρ (learned response
factor) · DATA_ARTIFACT / REAL_SIGNAL (input audit verdicts) · faithfulness gate ·
Lakebase (Databricks managed Postgres) · Model Serving (Databricks endpoint hosting) ·
service principal (non-human workspace identity) · JWT HS256 (shared-secret signed token)
