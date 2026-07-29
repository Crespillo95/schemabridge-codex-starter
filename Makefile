SHELL := /bin/bash
PYTHON ?= $(shell command -v python3.13 2>/dev/null || command -v python3.12 2>/dev/null || command -v python3.11 2>/dev/null || command -v python3 2>/dev/null || command -v python 2>/dev/null)
VENV ?= .venv
BIN := $(VENV)/bin
DEMO_DATABASE_URL ?= postgresql://schemabridge_reader:schemabridge_reader@127.0.0.1:55433/schemabridge
CONTROL_DATABASE_URL ?= postgresql://schemabridge_runtime:schemabridge_runtime@127.0.0.1:55434/schemabridge_control
CONTROL_RECONCILER_DATABASE_URL ?= postgresql://schemabridge_reconciler:schemabridge_reconciler@127.0.0.1:55434/schemabridge_control
CONTROL_MIGRATOR_DATABASE_URL ?= postgresql://schemabridge_migrator:schemabridge_migrator@127.0.0.1:55434/schemabridge_control
CONTROL_API_DATABASE_URL ?= postgresql://schemabridge_api:schemabridge_api@127.0.0.1:55434/schemabridge_control
CONTROL_WORKER_DATABASE_URL ?= postgresql://schemabridge_worker:schemabridge_worker@127.0.0.1:55434/schemabridge_control
CONTROL_CATALOG_DATABASE_URL ?= postgresql://schemabridge_catalog:schemabridge_catalog@127.0.0.1:55434/schemabridge_control
EXECUTION_CONNECTOR_SECRET_DIRECTORY ?= $(abspath .local/connector-secrets/execution)
PROFILE_CONNECTOR_SECRET_DIRECTORY ?= $(abspath .local/connector-secrets/profile)
CATALOG_CONNECTOR_SECRET_DIRECTORY ?= $(abspath .local/connector-secrets/catalog)
API_CLEAN_ENV := env \
	-u DATABASE_URL \
	-u OPENAI_API_KEY \
	-u DATAHUB_GMS_TOKEN \
	-u SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY \
	-u SCHEMABRIDGE_CONTROL_DATABASE_URL \
	-u SCHEMABRIDGE_CONTROL_RECONCILER_DATABASE_URL \
	-u SCHEMABRIDGE_CONTROL_MIGRATOR_DATABASE_URL \
	-u SCHEMABRIDGE_CONTROL_WORKER_DATABASE_URL \
	-u SCHEMABRIDGE_CONTROL_CATALOG_DATABASE_URL \
	-u SCHEMABRIDGE_CONTROL_RESTORE_DATABASE_URL \
	-u SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY \
	-u SCHEMABRIDGE_CONTROL_OPERATOR_ACTOR_ID \
	-u SCHEMABRIDGE_CONTROL_OPERATOR_ROLES \
	-u SCHEMABRIDGE_IDENTITY_MIGRATION_KEY
WORKER_CLEAN_ENV := env \
	-u DATABASE_URL \
	-u OPENAI_API_KEY \
	-u DATAHUB_GMS_TOKEN \
	-u SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY \
	-u SCHEMABRIDGE_CONTROL_DATABASE_URL \
	-u SCHEMABRIDGE_CONTROL_RECONCILER_DATABASE_URL \
	-u SCHEMABRIDGE_CONTROL_MIGRATOR_DATABASE_URL \
	-u SCHEMABRIDGE_CONTROL_API_DATABASE_URL \
	-u SCHEMABRIDGE_CONTROL_CATALOG_DATABASE_URL \
	-u SCHEMABRIDGE_CONTROL_RESTORE_DATABASE_URL \
	-u SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY \
	-u SCHEMABRIDGE_CONTROL_OPERATOR_ACTOR_ID \
	-u SCHEMABRIDGE_CONTROL_OPERATOR_ROLES \
	-u SCHEMABRIDGE_IDENTITY_MIGRATION_KEY \
	-u SCHEMABRIDGE_OIDC_ISSUER \
	-u SCHEMABRIDGE_OIDC_AUDIENCE \
	-u SCHEMABRIDGE_OIDC_PROVIDER \
	-u SCHEMABRIDGE_OIDC_ALLOWED_GROUPS \
	-u SCHEMABRIDGE_OIDC_ALLOWED_TENANTS \
	-u SCHEMABRIDGE_PSEUDONYMIZATION_KEY \
	-u SCHEMABRIDGE_API_LOCAL_BEARER_TOKEN \
	-u SCHEMABRIDGE_API_OIDC_JWKS_URL \
	-u SCHEMABRIDGE_INVENTORY_CURSOR_SIGNING_KEY
CATALOG_CLEAN_ENV := env \
	-u DATABASE_URL \
	-u POSTGRES_READER_USER \
	-u OPENAI_API_KEY \
	-u DATAHUB_GMS_URL \
	-u DATAHUB_GMS_TOKEN \
	-u SCHEMABRIDGE_LLM_MODEL \
	-u SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY \
	-u SCHEMABRIDGE_CONTROL_DATABASE_URL \
	-u SCHEMABRIDGE_CONTROL_RECONCILER_DATABASE_URL \
	-u SCHEMABRIDGE_CONTROL_MIGRATOR_DATABASE_URL \
	-u SCHEMABRIDGE_CONTROL_API_DATABASE_URL \
	-u SCHEMABRIDGE_CONTROL_WORKER_DATABASE_URL \
	-u SCHEMABRIDGE_CONTROL_RESTORE_DATABASE_URL \
	-u SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY \
	-u SCHEMABRIDGE_CONTROL_OPERATOR_ACTOR_ID \
	-u SCHEMABRIDGE_CONTROL_OPERATOR_ROLES \
	-u SCHEMABRIDGE_IDENTITY_MIGRATION_KEY \
	-u SCHEMABRIDGE_OIDC_ISSUER \
	-u SCHEMABRIDGE_OIDC_AUDIENCE \
	-u SCHEMABRIDGE_OIDC_PROVIDER \
	-u SCHEMABRIDGE_OIDC_ALLOWED_GROUPS \
	-u SCHEMABRIDGE_OIDC_ALLOWED_TENANTS \
	-u SCHEMABRIDGE_PSEUDONYMIZATION_KEY \
	-u SCHEMABRIDGE_API_LOCAL_BEARER_TOKEN \
	-u SCHEMABRIDGE_API_OIDC_JWKS_URL \
	-u SCHEMABRIDGE_INVENTORY_CURSOR_SIGNING_KEY
RECONCILER_CLEAN_ENV := env \
	-u DATABASE_URL \
	-u POSTGRES_READER_USER \
	-u OPENAI_API_KEY \
	-u SCHEMABRIDGE_LLM_MODEL \
	-u DATAHUB_GMS_URL \
	-u DATAHUB_GMS_TOKEN \
	-u SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY \
	-u SCHEMABRIDGE_CONTROL_DATABASE_URL \
	-u SCHEMABRIDGE_CONTROL_MIGRATOR_DATABASE_URL \
	-u SCHEMABRIDGE_CONTROL_API_DATABASE_URL \
	-u SCHEMABRIDGE_CONTROL_WORKER_DATABASE_URL \
	-u SCHEMABRIDGE_CONTROL_CATALOG_DATABASE_URL \
	-u SCHEMABRIDGE_CONTROL_RESTORE_DATABASE_URL \
	-u SCHEMABRIDGE_IDENTITY_MIGRATION_KEY \
	-u SCHEMABRIDGE_OIDC_ISSUER \
	-u SCHEMABRIDGE_OIDC_AUDIENCE \
	-u SCHEMABRIDGE_OIDC_PROVIDER \
	-u SCHEMABRIDGE_OIDC_ALLOWED_GROUPS \
	-u SCHEMABRIDGE_OIDC_ALLOWED_TENANTS \
	-u SCHEMABRIDGE_PSEUDONYMIZATION_KEY \
	-u SCHEMABRIDGE_API_LOCAL_BEARER_TOKEN \
	-u SCHEMABRIDGE_API_OIDC_JWKS_URL \
	-u SCHEMABRIDGE_INVENTORY_CURSOR_SIGNING_KEY \
	-u SCHEMABRIDGE_CATALOG_DATAHUB_CREDENTIAL_BINDING_REF
PROFILE_WORKER_CLEAN_ENV := env \
	-u DATABASE_URL \
	-u OPENAI_API_KEY \
	-u SCHEMABRIDGE_LLM_MODEL \
	-u DATAHUB_GMS_URL \
	-u DATAHUB_GMS_TOKEN \
	-u SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY \
	-u SCHEMABRIDGE_CONTROL_DATABASE_URL \
	-u SCHEMABRIDGE_CONTROL_RECONCILER_DATABASE_URL \
	-u SCHEMABRIDGE_CONTROL_MIGRATOR_DATABASE_URL \
	-u SCHEMABRIDGE_CONTROL_API_DATABASE_URL \
	-u SCHEMABRIDGE_CONTROL_CATALOG_DATABASE_URL \
	-u SCHEMABRIDGE_CONTROL_RESTORE_DATABASE_URL \
	-u SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY \
	-u SCHEMABRIDGE_CONTROL_OPERATOR_ACTOR_ID \
	-u SCHEMABRIDGE_CONTROL_OPERATOR_ROLES \
	-u SCHEMABRIDGE_IDENTITY_MIGRATION_KEY \
	-u SCHEMABRIDGE_OIDC_ISSUER \
	-u SCHEMABRIDGE_OIDC_AUDIENCE \
	-u SCHEMABRIDGE_OIDC_PROVIDER \
	-u SCHEMABRIDGE_OIDC_ALLOWED_GROUPS \
	-u SCHEMABRIDGE_OIDC_ALLOWED_TENANTS \
	-u SCHEMABRIDGE_PSEUDONYMIZATION_KEY \
	-u SCHEMABRIDGE_API_LOCAL_BEARER_TOKEN \
	-u SCHEMABRIDGE_API_OIDC_JWKS_URL \
	-u SCHEMABRIDGE_INVENTORY_CURSOR_SIGNING_KEY \
	-u SCHEMABRIDGE_SEMANTIC_REGISTRY_READER_ENV_PATH \
	-u SCHEMABRIDGE_SEMANTIC_PROFILE_SOURCE_WORKSPACE_ID \
	-u SCHEMABRIDGE_SEMANTIC_PROFILE_SOURCE_CONNECTION_ID \
	-u SCHEMABRIDGE_CATALOG_DATAHUB_CREDENTIAL_BINDING_REF

JUDGE_IMAGE ?= schemabridge-judge:local
JUDGE_PLATFORM ?= linux/amd64
PUBLIC_URL ?= http://127.0.0.1:7860
SCALE_ACCEPTANCE_READER_FACTORY ?= scripts.postgres_catalog_scale_reader:create_postgres_reader
SCALE_PREFLIGHT_REPORT_JSON ?= reports/m25-scale-preflight.json
SCALE_PREFLIGHT_REPORT_MARKDOWN ?= reports/m25-scale-preflight.md
SCALE_REPORT_JSON ?= reports/m25-scale-report.json
SCALE_REPORT_MARKDOWN ?= reports/m25-scale-report.md

.PHONY: help bootstrap install check runtime-wheel-smoke format lint type test coverage coverage-unit doctor evaluate submission-package submission-package-dev release-audit release-clean judge-build judge-smoke demo-up demo-down demo-reset demo-seed-check demo-reset-proof demo-health demo-query demo-compile demo-preview demo-guard demo-governed-plan demo-governed-preview demo-intent control-plane-up control-plane-down control-plane-reset control-plane-migrate control-plane-check api worker worker-once catalog catalog-once semantic-reconciler semantic-reconciler-once semantic-reconciler-probe semantic-profile-worker semantic-profile-worker-once semantic-profile-worker-probe test-api-integration test-worker-integration test-intent test-scale-correctness benchmark-scale-preflight benchmark-scale test-integration test-acceptance datahub-version datahub-start datahub-health datahub-init-admin datahub-ingest datahub-provision-mcp datahub-provision-writer datahub-catalog-check datahub-registry-check datahub-restart datahub-reset datahub-stop datahub-mcp-check ui clean

help:
	@printf '%s\n' \
	  'make bootstrap   Create .venv and install development dependencies' \
	  'make check       Format check, lint, types, and unit tests' \
	  'make runtime-wheel-smoke Build/install the wheel and validate packaged migrations' \
	  'make coverage    Run the >=80% full-suite coverage gate (services required)' \
	  'make coverage-unit Report service-free unit coverage without release gating' \
	  'make doctor      Verify the local starter environment' \
	  'make evaluate    Reset the synthetic database and reproduce evaluation reports' \
	  'make submission-package Generate final examples from a clean release commit' \
	  'make submission-package-dev Generate explicitly non-release examples from this tree' \
	  'make release-audit Scan architecture, secrets, links, artifacts, and licenses' \
	  'make release-clean Run the strict clean-HEAD M16 release proof' \
	  'make judge-build Build the recorded-mode public judge image' \
	  'make judge-smoke Smoke-test PUBLIC_URL (default http://127.0.0.1:7860)' \
	  'make demo-up     Start synthetic PostgreSQL' \
	  'make demo-down   Stop synthetic PostgreSQL' \
	  'make demo-reset  Destroy and recreate synthetic PostgreSQL' \
	  'make demo-seed-check Verify exact rows, schema, constraints, grants, and seed hashes' \
	  'make demo-reset-proof Rebuild twice and prove an identical verified seed fingerprint' \
	  'make demo-health Verify the reader connection and safety settings' \
	  'make demo-query  Run the separated north-star reference query' \
	  'make demo-compile Compile and guard the approved north-star query plan' \
	  'make demo-preview Execute the compiled north-star preview as the reader' \
	  'make demo-guard   Run three malicious SQL guard examples' \
	  'make demo-governed-plan Inspect the resolved guided north-star plan' \
	  'make demo-governed-preview Execute the governed preview and rejection report' \
	  'make demo-intent  Preview the Spanish request through the key-free typed parser' \
	  'make control-plane-up Start the separate PostgreSQL control plane' \
	  'make control-plane-reset Recreate only the local control PostgreSQL volume' \
	  'make control-plane-migrate Apply reviewed checksum-pinned control migrations' \
	  'make control-plane-check Verify schema version, roles, and source separation' \
	  'make api         Start only the authenticated API process' \
	  'make worker      Start only the durable execution worker' \
	  'make worker-once Process at most one durable worker poll' \
	  'make catalog     Start only the DataHub-reading catalog indexer' \
	  'make catalog-once Process at most one durable catalog refresh' \
	  'make semantic-reconciler Start only the semantic-change reconciler' \
	  'make semantic-reconciler-once Process at most one semantic-change scan' \
	  'make semantic-reconciler-probe Check schema without polling or source I/O' \
	  'make semantic-profile-worker Start only the aggregate join-profile worker' \
	  'make semantic-profile-worker-once Process at most one aggregate profile' \
	  'make semantic-profile-worker-probe Check schema without source I/O' \
	  'make test-api-integration Run the real PostgreSQL API/job-store slice' \
	  'make test-worker-integration Run the real PostgreSQL worker/job-store slice' \
	  'make test-intent  Run natural-language unit and service-free acceptance tests' \
	  'make test-scale-correctness Prove synthetic preflight paging at 10/5434' \
	  'make benchmark-scale-preflight Generate synthetic 5000/16 preflight evidence' \
	  'make benchmark-scale Generate PostgreSQL 5000/16 acceptance evidence' \
	  'make test-integration  Run PostgreSQL integration tests' \
	  'make test-acceptance   Run PostgreSQL-backed acceptance tests' \
	  'make datahub-start Start pinned local DataHub Core' \
	  'make datahub-health Verify DataHub GMS, UI, auth, and logical models' \
	  'make datahub-ingest Ingest the eight synthetic PostgreSQL schemas' \
	  'make datahub-provision-mcp Create/reuse the local read-only MCP identity' \
	  'make datahub-provision-writer Create/reuse the approval-gated semantic writer' \
	  'make datahub-catalog-check Verify synthetic catalog assets and profiles' \
	  'make datahub-registry-check Verify the exact live 7/31/5 semantic registry' \
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
	$(BIN)/python -m pip install -e '.[dev,postgres,sql,ui,api,datahub,llm]'
	$(BIN)/schemabridge version
	$(BIN)/schemabridge doctor

install:
	$(BIN)/python -m pip install -e '.[dev,postgres,sql,ui,api,datahub,llm]'

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
	@SCHEMABRIDGE_TEST_DATABASE_URL='$(DEMO_DATABASE_URL)' $(BIN)/pytest --cov=schemabridge --cov-report=term-missing

coverage-unit:
	$(BIN)/pytest --cov=schemabridge --cov-report=term-missing --cov-fail-under=0 -m 'not integration and not acceptance'

check: lint type test

runtime-wheel-smoke:
	$(BIN)/python scripts/smoke_runtime_wheel.py

doctor:
	$(BIN)/schemabridge doctor

evaluate: demo-reset
	@$(MAKE) --no-print-directory demo-seed-check
	@DATABASE_URL='$(DEMO_DATABASE_URL)' $(BIN)/python -m schemabridge.entrypoints.cli.main evaluate --output reports/evaluation.json --markdown examples/evaluation-report.md

submission-package: demo-reset
	@$(MAKE) --no-print-directory demo-seed-check
	@$(BIN)/python scripts/generate_submission_package.py --database-url '$(DEMO_DATABASE_URL)'

submission-package-dev: demo-reset
	@$(MAKE) --no-print-directory demo-seed-check
	@$(BIN)/python scripts/generate_submission_package.py --database-url '$(DEMO_DATABASE_URL)' --allow-uncommitted

release-audit:
	$(BIN)/python scripts/release_audit.py --require-release --check-external

release-clean:
	bash scripts/release_clean_room.sh

judge-build:
	docker build --platform '$(JUDGE_PLATFORM)' --build-arg SCHEMABRIDGE_RELEASE_REF="$$(git describe --always --dirty)" -t '$(JUDGE_IMAGE)' .

judge-smoke:
	$(BIN)/python scripts/smoke_deployment.py --url '$(PUBLIC_URL)'

demo-up:
	docker compose -f docker-compose.demo.yml up -d --wait

demo-down:
	docker compose -f docker-compose.demo.yml down

demo-reset:
	docker compose -f docker-compose.demo.yml down -v --remove-orphans
	docker compose -f docker-compose.demo.yml up -d --wait

demo-seed-check:
	@$(BIN)/python scripts/check_demo_seed.py --database-url '$(DEMO_DATABASE_URL)'

demo-reset-proof:
	@$(MAKE) --no-print-directory demo-reset
	@first_fingerprint="$$($(BIN)/python scripts/check_demo_seed.py --database-url '$(DEMO_DATABASE_URL)' --fingerprint-only)" && \
	  $(MAKE) --no-print-directory demo-reset && \
	  second_fingerprint="$$($(BIN)/python scripts/check_demo_seed.py --database-url '$(DEMO_DATABASE_URL)' --fingerprint-only)" && \
	  test "$$first_fingerprint" = "$$second_fingerprint" && \
	  printf '%s\n' "Two clean demo resets matched: $$second_fingerprint"

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

control-plane-up:
	docker compose -f docker-compose.control.yml up -d --wait

control-plane-down:
	docker compose -f docker-compose.control.yml down

control-plane-reset:
	docker compose -f docker-compose.control.yml down -v --remove-orphans
	docker compose -f docker-compose.control.yml up -d --wait

control-plane-migrate:
	@SCHEMABRIDGE_CONTROL_MIGRATOR_DATABASE_URL='$(CONTROL_MIGRATOR_DATABASE_URL)' \
	  $(BIN)/schemabridge control-plane migrate

control-plane-check:
	@DATABASE_URL='$(DEMO_DATABASE_URL)' \
	  SCHEMABRIDGE_CONTROL_DATABASE_URL='$(CONTROL_DATABASE_URL)' \
	  SCHEMABRIDGE_CONTROL_RECONCILER_DATABASE_URL='$(CONTROL_RECONCILER_DATABASE_URL)' \
	  SCHEMABRIDGE_CONTROL_MIGRATOR_DATABASE_URL='$(CONTROL_MIGRATOR_DATABASE_URL)' \
	  SCHEMABRIDGE_CONTROL_API_DATABASE_URL='$(CONTROL_API_DATABASE_URL)' \
	  SCHEMABRIDGE_CONTROL_WORKER_DATABASE_URL='$(CONTROL_WORKER_DATABASE_URL)' \
	  SCHEMABRIDGE_CONTROL_CATALOG_DATABASE_URL='$(CONTROL_CATALOG_DATABASE_URL)' \
	  $(BIN)/schemabridge control-plane check

api:
	@$(API_CLEAN_ENV) SCHEMABRIDGE_COMPONENT=api \
	  SCHEMABRIDGE_CONTROL_PLANE_MODE=postgres \
	  SCHEMABRIDGE_CONTROL_API_DATABASE_URL='$(CONTROL_API_DATABASE_URL)' \
	  $(BIN)/schemabridge-api

worker:
	@$(WORKER_CLEAN_ENV) SCHEMABRIDGE_COMPONENT=worker \
	  SCHEMABRIDGE_CONTROL_PLANE_MODE=postgres \
	  SCHEMABRIDGE_CONTROL_WORKER_DATABASE_URL='$(CONTROL_WORKER_DATABASE_URL)' \
	  SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY='$(EXECUTION_CONNECTOR_SECRET_DIRECTORY)' \
	  SCHEMABRIDGE_REGISTRY_MODE=live \
	  SCHEMABRIDGE_SEMANTIC_REGISTRY_SELECTION=active \
	  SCHEMABRIDGE_ALLOW_LOCAL_LIVE_READS=true \
	  $(BIN)/schemabridge-worker

worker-once:
	@$(WORKER_CLEAN_ENV) SCHEMABRIDGE_COMPONENT=worker \
	  SCHEMABRIDGE_CONTROL_PLANE_MODE=postgres \
	  SCHEMABRIDGE_CONTROL_WORKER_DATABASE_URL='$(CONTROL_WORKER_DATABASE_URL)' \
	  SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY='$(EXECUTION_CONNECTOR_SECRET_DIRECTORY)' \
	  SCHEMABRIDGE_REGISTRY_MODE=live \
	  SCHEMABRIDGE_SEMANTIC_REGISTRY_SELECTION=active \
	  SCHEMABRIDGE_ALLOW_LOCAL_LIVE_READS=true \
	  $(BIN)/schemabridge-worker --once

catalog:
	@$(CATALOG_CLEAN_ENV) SCHEMABRIDGE_COMPONENT=catalog \
	  SCHEMABRIDGE_AUTH_MODE=local-demo \
	  SCHEMABRIDGE_CONTROL_PLANE_MODE=postgres \
	  SCHEMABRIDGE_CONTROL_CATALOG_DATABASE_URL='$(CONTROL_CATALOG_DATABASE_URL)' \
	  SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY='$(CATALOG_CONNECTOR_SECRET_DIRECTORY)' \
	  $(BIN)/schemabridge-catalog

catalog-once:
	@$(CATALOG_CLEAN_ENV) SCHEMABRIDGE_COMPONENT=catalog \
	  SCHEMABRIDGE_AUTH_MODE=local-demo \
	  SCHEMABRIDGE_CONTROL_PLANE_MODE=postgres \
	  SCHEMABRIDGE_CONTROL_CATALOG_DATABASE_URL='$(CONTROL_CATALOG_DATABASE_URL)' \
	  SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY='$(CATALOG_CONNECTOR_SECRET_DIRECTORY)' \
	  $(BIN)/schemabridge-catalog --once

semantic-reconciler:
	@test -n "$${SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY:-}" || { printf '%s\n' 'SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY is required for the semantic reconciler.' >&2; exit 1; }
	@$(RECONCILER_CLEAN_ENV) SCHEMABRIDGE_ENVIRONMENT=development \
	  SCHEMABRIDGE_COMPONENT=reconciler \
	  SCHEMABRIDGE_AUTH_MODE=local-demo \
	  SCHEMABRIDGE_CONTROL_PLANE_MODE=postgres \
	  SCHEMABRIDGE_CONTROL_RECONCILER_DATABASE_URL='$(CONTROL_RECONCILER_DATABASE_URL)' \
	  SCHEMABRIDGE_REGISTRY_MODE=live \
	  SCHEMABRIDGE_SEMANTIC_REGISTRY_SELECTION=active \
	  SCHEMABRIDGE_ALLOW_LOCAL_LIVE_READS=true \
	  $(BIN)/schemabridge-semantic-reconciler

semantic-reconciler-once:
	@test -n "$${SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY:-}" || { printf '%s\n' 'SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY is required for the semantic reconciler.' >&2; exit 1; }
	@$(RECONCILER_CLEAN_ENV) SCHEMABRIDGE_ENVIRONMENT=development \
	  SCHEMABRIDGE_COMPONENT=reconciler \
	  SCHEMABRIDGE_AUTH_MODE=local-demo \
	  SCHEMABRIDGE_CONTROL_PLANE_MODE=postgres \
	  SCHEMABRIDGE_CONTROL_RECONCILER_DATABASE_URL='$(CONTROL_RECONCILER_DATABASE_URL)' \
	  SCHEMABRIDGE_REGISTRY_MODE=live \
	  SCHEMABRIDGE_SEMANTIC_REGISTRY_SELECTION=active \
	  SCHEMABRIDGE_ALLOW_LOCAL_LIVE_READS=true \
	  $(BIN)/schemabridge-semantic-reconciler --once

semantic-reconciler-probe:
	@test -n "$${SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY:-}" || { printf '%s\n' 'SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY is required for the semantic reconciler.' >&2; exit 1; }
	@$(RECONCILER_CLEAN_ENV) SCHEMABRIDGE_ENVIRONMENT=development \
	  SCHEMABRIDGE_COMPONENT=reconciler \
	  SCHEMABRIDGE_AUTH_MODE=local-demo \
	  SCHEMABRIDGE_CONTROL_PLANE_MODE=postgres \
	  SCHEMABRIDGE_CONTROL_RECONCILER_DATABASE_URL='$(CONTROL_RECONCILER_DATABASE_URL)' \
	  SCHEMABRIDGE_REGISTRY_MODE=live \
	  SCHEMABRIDGE_SEMANTIC_REGISTRY_SELECTION=active \
	  SCHEMABRIDGE_ALLOW_LOCAL_LIVE_READS=true \
	  $(BIN)/schemabridge-semantic-reconciler --probe-ready

semantic-profile-worker:
	@$(PROFILE_WORKER_CLEAN_ENV) SCHEMABRIDGE_ENVIRONMENT=development \
	  SCHEMABRIDGE_COMPONENT=worker \
	  SCHEMABRIDGE_AUTH_MODE=local-demo \
	  SCHEMABRIDGE_CONTROL_PLANE_MODE=postgres \
	  SCHEMABRIDGE_CONTROL_WORKER_DATABASE_URL='$(CONTROL_WORKER_DATABASE_URL)' \
	  SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY='$(PROFILE_CONNECTOR_SECRET_DIRECTORY)' \
	  SCHEMABRIDGE_WORKER_IDENTITY_LINEAGE_MODE=exact-local \
	  $(BIN)/schemabridge-semantic-profile-worker

semantic-profile-worker-once:
	@$(PROFILE_WORKER_CLEAN_ENV) SCHEMABRIDGE_ENVIRONMENT=development \
	  SCHEMABRIDGE_COMPONENT=worker \
	  SCHEMABRIDGE_AUTH_MODE=local-demo \
	  SCHEMABRIDGE_CONTROL_PLANE_MODE=postgres \
	  SCHEMABRIDGE_CONTROL_WORKER_DATABASE_URL='$(CONTROL_WORKER_DATABASE_URL)' \
	  SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY='$(PROFILE_CONNECTOR_SECRET_DIRECTORY)' \
	  SCHEMABRIDGE_WORKER_IDENTITY_LINEAGE_MODE=exact-local \
	  $(BIN)/schemabridge-semantic-profile-worker --once

semantic-profile-worker-probe:
	@$(PROFILE_WORKER_CLEAN_ENV) SCHEMABRIDGE_ENVIRONMENT=development \
	  SCHEMABRIDGE_COMPONENT=worker \
	  SCHEMABRIDGE_AUTH_MODE=local-demo \
	  SCHEMABRIDGE_CONTROL_PLANE_MODE=postgres \
	  SCHEMABRIDGE_CONTROL_WORKER_DATABASE_URL='$(CONTROL_WORKER_DATABASE_URL)' \
	  SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY='$(PROFILE_CONNECTOR_SECRET_DIRECTORY)' \
	  SCHEMABRIDGE_WORKER_IDENTITY_LINEAGE_MODE=exact-local \
	  $(BIN)/schemabridge-semantic-profile-worker --probe-ready

test-api-integration:
	@SCHEMABRIDGE_TEST_DATABASE_URL='$(DEMO_DATABASE_URL)' \
	  $(BIN)/pytest -m integration \
	  tests/integration/test_background_jobs_postgres.py \
	  tests/acceptance/test_authenticated_api_socket_acceptance.py

test-worker-integration:
	@SCHEMABRIDGE_TEST_DATABASE_URL='$(DEMO_DATABASE_URL)' \
	  $(BIN)/pytest -m integration \
	  tests/integration/test_background_jobs_postgres.py \
	  tests/integration/test_m24_identity_lineage_postgres.py \
	  tests/integration/test_m24_process_lifecycle.py

test-intent:
	$(BIN)/pytest tests/unit -k 'intent or language or prompt'
	$(BIN)/pytest -m acceptance -k natural_language

test-scale-correctness:
	@PYTHONHASHSEED=0 $(BIN)/pytest -q \
	  tests/unit/test_scale_harness.py \
	  tests/unit/test_postgres_scale_reader.py
	@PYTHONHASHSEED=0 $(BIN)/pytest -q -m scale tests/unit/test_lazy_synthetic_catalog.py
	@PYTHONHASHSEED=0 $(BIN)/python -m scripts.benchmark_catalog_scale \
	  --mode correctness \
	  --output-json '$(SCALE_PREFLIGHT_REPORT_JSON)' \
	  --output-markdown '$(SCALE_PREFLIGHT_REPORT_MARKDOWN)'

benchmark-scale-preflight:
	@PYTHONHASHSEED=0 $(BIN)/python -m scripts.benchmark_catalog_scale \
	  --mode benchmark \
	  --read-count 5000 \
	  --concurrency 16 \
	  --load-page-size 17 \
	  --output-json '$(SCALE_PREFLIGHT_REPORT_JSON)' \
	  --output-markdown '$(SCALE_PREFLIGHT_REPORT_MARKDOWN)'

benchmark-scale:
	@PYTHONHASHSEED=0 $(BIN)/python -m scripts.benchmark_catalog_scale \
	  --mode benchmark \
	  --read-count 5000 \
	  --concurrency 16 \
	  --load-page-size 17 \
	  --reader-factory '$(SCALE_ACCEPTANCE_READER_FACTORY)' \
	  --output-json '$(SCALE_REPORT_JSON)' \
	  --output-markdown '$(SCALE_REPORT_MARKDOWN)'

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

datahub-registry-check:
	@SCHEMABRIDGE_CATALOG_MODE=live SCHEMABRIDGE_REGISTRY_MODE=live \
	  SCHEMABRIDGE_ALLOW_LOCAL_LIVE_READS=true \
	  $(BIN)/python scripts/check_datahub_registry.py

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
