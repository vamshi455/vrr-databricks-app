# Prefer the project venv when one exists, so `make` works without activating it (and is
# never hijacked by whatever `python` a conda base env happens to put first on PATH).
# Override explicitly with `make <target> PYTHON=...` to use a different interpreter.
PYTHON ?= $(shell test -x .venv/bin/python && echo .venv/bin/python || echo python)

.PHONY: up down install test seed build audit knowledge loaders chunks floor llm-check queue writeback register users diagram api web web-build app share agent \
	stream-init stream-produce guide \
        agent-model prompts traces eval judges lint

up:            ## start the local OSS stack (postgres+pgvector, unity catalog, mlflow)
	# Host maps MLflow 5001→container 5000 (macOS AirPlay holds 5000). Point
	# MLFLOW_TRACKING_URI at http://localhost:5001. Starts containers only —
	# seed / queue / app are separate; that path is not claimed verified here.
	docker compose up -d

down:          ## stop the stack
	docker compose down

install:       ## editable install + dev deps
	pip install -e ".[dev]"

test:          ## run the pure off-DB unit tests (no stack needed)
	# `$(PYTHON) -m pytest`, never a bare `pytest`: a bare one resolves through PATH to
	# conda base or homebrew 3.14 here, neither of which has psycopg, so every test that
	# imports the pipeline fails at collection. That is CLAUDE.md operating rule 1, and
	# this target was breaking it.
	$(PYTHON) -m pytest -q

seed:          ## generate + load synthetic VRR data into Postgres
	$(PYTHON) -m vrr_agent_open.pipeline.seed

build:         ## rebuild vrr_curated from vrr_raw only (core.physics; no reseed)
	$(PYTHON) -m vrr_agent_open.pipeline.build

audit:         ## input-audit gate: verdict per pattern (DATA_ARTIFACT vs REAL_SIGNAL)
	$(PYTHON) -m vrr_agent_open.pipeline.input_audit

knowledge:     ## register docs in ./knowledge_uploads, then ingest the APPROVED ones
	$(PYTHON) -m vrr_agent_open.pipeline.knowledge_ingest

loaders:       ## load ./knowledge_uploads (or from=… / a URL) → List[Document] summary
	$(PYTHON) -m vrr_agent_open.pipeline.document_loaders $(or $(from),./knowledge_uploads)

chunks:        ## compare chunking strategies, scored by retrieval (recall@k, MRR)
	$(PYTHON) -m vrr_agent_open.pipeline.text_splitters

floor:         ## measure the retrieval similarity floor (answerable vs off-topic)
	$(PYTHON) scripts/calibrate_floor.py

llm-check:     ## can each provider do a completion AND tool calling? (add p=openai)
	$(PYTHON) scripts/check_llm.py $(p)

queue:         ## run the anomaly → action_queue job (drafts for human approval)
	$(PYTHON) -m vrr_agent_open.pipeline.anomaly_to_queue

writeback:     ## fill actual_post_vrr from the next monthly VRR and EMA-update ρ
	$(PYTHON) -m vrr_agent_open.pipeline.outcome_writeback

agent-model:   ## log + register the agent as an MLflow model (alias: candidate)
	$(PYTHON) scripts/register_model.py

prompts:       ## push prompt templates to the MLflow Prompt Registry (alias: production)
	$(PYTHON) scripts/register_prompt.py

traces:        ## run the agent over data/evaluation questions, logging traces + expectations
	$(PYTHON) scripts/create_traces.py

eval:          ## score recent traces (deterministic scorers + LLM judges if a model is up)
	$(PYTHON) scripts/evaluate_model.py --eval-only

judges:        ## register the LLM judges server-side (add --start for automatic scoring)
	$(PYTHON) scripts/register_judge.py

register:      ## register vrr schemas/tables/functions in Unity Catalog OSS
	$(PYTHON) -m vrr_agent_open.governance.uc_register

users:         ## create vrr_agent.app_user + seed the demo accounts (add p=<password>)
	$(PYTHON) scripts/seed_users.py $(p)

diagram:       ## render the architecture posters (graphviz PNG + D2 SVG)
	$(PYTHON) scripts/make_architecture_diagram.py
	@command -v d2 >/dev/null \
	  && d2 --theme 200 --dark-theme 200 --pad 40 docs/architecture.d2 docs/img/architecture-d2.svg \
	  || echo "  (D2 skipped — brew install d2)"

api:           ## FastAPI backend (docs at http://localhost:8000/docs)
	$(PYTHON) -m uvicorn vrr_agent_open.api.main:app --reload --port 8000

web:           ## React dev server with hot reload (proxies /api to :8000)
	cd web && npm install && npm run dev

web-build:     ## build the React app; `make api` then serves it at :8000
	cd web && npm install && npm run build

app:           ## one-process workbench: build the UI, then serve it from FastAPI
	$(MAKE) web-build && $(PYTHON) -m uvicorn vrr_agent_open.api.main:app --port 8000

share:         ## expose the workbench publicly through an ngrok tunnel (demo posture)
	@command -v ngrok >/dev/null || { echo "ngrok not installed — brew install ngrok"; exit 1; }
	@ngrok config check >/dev/null 2>&1 || { \
	  echo "ngrok has no authtoken. Make a free account at https://dashboard.ngrok.com,"; \
	  echo "then: ngrok config add-authtoken <YOUR_TOKEN>"; exit 1; }
	@grep -qE '^VRR_JWT_SECRET=.+' .env 2>/dev/null || { \
	  echo "VRR_JWT_SECRET is not set in .env. Share mode refuses to start without it —"; \
	  echo "an ephemeral key signs tokens that die on the next restart. Generate one:"; \
	  echo '  $(PYTHON) -c "import secrets;print(secrets.token_urlsafe(48))"'; exit 1; }
	@curl -sf localhost:8000/api/health >/dev/null || { \
	  echo "Nothing is serving on :8000. Start it first, in another terminal:"; \
	  echo "  VRR_SHARE=1 make app          # reads need a sign-in"; \
	  echo "  VRR_SHARE=1 VRR_PUBLIC_READS=1 make app   # reads open to anyone with the link"; \
	  exit 1; }
	@curl -s localhost:8000/api/health | grep -q share_mode || { \
	  echo "The server on :8000 was NOT started with VRR_SHARE=1, so reads are open and"; \
	  echo "/api/health still reports your Postgres host. Restart it with VRR_SHARE=1"; \
	  echo "(env is read once at import — a running process cannot pick this up)."; exit 1; }
	@echo "Tunnelling :8000 — anyone with the printed URL can reach this machine."
	@echo "Stop with Ctrl-C; the URL dies with it."
	ngrok http 8000

stream-init:   ## create the vrr_stream schema + persist the calibrated base rates
	@command -v psql >/dev/null || { echo "psql not found — brew install libpq && brew link --force libpq"; exit 1; }
	psql "$${VRR_PG_DSN:-postgresql://vrr:vrr@localhost:5432/vrr}" \
	  -v ON_ERROR_STOP=1 -f src/vrr_agent_open/pipeline/schema.sql
	$(PYTHON) -m vrr_agent_open.streaming.rates

stream-produce: ## simulate production volumes (rate=days/sec days=N transport=direct|kafka)
	$(PYTHON) -m vrr_agent_open.streaming.producer \
	  $(if $(rate),--rate $(rate),) $(if $(days),--days $(days),) \
	  $(if $(transport),--transport $(transport),)

guide:         ## generate the in-app user guide from core/help_topics.py + ingest it
	$(PYTHON) scripts/build_app_guide.py

agent:         ## run one agent question from the CLI
	$(PYTHON) -m vrr_agent_open.agent.graph "Why is UNITY's VRR high in April 2026?"

lint:
	ruff check src tests
