# MARS development commands.
#
# Two ways to work:
#   make up       everything in Docker (needs Docker Desktop)
#   make verify   every check that runs without Docker
#
# On Windows, run these under Git Bash, or read the recipe and run the command
# directly. Nothing here is required by the application - these are shortcuts.

SHELL := /bin/bash
.DEFAULT_GOAL := help

BACKEND := backend
FRONTEND := frontend
VENV := $(BACKEND)/.venv
PY := $(VENV)/Scripts/python.exe
ifeq (,$(wildcard $(VENV)/Scripts/python.exe))
PY := $(VENV)/bin/python
endif

.PHONY: help
help: ## Show the available commands
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
.PHONY: install
install: install-backend install-frontend ## Install both toolchains

.PHONY: install-backend
install-backend: ## Create the backend virtual environment and install
	python -m venv $(VENV)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e "./$(BACKEND)[dev]"

.PHONY: install-frontend
install-frontend: ## Install frontend dependencies from the lockfile
	cd $(FRONTEND) && npm ci

# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------
.PHONY: up
up: ## Start the full stack (postgis, redis, api, worker, web)
	docker compose up --build -d
	@echo "API   http://localhost:8000/docs"
	@echo "Web   http://localhost:5173"
	@echo "Next: make migrate"

.PHONY: down
down: ## Stop the stack, keeping data
	docker compose down

.PHONY: clean
clean: ## Stop the stack and discard all data
	docker compose down -v

.PHONY: logs
logs: ## Follow service logs
	docker compose logs -f

.PHONY: compose-config
compose-config: ## Validate the Compose file
	docker compose config --quiet && echo "docker-compose.yml is valid"

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
.PHONY: migrate
migrate: ## Apply migrations to the running database
	docker compose run --rm api alembic upgrade head

.PHONY: migrate-down
migrate-down: ## Roll back one migration
	docker compose run --rm api alembic downgrade -1

.PHONY: migration-sql
migration-sql: ## Render the full schema as SQL, without a database
	cd $(BACKEND) && MARS_DATABASE_URL=postgresql+psycopg://mars:offline@localhost:5432/mars \
		.venv/Scripts/alembic.exe upgrade head --sql

.PHONY: revision
revision: ## Create a migration: make revision M="add signal table"
	docker compose run --rm api alembic revision --autogenerate -m "$(M)"

# ---------------------------------------------------------------------------
# Quality gates
# ---------------------------------------------------------------------------
.PHONY: verify
verify: terminology backend-verify frontend-verify geography ## Every check that runs without Docker

.PHONY: backend-verify
backend-verify: ## Backend format, lint, types and tests
	cd $(BACKEND) && .venv/Scripts/ruff.exe format --check .
	cd $(BACKEND) && .venv/Scripts/ruff.exe check .
	cd $(BACKEND) && .venv/Scripts/mypy.exe
	cd $(BACKEND) && .venv/Scripts/pytest.exe -q

.PHONY: frontend-verify
frontend-verify: ## Frontend lint, types, tests and build
	cd $(FRONTEND) && npm run lint
	cd $(FRONTEND) && npm run typecheck
	cd $(FRONTEND) && npm run test
	cd $(FRONTEND) && npx vite build

.PHONY: terminology
terminology: ## Check for prohibited resistance claims
	$(PY) scripts/terminology_lint.py

.PHONY: geography
geography: ## Verify the supplied boundary files are unchanged
	$(PY) scripts/geography_audit.py --verify-only

.PHONY: geography-audit
geography-audit: ## Regenerate the geography audit document
	$(PY) scripts/geography_audit.py \
		--json data/manifests/geography-audit.json \
		--markdown docs/data-dictionary/geography-audit.md

# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------
.PHONY: contract
contract: ## Regenerate the OpenAPI contract and TypeScript types
	$(PY) scripts/export_openapi.py
	cd $(FRONTEND) && npm run generate:api

.PHONY: contract-check
contract-check: ## Fail if the contract is out of date
	$(PY) scripts/export_openapi.py --check

# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------
.PHONY: test-integration
test-integration: ## Run integration tests against the Compose database
	cd $(BACKEND) && MARS_TEST_DATABASE_URL=postgresql+psycopg://mars:mars_local_development@localhost:5433/mars \
		.venv/Scripts/pytest.exe -m integration -q
