-- vrr_agent_open — PostgreSQL schema, aligned to the production VRR data model
-- (CreateVRR/src/vrr_sql_builder.sql). See docs/vrr_data_model.md for the mapping from
-- the RMDE/TRUSTED_DB source tables to these local ones, column by column.
--
-- Structure that matters (and that the first cut of this port got wrong):
--   * daily volumes are keyed by COMPLETION + DATE only — a completion has no pattern
--     of its own. Pattern membership comes from PATTERN_CONTRIBUTION_FACTOR, which is
--     TIME-WINDOWED (effect_date → next effect_date), so one completion can belong to
--     several patterns with different FACTORs over time.
--   * pattern pressure is likewise time-windowed (a reading holds until the next one).
--   * PVT is a set of lab tests per completion (test_date + pressure); the FVFs used on
--     a given day are interpolated by PRESSURE within the applicable test_date window.
--   * Amount_Type is DERIVED, not stored: Production when OIL+WATER+GAS > 0.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA IF NOT EXISTS vrr_raw;
CREATE SCHEMA IF NOT EXISTS vrr_curated;
CREATE SCHEMA IF NOT EXISTS vrr_agent;

-- ===========================================================================
-- raw — source-shaped (local stand-ins for TRUSTED_DB / ENRICHMENT_DB.RMDE_*)
-- ===========================================================================

-- ← TRUSTED_DB.PRODUCTION_VOLUME.PRODUCTION_VOLUMES_DAILY_OILFIELD
-- Allocated daily volumes per COMPLETION (no pattern column — see header note).
-- OilField uom: oil/water in STB, gas in KSCF. The _METRIC variant (SM3) is the same
-- shape with different units; `uom` records which convention a row is in.
CREATE TABLE IF NOT EXISTS vrr_raw.production_volumes_daily (
  id_completion text NOT NULL,
  prod_date date NOT NULL,
  alloc_oil_vol_stb double precision,
  alloc_water_vol_stb double precision,
  alloc_gas_vol_kscf double precision,
  alloc_water_inj_vol_stb double precision,
  alloc_gas_inj_vol_kscf double precision,
  uom text DEFAULT 'OilField',
  PRIMARY KEY (id_completion, prod_date)
);

-- ← {source_schema}.PATTERN — the pattern registry.
-- IDs are 16-char uppercase hex, like the production surrogate keys (core/ids.py mints
-- them); the CHECK keeps malformed keys out at the boundary instead of failing later.
CREATE TABLE IF NOT EXISTS vrr_raw.pattern (
  id_pattern text PRIMARY KEY CHECK (id_pattern ~ '^[0-9A-F]{16}$'),
  pattern_name text,
  asset text
);

-- Completion registry (a real ingestion carries one; volumes reference it).
CREATE TABLE IF NOT EXISTS vrr_raw.completion (
  id_completion text PRIMARY KEY CHECK (id_completion ~ '^[0-9A-F]{16}$'),
  completion_name text,
  uwi text,
  asset text,
  completion_type text                       -- producer | injector | dual (as designed)
);

-- ← {source_schema}.PATTERN_CONTRIBUTION_FACTOR — completion→pattern allocation.
-- effect_date opens a window that closes at the next effect_date for the same
-- (completion, pattern); end_date is derived with LEAD at build time, not stored.
CREATE TABLE IF NOT EXISTS vrr_raw.pattern_contribution_factor (
  id_completion text NOT NULL,
  id_pattern text NOT NULL,
  factor double precision,
  effect_date date NOT NULL,
  PRIMARY KEY (id_completion, id_pattern, effect_date)
);

-- ← {source_schema}.PATTERN_PRESSURE — pattern datum pressure, time-windowed.
CREATE TABLE IF NOT EXISTS vrr_raw.pattern_pressure (
  id_pattern text NOT NULL,
  pressure_date date NOT NULL,
  pressure double precision,
  PRIMARY KEY (id_pattern, pressure_date)
);

-- ← {source_schema}.COMPLETION_PVT_CHARACTERISTICS — lab PVT, interpolated by pressure.
-- Column comments give the production names.
CREATE TABLE IF NOT EXISTS vrr_raw.completion_pvt_characteristics (
  id_completion text NOT NULL,
  test_date date NOT NULL,
  pressure double precision NOT NULL,        -- PRESSURE
  bo double precision,                       -- OIL_FORMATION_VOLUME_FACTOR
  bg double precision,                       -- GAS_FORMATION_VOLUME_FACTOR
  bw double precision,                       -- WATER_FORMATION_VOLUME_FACTOR
  bg_inj double precision,                   -- INJECTED_GAS_FORMATION_VOLUME_FACTOR
  bw_inj double precision,                   -- INJECTED_WATER_FORMATION_VOLUME_FACTOR
  rs double precision,                       -- SOLUTION_GAS_OIL_RATIO
  rv double precision,                       -- VOLATIZED_OIL_GAS_RATIO
  PRIMARY KEY (id_completion, test_date, pressure)
);

CREATE INDEX IF NOT EXISTS production_volumes_daily_date_idx
  ON vrr_raw.production_volumes_daily (prod_date);
CREATE INDEX IF NOT EXISTS pattern_contribution_factor_completion_idx
  ON vrr_raw.pattern_contribution_factor (id_completion);

-- Local addition (the production model carries targets elsewhere): per-pattern target.
CREATE TABLE IF NOT EXISTS vrr_raw.pattern_target (
  id_pattern text PRIMARY KEY,
  target_vrr double precision
);

-- ===========================================================================
-- curated — the lineage layer + the DAILY/MONTHLY pattern VRR outputs
-- ===========================================================================

-- ≈ the VolumeWithPVT checkpoint, materialised: one row per
-- (pattern, completion, day) carrying EVERY root input, the resolved factor window,
-- the pressure in force, the PVT method + the exact FVFs used, and every derived
-- reservoir volume. This is what makes a VRR number auditable row by row.
CREATE TABLE IF NOT EXISTS vrr_curated.completion_contrib (
  id_pattern text NOT NULL,
  pattern_name text,
  id_completion text NOT NULL,
  vrr_date date NOT NULL,
  amount_type text,                          -- DERIVED: Production | Injection
  -- resolved allocation + pressure windows
  factor double precision,
  factor_effect_date date,
  pattern_pressure_psia double precision,
  -- surface volumes (root inputs, copied so the row stands alone)
  oil_volume_bbl double precision,
  water_volume_bbl double precision,
  gas_volume_kscf double precision,
  water_inj_volume_bbl double precision,
  gas_inj_volume_kscf double precision,
  -- PVT actually applied + how it was obtained
  pvt_method text,                           -- exact|interpolated|extrapolated|closest|none
  pvt_test_date date,
  bo double precision, bw double precision, bg double precision,
  bg_inj double precision, bw_inj double precision,
  rs double precision, rv double precision,
  -- derived reservoir volumes (core/physics.py)
  res_oil_volume_bbl double precision,
  res_water_volume_bbl double precision,
  res_free_gas_volume_bbl double precision,  -- NULL for non-producers; may be negative
  res_water_inj_volume_bbl double precision,
  res_gas_inj_volume_bbl double precision,
  run_id text,
  built_at timestamptz DEFAULT now(),
  PRIMARY KEY (id_pattern, id_completion, vrr_date)
);
CREATE INDEX IF NOT EXISTS completion_contrib_pattern_date_idx
  ON vrr_curated.completion_contrib (id_pattern, vrr_date);

-- ← DAILY_PATTERN_VRR / MONTHLY_PATTERN_VRR (OilField/BBL variant).
-- Same column set at both grains, so one table + a `grain` column instead of two.
CREATE TABLE IF NOT EXISTS vrr_curated.pattern_vrr (
  id_alias text,                             -- CONCAT(id_pattern, id_fmt)
  id_pattern text NOT NULL,
  pattern_name text,
  vrr_date date NOT NULL,
  grain text NOT NULL,                       -- 'daily' | 'monthly'
  pattern_pressure_psia double precision,
  -- surface totals
  oil_volume_bbl double precision,
  water_volume_bbl double precision,
  water_inj_volume_bbl double precision,
  gas_volume_kscf double precision,
  gas_inj_volume_kscf double precision,
  -- reservoir totals
  res_oil_volume_bbl double precision,
  res_water_volume_bbl double precision,
  res_free_gas_volume_bbl double precision,
  res_water_inj_volume_bbl double precision,
  res_gas_inj_volume_bbl double precision,
  res_production_volume_bbl double precision,
  res_injection_volume_bbl double precision,
  vrr_bblbbl double precision,               -- COALESCE(inj/NULLIF(prod,0), 0)
  -- volume-weighted average PVT actually applied
  avg_oil_fvf double precision, avg_water_fvf double precision, avg_gas_fvf double precision,
  avg_inj_water_fvf double precision, avg_inj_gas_fvf double precision,
  avg_solution_gas_oil_ratio double precision, avg_volatized_oil_gas_ratio double precision,
  -- local additions used by the agent
  n_completions int,
  any_extrapolated boolean,                  -- confidence flag (low-confidence PVT)
  run_id text,
  execution_time timestamptz DEFAULT now(),
  PRIMARY KEY (id_pattern, vrr_date, grain)
);

-- ← vrr_cumulative_calculator.sql — running totals per pattern by date.
CREATE INDEX IF NOT EXISTS pattern_vrr_grain_date_idx
  ON vrr_curated.pattern_vrr (grain, vrr_date);

CREATE TABLE IF NOT EXISTS vrr_curated.pattern_vrr_cumulative (
  id_pattern text NOT NULL,
  pattern_name text,
  vrr_date date NOT NULL,
  grain text NOT NULL,
  res_cumulative_oil_production_volume_bbl double precision,
  res_cumulative_water_production_volume_bbl double precision,
  res_cumulative_water_injection_volume_bbl double precision,
  res_cumulative_gas_injection_volume_bbl double precision,
  res_cumulative_production_volume_bbl double precision,
  res_cumulative_injection_volume_bbl double precision,
  cumulative_vrr_bblbbl double precision,
  run_id text,
  execution_time timestamptz DEFAULT now(),
  PRIMARY KEY (id_pattern, vrr_date, grain)
);

-- ===========================================================================
-- agent — memory + queue + knowledge (unchanged by the data-model correction)
-- ===========================================================================
CREATE TABLE IF NOT EXISTS vrr_agent.pattern_memory (
  id_pattern text PRIMARY KEY, pattern_name text,
  latest_vrr double precision, latest_date date,
  typical_low double precision, typical_high double precision,
  response_factor double precision DEFAULT 1.0, n_adjustments int DEFAULT 0,
  tendencies text, updated_at timestamptz
);
-- Input-audit verdicts (parent Slice A): is a flagged VRR a real reservoir signal or a
-- data artifact? Written by pipeline/input_audit.py after each build; read by the agent
-- before it is allowed to propose a valve change.
CREATE TABLE IF NOT EXISTS vrr_agent.input_audit (
  id_pattern text NOT NULL,
  pattern_name text,
  vrr_date date NOT NULL,
  verdict text NOT NULL,                     -- DATA_ARTIFACT | INCONCLUSIVE | REAL_SIGNAL
  actionable boolean,
  summary text,
  findings jsonb,
  run_id text,
  audited_at timestamptz DEFAULT now(),
  PRIMARY KEY (id_pattern, vrr_date)
);

CREATE TABLE IF NOT EXISTS vrr_agent.adjustment_history (
  action_id text, id_pattern text, pattern_name text, vrr_date date, driver text,
  anomaly text, change_type text, d_inj_res_bbl double precision, d_surface_pct double precision,
  pre_vrr double precision, predicted_post_vrr double precision, actual_post_vrr double precision,
  decision text, approved_by text, outcome text, ts timestamptz DEFAULT now()
);
CREATE TABLE IF NOT EXISTS vrr_agent.safety_limits (
  id_pattern text, id_completion text, max_inj_rate_change_pct double precision,
  max_inj_pressure double precision, fracture_gradient double precision, note text
);
CREATE TABLE IF NOT EXISTS vrr_agent.action_queue (
  action_id text PRIMARY KEY, id_pattern text, pattern_name text, vrr_date date,
  anomaly_kind text, severity text, anomaly_detail text, driver text, action_type text,
  recommendation jsonb, precedent jsonb, confidence text, narrative text,
  stage text DEFAULT 'draft', stage_by text, stage_ts timestamptz,
  run_id text, created_at timestamptz DEFAULT now()
);
-- semantic memory: PII-redacted chunks + pgvector embedding (the knowledge index)
CREATE TABLE IF NOT EXISTS vrr_agent.reservoir_knowledge (
  chunk_id text PRIMARY KEY, doc_id text, file_name text, page int, chunk_seq int,
  text text, pii_redacted boolean, embedding vector(768), ingested_at timestamptz DEFAULT now()
);
CREATE TABLE IF NOT EXISTS vrr_agent.knowledge_registry (
  doc_id text PRIMARY KEY, file_name text, status text DEFAULT 'pending_review',
  reviewed_by text, pii_found boolean, pii_kinds text, n_chunks int, registered_at timestamptz DEFAULT now()
);
-- Browser uploads (api/routes_knowledge.py) need provenance the folder-drop flow never
-- did: WHO sent the file, what it weighed, and what it hashed to. Added as ALTERs rather
-- than folded into the CREATE above so a database seeded before uploads existed gains
-- them on the next `psql -f schema.sql` instead of needing a rebuild.
ALTER TABLE vrr_agent.knowledge_registry
  ADD COLUMN IF NOT EXISTS source        text DEFAULT 'folder',  -- folder | upload
  ADD COLUMN IF NOT EXISTS uploaded_by   text,                   -- token subject
  ADD COLUMN IF NOT EXISTS stored_name   text,                   -- sanitised name on disk
  ADD COLUMN IF NOT EXISTS content_kind  text,                   -- pdf|text|html|docx|csv
  ADD COLUMN IF NOT EXISTS size_bytes    bigint,
  ADD COLUMN IF NOT EXISTS sha256        text,
  ADD COLUMN IF NOT EXISTS reviewed_at   timestamptz,
  ADD COLUMN IF NOT EXISTS review_note   text,                   -- why it was rejected
  ADD COLUMN IF NOT EXISTS ingest_error  text;                   -- why embedding failed
-- Content-addressed dedupe. Re-uploading the same bytes under a new name must not create
-- a second copy of every chunk in the vector index, where it would crowd out other
-- documents in the top-k. Partial, because the folder-drop rows have no hash.
-- Which CORPUS a document belongs to. Not cosmetic: the top-k a question gets back is
-- finite, so "how do I approve a change?" would otherwise compete with the injection
-- change PROCEDURE for the same four slots — and the reservoir document wins on
-- similarity, answering a question about a button with a paragraph about valve limits.
-- Two corpora in one table, never mixed in one search.
ALTER TABLE vrr_agent.knowledge_registry
  ADD COLUMN IF NOT EXISTS doc_kind text NOT NULL DEFAULT 'reservoir';
ALTER TABLE vrr_agent.reservoir_knowledge
  ADD COLUMN IF NOT EXISTS doc_kind text NOT NULL DEFAULT 'reservoir';
CREATE INDEX IF NOT EXISTS reservoir_knowledge_kind_idx
  ON vrr_agent.reservoir_knowledge (doc_kind);
CREATE UNIQUE INDEX IF NOT EXISTS knowledge_registry_sha256_idx
  ON vrr_agent.knowledge_registry (sha256) WHERE sha256 IS NOT NULL;
CREATE INDEX IF NOT EXISTS knowledge_registry_status_idx
  ON vrr_agent.knowledge_registry (status, registered_at DESC);
CREATE INDEX IF NOT EXISTS reservoir_knowledge_doc_idx
  ON vrr_agent.reservoir_knowledge (doc_id);
-- Analyst chat transcript. One row per question+answer turn asked in the chat drawer
-- drawer, scoped by pattern and SHARED across users: opening a pattern shows what anyone
-- already asked about it, so a review is not restarted from zero. Written only by the app
-- (agent/history.py) and never by chat.respond(), so evaluation runs (make traces) do not
-- pollute it. `payload` is the tool output the answer was built from (truncated); `meta`
-- is the gate/LLM provenance rendered under each answer. Deletes are soft (deleted_at).
CREATE TABLE IF NOT EXISTS vrr_agent.chat_history (
  chat_id text PRIMARY KEY, id_pattern text NOT NULL, pattern_name text, vrr_date date,
  question text NOT NULL, answer text, intent text, agentic boolean DEFAULT false,
  llm_used boolean, model text, gate text, tools_called jsonb, meta jsonb, payload jsonb,
  asked_by text, deleted_at timestamptz, run_id text, created_at timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chat_history_pattern_created_idx
  ON vrr_agent.chat_history (id_pattern, created_at DESC);

-- API accounts. The approval chain is a chain of PEOPLE, so who you are has to be
-- something the server establishes rather than something the client claims: before this
-- table the role travelled in the request body, and any caller could POST "role":"site"
-- and execute a valve change. Now `role` is a signed JWT claim minted from THIS row.
-- Passwords are bcrypt hashes; the plaintext never reaches the database or a log.
-- Seed demo accounts with `make users`.
CREATE TABLE IF NOT EXISTS vrr_agent.app_user (
  username text PRIMARY KEY,
  password_hash text NOT NULL,
  role text NOT NULL CHECK (role IN ('analyst', 'rm', 'site', 'data_steward', 'admin')),
  full_name text,
  active boolean NOT NULL DEFAULT true,
  created_at timestamptz DEFAULT now(),
  last_login timestamptz
);

-- Per-user "clear my chat" cutoffs. Clearing hides the transcript FOR THAT USER from
-- this timestamp back; the chat_history rows are never touched and every turn stays in
-- MLflow as a trace. An audit trail a user can erase from the UI is not an audit trail.
CREATE TABLE IF NOT EXISTS vrr_agent.chat_clear (
  username text NOT NULL, id_pattern text NOT NULL,
  cleared_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (username, id_pattern)
);

-- ===========================================================================
-- vrr_stream — the streaming landing zone
-- ===========================================================================
-- Volumes ARRIVE here rather than being seeded. The seeded history ends
-- 2026-07-31; the simulator continues forward from the next day, so this
-- schema never competes with the demo data. A separate promotion step moves
-- settled rows into vrr_raw.production_volumes_daily.
CREATE SCHEMA IF NOT EXISTS vrr_stream;

-- Calibrated per-completion base rates. pipeline/seed.py computes these inside
-- generate_raw and throws them away; persisting them is what lets a single day
-- be generated in isolation instead of replaying three years of draws.
CREATE TABLE IF NOT EXISTS vrr_stream.completion_base_rate (
  id_completion text PRIMARY KEY,
  id_pattern    text NOT NULL,
  role          text NOT NULL,              -- producer | injector
  base_oil      double precision,
  base_water    double precision,
  base_gas      double precision,
  base_inj      double precision,           -- AFTER _calibrate_injection
  seed          bigint NOT NULL,
  built_at      timestamptz DEFAULT now()
);

-- The landing table. Four clocks, because three of them are genuinely
-- different: event_date is a SIMULATED business date (it runs ahead of the
-- wall clock), so "now() - event_date" would be meaningless as a latency.
--   end-to-end = stored_ts  - emitted_ts
--   broker     = ingest_ts  - emitted_ts
--   sink       = stored_ts  - ingest_ts
CREATE TABLE IF NOT EXISTS vrr_stream.volume_events (
  id_completion text NOT NULL,
  event_date    date NOT NULL,              -- simulated production date
  alloc_oil_vol_stb        double precision,
  alloc_water_vol_stb      double precision,
  alloc_gas_vol_kscf       double precision,
  alloc_water_inj_vol_stb  double precision,
  alloc_gas_inj_vol_kscf   double precision,
  uom           text DEFAULT 'OilField',
  emitted_ts    timestamptz NOT NULL,       -- producer published
  ingest_ts     timestamptz NOT NULL,       -- consumer read it off the topic
  -- clock_timestamp(), NOT now(): now() is TRANSACTION START time and is constant
  -- for a whole executemany, so every row in a batch shared one value and the
  -- measured latency was a transaction boundary rather than a wall clock.
  stored_ts     timestamptz DEFAULT clock_timestamp(),  -- Postgres wrote the row
  partition     int,
  "offset"      bigint,
  promoted_at   timestamptz,                -- NULL until promoted to vrr_raw
  -- Incremented on conflict. sum(n_deliveries - 1) is literal, countable evidence
  -- that at-least-once delivery really did redeliver something.
  n_deliveries  int NOT NULL DEFAULT 1,
  PRIMARY KEY (id_completion, event_date)   -- the ON CONFLICT target: replaying
);                                          -- an offset is a no-op, not a dupe

CREATE INDEX IF NOT EXISTS volume_events_stored_idx
  ON vrr_stream.volume_events (stored_ts);
CREATE INDEX IF NOT EXISTS volume_events_unpromoted_idx
  ON vrr_stream.volume_events (event_date) WHERE promoted_at IS NULL;

-- Spark's own numbers, captured from StreamingQueryProgress by a listener.
CREATE TABLE IF NOT EXISTS vrr_stream.batch_progress (
  batch_id    bigint PRIMARY KEY,
  ts          timestamptz DEFAULT now(),
  input_rows  bigint,
  rows_per_second double precision,
  duration_ms bigint,
  sources     jsonb
);

-- Anything that failed to parse or write is kept, not dropped. A malformed
-- payload must not be able to kill the consumer.
CREATE TABLE IF NOT EXISTS vrr_stream.dead_letter (
  id        bigserial PRIMARY KEY,
  ts        timestamptz DEFAULT now(),
  partition int,
  "offset"  bigint,
  payload   text,
  error     text
);
