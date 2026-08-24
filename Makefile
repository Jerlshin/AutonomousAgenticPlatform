# ==============================================================================
#  Pluton R&D Engine — Makefile
#
#  Targets are grouped by lifecycle. Anything marked [planned] depends on a
#  component that is specified but not yet implemented; it prints the relevant
#  spec section instead of failing with a confusing error.
#
#  Specs: docs/ARCHITECTURE.md · docs/AGENTS.md · docs/MLOPS.md · notes.md
# ==============================================================================

SHELL       := /bin/bash
BACKEND     := backend
PY          := python3
PROFILE     ?=
COMPOSE_ARGS = $(if $(PROFILE),--profile $(PROFILE),)
# The compose file lives in infrastructure/, so compose would otherwise look for its
# variables in infrastructure/.env. Point it at the repository-root .env, which is the
# one `make init-secrets` writes and the backend reads.
ENV_FILE     = $(if $(wildcard .env),--env-file .env,)
COMPOSE     := docker compose $(ENV_FILE) -f infrastructure/docker-compose.yml

# Colours
C_OK   := \033[0;32m
C_WARN := \033[0;33m
C_ERR  := \033[0;31m
C_HDR  := \033[1;36m
C_OFF  := \033[0m

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@printf "$(C_HDR)Pluton R&D Engine$(C_OFF)\n\n"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'
	@printf "\n  Pass PROFILE=observability|hardened|linux-gpu to compose targets.\n\n"

# ------------------------------------------------------------------------------
#  Setup
# ------------------------------------------------------------------------------

.PHONY: setup
setup: init-secrets pull-models ## First-run setup: secrets + Ollama models
	@printf "$(C_OK)Setup complete. Next: make up && make migrate$(C_OFF)\n"

.PHONY: init-secrets
init-secrets: ## Generate .env from .env.example with fresh secrets
	@if [ -f .env ]; then \
		printf "$(C_WARN).env already exists — not overwriting. Delete it first to regenerate.$(C_OFF)\n"; \
	else \
		$(PY) scripts/gen_env_example.py; \
		cp .env.example .env; \
		$(PY) scripts/gen_secrets.py .env; \
		chmod 600 .env; \
		printf "$(C_OK)Wrote .env with generated secrets (mode 0600).$(C_OFF)\n"; \
	fi

.PHONY: gen-env-example
gen-env-example: ## Regenerate .env.example from backend/app/core/config.py
	$(PY) scripts/gen_env_example.py

.PHONY: check-env-example
check-env-example: ## Fail if .env.example has drifted from Settings
	$(PY) scripts/gen_env_example.py --check

.PHONY: pull-models
pull-models: ## Pull the Ollama models this platform routes to
	@command -v ollama >/dev/null 2>&1 || { \
		printf "$(C_ERR)ollama not found. Install from https://ollama.com/download$(C_OFF)\n"; exit 1; }
	@printf "$(C_HDR)Pulling models (~20 GB for the standard tier)...$(C_OFF)\n"
	ollama pull qwen2.5:14b-instruct
	ollama pull llama3.1:8b
	ollama pull qwen2.5-coder:7b
	ollama pull nomic-embed-text

.PHONY: pull-models-small
pull-models-small: ## Pull the low-resource model tier (~6 GB, for <16 GB RAM)
	ollama pull llama3.2:3b
	ollama pull qwen2.5-coder:3b
	ollama pull nomic-embed-text

.PHONY: doctor
doctor: ## Diagnose the local environment
	@printf "$(C_HDR)Environment check$(C_OFF)\n"
	@printf "  docker      : "; docker --version 2>/dev/null || printf "$(C_ERR)MISSING$(C_OFF)\n"
	@printf "  compose     : "; docker compose version --short 2>/dev/null || printf "$(C_ERR)MISSING$(C_OFF)\n"
	@printf "  python      : "; $(PY) --version 2>/dev/null || printf "$(C_ERR)MISSING$(C_OFF)\n"
	@printf "  ollama      : "; ollama --version 2>/dev/null || printf "$(C_ERR)MISSING$(C_OFF)\n"
	@printf "  .env        : "; [ -f .env ] && printf "$(C_OK)present$(C_OFF)\n" || printf "$(C_ERR)missing — run 'make init-secrets'$(C_OFF)\n"
	@printf "  ollama api  : "; curl -fsS http://localhost:11434/api/version 2>/dev/null || printf "$(C_ERR)unreachable on :11434$(C_OFF)\n"
	@printf "\n$(C_HDR)Containers$(C_OFF)\n"
	@$(COMPOSE) ps 2>/dev/null || printf "  (stack not running)\n"

# ------------------------------------------------------------------------------
#  Stack lifecycle
# ------------------------------------------------------------------------------

.PHONY: up
up: ## Start the stack (PROFILE=... for extras)
	$(COMPOSE) $(COMPOSE_ARGS) up -d
	@$(MAKE) --no-print-directory ps

.PHONY: up-infra
up-infra: ## Start data services only (postgres, redis, qdrant, mlflow)
	$(COMPOSE) up -d postgres redis qdrant mlflow
	@$(MAKE) --no-print-directory ps

.PHONY: down
down: ## Stop the stack, keeping volumes
	$(COMPOSE) down

.PHONY: nuke
nuke: ## Stop the stack and DELETE ALL VOLUMES (irreversible)
	@printf "$(C_ERR)This deletes postgres, redis, qdrant, and mlflow data permanently.$(C_OFF)\n"
	@read -p "Type 'nuke' to confirm: " ans; [ "$$ans" = "nuke" ] || { echo "Aborted."; exit 1; }
	$(COMPOSE) down -v

.PHONY: restart
restart: down up ## Restart the stack

.PHONY: ps
ps: ## Show container status
	@$(COMPOSE) ps

.PHONY: logs
logs: ## Tail logs (S=service to narrow, e.g. make logs S=mlflow)
	$(COMPOSE) logs -f --tail=200 $(S)

.PHONY: health
health: ## Query the deep health endpoint
	@curl -fsS http://localhost:8000/api/v1/health/deep | $(PY) -m json.tool \
		|| printf "$(C_ERR)API unreachable on :8000 — is it running?$(C_OFF)\n"

# ------------------------------------------------------------------------------
#  Database
# ------------------------------------------------------------------------------

.PHONY: migrate
migrate: ## Apply Alembic migrations to head
	cd $(BACKEND) && alembic upgrade head

.PHONY: migration
migration: ## Create a migration (M="message")
	@[ -n "$(M)" ] || { printf "$(C_ERR)Usage: make migration M=\"add runs table\"$(C_OFF)\n"; exit 1; }
	cd $(BACKEND) && alembic revision --autogenerate -m "$(M)"

.PHONY: downgrade
downgrade: ## Roll back one migration
	cd $(BACKEND) && alembic downgrade -1

.PHONY: psql
psql: ## Open a psql shell on the app database
	$(COMPOSE) exec postgres psql -U postgres -d agent_platform

.PHONY: redis-cli
redis-cli: ## Open a redis-cli shell
	$(COMPOSE) exec redis redis-cli

# ------------------------------------------------------------------------------
#  Development
# ------------------------------------------------------------------------------

.PHONY: dev
dev: ## Run the API locally with reload
	cd $(BACKEND) && uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

.PHONY: install
install: ## Install backend dependencies into the active environment
	$(PY) -m pip install -e ".[dev]"

.PHONY: lint
lint: ## Ruff lint + format check
	cd $(BACKEND) && ruff check .
	cd $(BACKEND) && ruff format --check .

.PHONY: format
format: ## Ruff auto-format and auto-fix
	cd $(BACKEND) && ruff format .
	cd $(BACKEND) && ruff check --fix .

.PHONY: typecheck
typecheck: ## Static type check
	cd $(BACKEND) && mypy app

.PHONY: test
test: ## Run the backend test suite
	cd $(BACKEND) && pytest -q

.PHONY: test-cov
test-cov: ## Run tests with a coverage report
	cd $(BACKEND) && pytest --cov=app --cov-report=term-missing --cov-report=html

.PHONY: check-docs
check-docs: ## Verify every intra-repo documentation link resolves
	$(PY) scripts/check_docs_links.py

.PHONY: check
check: lint typecheck test check-docs check-env-example ## Everything CI runs

.PHONY: clean
clean: ## Remove caches and build detritus
	find . -type d -name "__pycache__"   -prune -exec rm -rf {} +
	find . -type d -name ".ruff_cache"   -prune -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -prune -exec rm -rf {} +
	find . -type d -name ".mypy_cache"   -prune -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	rm -rf $(BACKEND)/htmlcov $(BACKEND)/.coverage

# ------------------------------------------------------------------------------
#  Sandbox, datasets, corpus  [planned — see docs/ARCHITECTURE.md §10]
# ------------------------------------------------------------------------------

.PHONY: build-sandbox
build-sandbox: ## [planned] Build the sandbox images and pin their digests
	@if [ ! -s infrastructure/docker/sandbox/Dockerfile.exec ]; then \
		printf "$(C_WARN)Sandbox Dockerfiles are not written yet.$(C_OFF)\n"; \
		printf "  Spec: docs/ARCHITECTURE.md §10.10 (Sandbox images)\n"; exit 0; \
	fi
	docker build -f infrastructure/docker/sandbox/Dockerfile.exec  -t pluton-sandbox-exec:latest  infrastructure/docker/sandbox
	docker build -f infrastructure/docker/sandbox/Dockerfile.train -t pluton-sandbox-train:latest infrastructure/docker/sandbox
	@docker image inspect pluton-sandbox-exec:latest pluton-sandbox-train:latest \
		--format '{{.RepoTags}} {{.Id}}' > infrastructure/docker/sandbox/digests.json
	@printf "$(C_OK)Sandbox images built; digests pinned.$(C_OFF)\n"

.PHONY: seed-datasets
seed-datasets: ## [planned] Populate the read-only dataset registry
	@if [ ! -f scripts/seed_datasets.py ]; then \
		printf "$(C_WARN)scripts/seed_datasets.py not written yet.$(C_OFF)\n"; \
		printf "  Spec: docs/ARCHITECTURE.md §10.8 (Dataset registry)\n"; exit 0; \
	fi
	$(PY) scripts/seed_datasets.py

.PHONY: ingest
ingest: ## [planned] Ingest documents into the RAG corpus (D=path)
	@if [ ! -f scripts/ingest_corpus.py ]; then \
		printf "$(C_WARN)scripts/ingest_corpus.py not written yet.$(C_OFF)\n"; \
		printf "  Spec: docs/ARCHITECTURE.md §7.3 (Qdrant collections)\n"; exit 0; \
	fi
	$(PY) scripts/ingest_corpus.py --path "$(or $(D),corpus/)"

.PHONY: prune-runs
prune-runs: ## [planned] Sweep expired run scratch directories
	@printf "$(C_WARN)Run-volume sweep is specified in docs/MLOPS.md §8.4.$(C_OFF)\n"

# ------------------------------------------------------------------------------
#  MLOps
# ------------------------------------------------------------------------------

.PHONY: mlflow-ui
mlflow-ui: ## Open the MLflow UI (host port 5001)
	@printf "MLflow UI: http://localhost:5001\n"
	@command -v open >/dev/null 2>&1 && open http://localhost:5001 || true

.PHONY: storage-report
storage-report: ## Report volume usage
	@docker system df -v 2>/dev/null | grep -E 'VOLUME NAME|postgres_data|redis_data|qdrant_data|mlflow' || true

# ------------------------------------------------------------------------------
#  Benchmarks  [planned — see docs/AGENTS.md §13]
# ------------------------------------------------------------------------------

.PHONY: bench
bench: ## [planned] Run the core-10 benchmark suite
	@printf "$(C_WARN)Benchmark suites are specified in docs/AGENTS.md §13.$(C_OFF)\n"

.PHONY: bench-rag
bench-rag: ## [planned] Measure retrieval precision@5
	@printf "$(C_WARN)RAG benchmark is specified in docs/AGENTS.md §13.1.$(C_OFF)\n"

# ------------------------------------------------------------------------------
#  Frontend  [planned — see docs/ARCHITECTURE.md §9]
# ------------------------------------------------------------------------------

.PHONY: fe-install
fe-install: ## [planned] Install frontend dependencies
	@if [ ! -s frontend/package.json ]; then \
		printf "$(C_WARN)frontend/package.json is empty — the Next.js app is not scaffolded yet.$(C_OFF)\n"; exit 0; \
	fi
	cd frontend && npm install

.PHONY: fe-dev
fe-dev: ## [planned] Run the Next.js dev server
	@if [ ! -s frontend/package.json ]; then \
		printf "$(C_WARN)frontend/package.json is empty — the Next.js app is not scaffolded yet.$(C_OFF)\n"; exit 0; \
	fi
	cd frontend && npm run dev
