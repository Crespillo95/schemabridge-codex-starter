SHELL := /bin/bash
PYTHON ?= $(shell command -v python3.13 2>/dev/null || command -v python3.12 2>/dev/null || command -v python3.11 2>/dev/null || command -v python3 2>/dev/null || command -v python 2>/dev/null)
VENV ?= .venv
BIN := $(VENV)/bin
DEMO_DATABASE_URL ?= postgresql://schemabridge_reader:schemabridge_reader@127.0.0.1:55433/schemabridge

.PHONY: help bootstrap install check format lint type test coverage doctor evaluate release-audit release-clean demo-up demo-down demo-reset demo-health demo-query demo-compile demo-preview demo-guard demo-governed-plan demo-governed-preview demo-intent test-intent test-integration test-acceptance datahub-version datahub-start datahub-health datahub-init-admin datahub-ingest datahub-provision-mcp datahub-provision-writer datahub-catalog-check datahub-restart datahub-reset datahub-stop datahub-mcp-check ui clean

help:
	@printf '%s\n' \
	  'make bootstrap   Create .venv and install development dependencies' \
	  'make check       Format check, lint, types, and unit tests' \
	  'make doctor      Verify the local starter environment' \
	  'make evaluate    Reset the synthetic database and reproduce evaluation reports' \
	  'make release-audit Scan architecture, secrets, links, artifacts, and licenses' \
	  'make release-clean Run the strict clean-HEAD M16 release proof' \
	  'make demo-up     Start synthetic PostgreSQL' \
	  'make demo-down   Stop synthetic PostgreSQL' \
	  'make demo-reset  Destroy and recreate synthetic PostgreSQL' \
	  'make demo-health Verify the reader connection and safety settings' \
	  'make demo-query  Run the separated north-star reference query' \
	  'make demo-compile Compile and guard the approved north-star query plan' \
	  'make demo-preview Execute the compiled north-star preview as the reader' \
	  'make demo-guard   Run three malicious SQL guard examples' \
	  'make demo-governed-plan Inspect the resolved guided north-star plan' \
	  'make demo-governed-preview Execute the governed preview and rejection report' \
	  'make demo-intent  Preview the Spanish request through the key-free typed parser' \
	  'make test-intent  Run natural-language unit and service-free acceptance tests' \
	  'make test-integration  Run PostgreSQL integration tests' \
	  'make test-acceptance   Run PostgreSQL-backed acceptance tests' \
	  'make datahub-start Start pinned local DataHub Core' \
	  'make datahub-health Verify DataHub GMS, UI, auth, and logical models' \
	  'make datahub-ingest Ingest the four synthetic PostgreSQL schemas' \
	  'make datahub-provision-mcp Create/reuse the local read-only MCP identity' \
	  'make datahub-provision-writer Create/reuse the approval-gated semantic writer' \
	  'make datahub-catalog-check Verify synthetic catalog assets and profiles' \
	  'make datahub-mcp-check Verify read-only MCP tools and catalog reads' \
	  'make datahub-restart Restart DataHub without deleting metadata' \
	  'make datahub-reset Destroy only local DataHub quickstart state and restart' \
	  'make datahub-stop Stop DataHub without deleting metadata' \
	  'make ui          Start the judge-ready Streamlit application'

bootstrap:
	@test -n "$(PYTHON)" || { printf '%s\n' 'Python >=3.11,<3.14 was not found on PATH.' >&2; exit 1; }
	@$(PYTHON) -c 'import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] < (3, 14) else "SchemaBridge requires Python >=3.11,<3.14")'
	$(PYTHON) -m venv --clear $(VENV)
	$(BIN)/python -m pip install --upgrade pip
	$(BIN)/python -m pip install -e '.[dev,postgres,sql,ui,llm]'
	$(BIN)/schemabridge version
	$(BIN)/schemabridge doctor

install:
	$(BIN)/python -m pip install -e '.[dev,postgres,sql,ui,datahub,llm]'

format:
	$(BIN)/ruff format src tests scripts
	$(BIN)/ruff check --fix src tests scripts

lint:
	$(BIN)/ruff format --check src tests scripts
	$(BIN)/ruff check src tests scripts

type:
	$(BIN)/mypy src

test:
	$(BIN)/pytest -m 'not integration and not acceptance'

coverage:
	$(BIN)/pytest --cov=schemabridge --cov-report=term-missing -m 'not integration and not acceptance'

check: lint type test

doctor:
	$(BIN)/schemabridge doctor

evaluate: demo-reset
	@DATABASE_URL='$(DEMO_DATABASE_URL)' $(BIN)/python -m schemabridge.entrypoints.cli.main evaluate --output reports/evaluation.json --markdown examples/evaluation-report.md

release-audit:
	$(BIN)/python scripts/release_audit.py --require-release --check-external

release-clean:
	bash scripts/release_clean_room.sh

demo-up:
	docker compose -f docker-compose.demo.yml up -d --wait

demo-down:
	docker compose -f docker-compose.demo.yml down

demo-reset:
	docker compose -f docker-compose.demo.yml down -v --remove-orphans
	docker compose -f docker-compose.demo.yml up -d --wait

demo-health:
	@DATABASE_URL='$(DEMO_DATABASE_URL)' $(BIN)/schemabridge postgres-health

demo-query:
	@docker compose -f docker-compose.demo.yml exec -T -e PGPASSWORD=schemabridge_reader postgres psql -h 127.0.0.1 -U schemabridge_reader -d schemabridge -v ON_ERROR_STOP=1 -f - < demo/reference/north_star.sql

demo-compile:
	$(BIN)/schemabridge query-demo

demo-preview:
	@DATABASE_URL='$(DEMO_DATABASE_URL)' $(BIN)/schemabridge query-demo --execute

demo-guard:
	$(BIN)/schemabridge sql-guard-demo

demo-governed-plan:
	$(BIN)/schemabridge governed-demo

demo-governed-preview:
	@DATABASE_URL='$(DEMO_DATABASE_URL)' $(BIN)/schemabridge governed-demo --execute

demo-intent:
	$(BIN)/schemabridge intent-demo

test-intent:
	$(BIN)/pytest tests/unit -k 'intent or language or prompt'
	$(BIN)/pytest -m acceptance -k natural_language

test-integration:
	@SCHEMABRIDGE_TEST_DATABASE_URL='$(DEMO_DATABASE_URL)' $(BIN)/pytest -m integration

test-acceptance:
	@SCHEMABRIDGE_TEST_DATABASE_URL='$(DEMO_DATABASE_URL)' $(BIN)/pytest -m acceptance

datahub-version:
	@bash scripts/datahub.sh version

datahub-start:
	@bash scripts/datahub.sh start

datahub-health:
	@bash scripts/datahub.sh health

datahub-init-admin:
	@bash scripts/datahub.sh init-admin

datahub-ingest:
	@bash scripts/datahub.sh ingest

datahub-provision-mcp:
	@bash scripts/datahub.sh provision-mcp

datahub-provision-writer:
	@bash scripts/datahub.sh provision-writer

datahub-catalog-check:
	@$(BIN)/python scripts/check_datahub_catalog.py

datahub-restart:
	@bash scripts/datahub.sh restart

datahub-reset:
	@bash scripts/datahub.sh reset

datahub-stop:
	@bash scripts/datahub.sh stop

datahub-mcp-check:
	@UV_CACHE_DIR=.local/uv-cache UV_TOOL_DIR=.local/uv-tools UV_PYTHON_INSTALL_DIR=.local/uv-python \
	  $(BIN)/uv run --python 3.11 --no-project --with 'mcp-server-datahub==0.6.0' \
	  scripts/check_datahub_mcp.py

ui:
	@DATABASE_URL='$(DEMO_DATABASE_URL)' $(BIN)/streamlit run src/schemabridge/entrypoints/streamlit/app.py

clean:
	rm -rf .venv .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov dist build
