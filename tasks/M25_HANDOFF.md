# Milestone handoff

## Summary

- Milestone: M25 — Scale, indexing, pagination, and tenant capacity
- Status: complete; accepted locally
- Recommended operator decision: accept M25 as the local productionization baseline and keep the
  global production/release decision at NO-GO
- Proposed commit message: `feat: add tenant-scoped dynamic catalog indexing and pagination`

## Implemented

- Added tenant/workspace/connection-qualified catalog identities, durable capacity policy,
  refresh/generation lifecycle, bounded source changes, and keyset page contracts without changing
  the SQL-facing `PhysicalDatasetRef`.
- Added control schema v4 and the sixth `schemabridge_catalog` role. Public API operations, private
  route lookup, refresh ownership, generation writes, and pruning use separate least-privilege
  database capabilities.
- Added authenticated connection/asset/field/refresh HTTP operations with strict bounded models,
  `platform_admin` mutations, tenant-scoped reads, distributed rate admission, and one safe cursor
  failure boundary.
- Added HMAC-authenticated, 15-minute, generation-bound keyset cursors and PostgreSQL list queries
  using compound keys plus `LIMIT page_size + 1`; interactive pages return at most 50 items.
- Added atomic full/delta refresh staging, durable checkpoints, database-time leases, monotonic
  fencing, terminal-page resume, compare-and-swap promotion, immutable tombstones, retention, and
  server-side count/quota/fingerprint verification.
- Added a mutation-free DataHub GraphQL scroll source and a lazy heterogeneous synthetic source.
  The live DataHub scope uses an exact dataset-URN prefix; DataHub is honestly labeled full
  reconciliation and never falls back to the synthetic source.
- Added one bounded lifecycle-managed PostgreSQL pool per API, worker, and catalog-indexer process,
  transactional tenant job admission, fair workspace claim rotation, and sanitized saturation.
- Added the independent `schemabridge-catalog` process with readiness-only, `--once`, continuous,
  and cooperative shutdown modes.
- Added dynamic scale fixtures for 10 and 5,434 assets across connections, more than 40,000 varied
  fields, page-size 1/17/50 traversal, local memory/load measurements, and reviewed keyset plans.
  Every 997th large-fixture asset is deliberately wide with 64 fields, including nested paths,
  Unicode names, heterogeneous native types, definitions, null/key variation, and tags.
- Added a version-checked tenant-policy operator command backed by a migrator-only
  `SECURITY DEFINER` function and an immutable revision ledger. Connection, asset, field, request,
  job, cursor-retention, and generation-retention policy can change per workspace without a code
  release or schema migration.
- Included field tags and glossary terms in the stored PostgreSQL full-text search document and
  GIN index, so M27 can retrieve them as bounded candidate evidence rather than scanning all
  fields.
- Preserved the independent one-connection, three-table, two-join query boundary and the existing
  deterministic compiler, AST guard, fanout, row-limit, timeout, and read-only source controls.
- Added a bounded same-origin browser-acceptance panel for the small, large, and live-DataHub
  tenants. It is test instrumentation, not the production Query Studio.
- Added a hardened two-replica Kubernetes catalog-indexer workload with a dedicated control DSN
  and DataHub read token, pod-name lease identity, schema-only startup/readiness probes, process
  liveness, non-root execution, dropped capabilities, and a read-only root filesystem.
- M25 made no OpenAI request and does not inject `OPENAI_API_KEY` into API, worker, or indexer.

## Files changed

- `src/schemabridge/domain/catalog_inventory.py`: pure inventory, capacity, generation, refresh,
  change, and page values.
- `src/schemabridge/application/catalog_inventory.py`,
  `src/schemabridge/application/catalog_indexer.py`, and
  `src/schemabridge/application/api_capacity.py`: catalog/capacity use cases.
- `src/schemabridge/application/ports/catalog_inventory.py`: public, private-route, source,
  refresh-generation, cursor, and capacity ports.
- `src/schemabridge/adapters/control_plane/`: schema-v4 PostgreSQL stores, pool, migrations, and
  role-scoped capabilities.
- `src/schemabridge/adapters/catalog/`: lazy synthetic and mutation-free DataHub scroll sources.
- `src/schemabridge/entrypoints/http/` and `src/schemabridge/entrypoints/catalog/`: authenticated
  catalog API and independently operated indexer.
- `migrations/control_plane/0004_dynamic_catalog_inventory.sql`: additive schema-v4 migration.
- `deploy/kubernetes/m24-runtime.yaml`: independent hardened API, worker, and catalog-indexer
  workload examples with bounded replicas and distinct credentials.
- `scripts/benchmark_catalog_scale.py`, `scripts/postgres_catalog_scale_reader.py`, and
  `reports/m25-scale-report.{json,md}`: reproducible local scale evidence.
- `scripts/m25_catalog_browser_panel.py`: server-side-token acceptance instrumentation.
- `tests/unit/test_catalog_*.py`, `tests/unit/test_capacity_limits.py`,
  `tests/unit/test_control_plane_pool.py`, `tests/unit/test_http_catalog_api.py`, and related
  integration/acceptance tests: adversarial contracts and service evidence.
- `docs/adr/0011-dynamic-catalog-inventory.md` and the M25 architecture, security, query, test,
  runbook, deployment, UI, browser, state, decision, and plan documents: implemented contract,
  measured evidence, final coverage gate, and local acceptance record.

## Commands executed

The following results were produced against the final integrated M25 code state.

| Command | Result | Notes |
|---|---|---|
| `shasum -a 256 migrations/control_plane/0004_dynamic_catalog_inventory.sql` | pass | `45c7d95e56a336f267cbe29ad390f6fd54a59d826ffd33253d290be0257060bb` |
| `shasum -a 256 reports/m25-scale-report.json reports/m25-scale-report.md` | pass | JSON `ee9cc46f3e0a9e3c7ede11d8f49dee89fccb4556febc87fab6193e96bca38c9a`; Markdown `6f03ea8a8719dd6829a3f04a22ddd91fe5c48267adc0457664980ceb315d3eca` |
| `make control-plane-reset && make control-plane-migrate && make control-plane-check` | pass | All six roles reported expected/current v4, no pending migration, and verified source/control database separation. |
| Focused catalog/storage/DataHub/pool/API/query-safety tests | pass | Focused adversarial cuts passed throughout implementation. |
| `make benchmark-scale` | pass | Operated PostgreSQL evidence profile; exact values below. |
| `make test-scale-correctness` | pass | 20 tests in 11.16 s; PostgreSQL scale cut 8 passed/4 deselected in 6.37 s; synthetic report PASS. |
| `make test-api-integration` | pass | 18 passed in 44.97 s. |
| `make test-worker-integration` | pass | 24 passed in 38.12 s. |
| `make test-integration` | pass | 111 passed/1,427 deselected in 145.02 s; six expected DataHub idempotent `IngestionAttributionWarning` warnings. |
| `make test-acceptance` | pass | 19 passed/1,519 deselected in 56.04 s; one expected DataHub idempotent attribution warning. |
| `make evaluate` | pass | Deterministic evaluation completed over 11 tables/465 rows, seed SHA-256 `487388495115265d5fac5a17675e05c236fd4eb4c430613ef02050a5f6010654`; `live_llm=not_run`. |
| `make check` | pass | Final post-fixture rerun: Ruff format 362 files; Ruff lint 0 findings; mypy 194 source files/0 errors; pytest 1,412 passed/126 deselected, 0 warnings, in 83.29 s. |
| `make coverage` | pass | Clean run exit 0: 1,538 collected/passed; 0 failed/skipped/deselected; 7 warnings; 81.98% versus 80% threshold; pytest 316.33 s, real 319.02 s, user 130.54 s, sys 9.89 s. |
| `make runtime-wheel-smoke` | pass | Clean wheel smoke discovered packaged control migrations 1–4 outside the checkout. |
| `.venv/bin/python scripts/release_audit.py` | pass | 555 candidate files, 23 direct-dependency licenses, 0 external links checked; only expected `release_tree_dirty` warning. |
| `git diff --check` | pass | Passed after the final coverage and milestone-state consolidation. |

## Automated test results

- Focused tests: scale correctness passed 20 tests plus the 8-pass/4-deselected PostgreSQL cut and
  the synthetic PASS report.
- `make check`: passed Ruff format over 362 files, Ruff lint with zero findings, strict mypy over
  194 source files with zero errors, and 1,412 pytest tests with 126 deselected.
- Integration tests: 111 passed with six expected DataHub attribution warnings; API and worker
  subsets passed 18 and 24 respectively.
- Acceptance tests: 19 passed with one expected DataHub attribution warning; the scale report and
  complete desktop/mobile internal-browser record also passed.
- Coverage: 1,538 passed with seven known warnings and no failed, skipped, or deselected tests;
  total 25,387 statements/3,668 missed and 6,608 branches/1,705 partial branches, for 81.98%
  against the 80% threshold. Sixty-nine completely covered files were omitted from detailed
  output. Socket acceptance passed 4/4 and M24 process lifecycle passed 4/4 inside this gate.

### Resolved coverage-gate instability

Two earlier full coverage attempts failed because artificial lease durations in slow
coverage-instrumented tests were shorter than their own workload. Both failures were correct
fail-closed lease-expiry behavior, not a runtime policy bypass. Only test fixtures changed:

- the M24 recovered-owner process fixture moved its named recovered lease from 2 to 60 seconds;
  the first-expiry lease remains 4 seconds and the explicit stale-owner probe remains 2 seconds;
- socket acceptance now uses the runtime-default 120-second lease for both the worker and its
  cooperative claim.

Focused normal/coverage cuts and the acceptance-file coverage cut passed before the clean full
rerun. The final zero-failure run above includes all four socket acceptance tests and all four M24
lifecycle tests. Runtime lease defaults and fail-closed production behavior were not weakened.

### Operated local scale result

`reports/m25-scale-report.json` was measured on Darwin 25.5.0 arm64, CPython 3.13.13,
PostgreSQL 16.13, 10 logical CPUs, and 16 GiB physical memory. It is local regression evidence,
not a production SLO.

Current artifact SHA-256 values are
`ee9cc46f3e0a9e3c7ede11d8f49dee89fccb4556febc87fab6193e96bca38c9a` for JSON and
`6f03ea8a8719dd6829a3f04a22ddd91fe5c48267adc0457664980ceb315d3eca` for Markdown.

| Measure | Observed | Local budget/result |
|---|---:|---:|
| Small inventory | 10 assets / 75 fields | exact |
| Large inventory | 5,434 assets / 41,028 fields | exact assets; fields >= 40,000 |
| Active large-tenant connections | 2 | multi-connection observed |
| Page size 1 | 5,434 pages; max 2 rows read / 1 returned | pass |
| Page size 17 | 320 pages; max 18 rows read / 17 returned | pass |
| Page size 50 | 109 pages; max 51 rows read / 50 returned | pass |
| Full refresh | 110 persisted pages / 29.685331 s | <= 60 s |
| Python heap delta | 126,601 bytes | <= 16 MiB |
| Process RSS delta | 0 bytes | <= 64 MiB |
| Indexed reads | 5,000 at concurrency 16 | 0 unexpected errors |
| Latency p50 / p95 / p99 / max | 26.5 / 41.762 / 54.951 / 81.658 ms | p95 <= 250 ms; p99 <= 500 ms |
| Pool wait p95 / p99 | 12.266 / 21.651 ms | measured, 5,000 observations |

Asset and field keyset plans used the expected indexes under the default PostgreSQL planner with
no planner override. The field probe selected a naturally wide 64-field synthetic asset and both
reviewed plans contained `Index Scan` plus `Limit`, with no forbidden plan node.

## Operator manual test

The following procedure passed through Codex's internal browser on 2026-07-23:

1. Start the exact schema-v4 control plane, signed-JWT provider, API, and catalog-indexer processes
   with separate role credentials and no OpenAI key.
2. In Codex's internal browser, load the small tenant and traverse its 10 assets and fields.
3. Load the large tenant and inspect first, middle, and final pages of all 5,434 assets without
   downloading a full list.
4. Exercise bit-tampered, changed-filter, cross-tenant/cross-connection, and genuinely expired
   cursors. All must return the same `inventory_cursor_unavailable` boundary.
5. Set a three-request policy and show request four returning `429` with bounded `Retry-After`;
   restore the normal policy afterward.
6. Load the live DataHub tenant, show the completed 11-asset/59-field PostgreSQL generation, stop
   DataHub, show stale-but-readable inventory, and show a new refresh failing safely as
   `source_unavailable` without fallback.
7. Repeat at 390x844 and desktop. Verify no horizontal document overflow, no warning/error console
   entries, and zero protected-data hits.

Observed result:

```text
desktop: 1440x900; client/scroll/body widths 1440; no overflow
small: generation 1; 10 assets / 75 fields
small page_size=1: pages 1, 5, and 10 observed; final hasNext=false
large secondary: 0 assets / 0 fields
large primary: 5,434 assets / 41,028 fields
large page_size=50: page 1=50, page 55=50, page 109=34; final hasNext=false
retained pages: bounded at 64
tampered / changed-filter / cross-alias cursors: 404 inventory_cursor_unavailable
early expiry attempt: 409 cursor_not_expired
genuine expiry: issued 21:41:07Z; queried 21:56:43Z; 404 inventory_cursor_unavailable
rate policy: 3/minute; fourth request 429 with Retry-After=41; restored to 10,000
description search: "leading zeroes" on synthetic-asset-00000996 (64 fields)
description match: exactly contract_id with its definition, type, and tag
DataHub fresh: generation 1; 11 assets / 59 fields
DataHub stopped: stale but readable; bank.account_holders remained visible
failed refresh: refresh-00359c5c1c534e018cc71e0c8a82f22b; source_unavailable; 0 pages
post-failure: active generation 1 unchanged; no fallback; source restarted healthy
mobile iframe: 390x844; client/scroll/body/main widths 390; no overflow
mobile inventory: small 10/75; large 5,434/41,028; first page 50 with hasNext=true
protected-data scan: zero hits for every named secret pattern
console: monitored action produced no warning/error event
```

## Architecture and security review

- Dependency direction: the new domain is I/O-free; application code depends on ports; PostgreSQL,
  DataHub, FastAPI, Psycopg, and process concerns remain adapters/entrypoints composed only in
  `bootstrap.py`.
- Source database writes: none. The catalog source is metadata-read-only; governed execution keeps
  the independent source reader, read-only transaction, allowlist, timeout, and row cap.
- SQL/LLM validation: inventory metadata never becomes executable SQL. M25 invokes no LLM; a
  future M27 match still has to produce typed intent and obtain semantic approval.
- DataHub mutation approval: the catalog source exposes no mutation method. M25 performs full
  reconciliation reads only and does not write DataHub.
- Secrets/proprietary data: public/catalog state and the scale report omit route bindings,
  credentials, claims, SQL, parameters, source rows, samples, prompts, and OpenAI material.
- Fanout/semantic risks: a name or definition is candidate context only. Inventory cardinality does
  not authorize a mapping, join, or query above three tables/two joins.

## Decisions made

- Decision: scope live DataHub discovery with an exact dataset-URN `START_WITH` prefix and stable
  URN scroll order, not dataset-name prefix matching.
- Reason: the operated DataHub GraphQL endpoint returned the complete 11-asset synthetic scope for
  the URN prefix while the name-prefix form returned zero; URN also binds platform and environment
  without broad search.
- Logged in: `tasks/DECISION_LOG.md` D081.

- Decision: compute asset identity/fingerprint independently from fields and compute each field
  fingerprint from its complete persisted metadata.
- Reason: field evolution must not rewrite asset identity, while definitions/tags/terms/type flags
  must remain observable as field changes.
- Logged in: `tasks/DECISION_LOG.md` D082.

- Decision: use narrow fixed-`search_path` `SECURITY DEFINER` operations for API refresh
  request/status and catalog private-route lookup instead of direct table grants.
- Reason: API must not read arbitrary refresh rows and the indexer must not enumerate private
  routes; ownership, lease, fence, and transient capability checks belong in the database
  capability boundary.
- Logged in: `tasks/DECISION_LOG.md` D083.

- Decision: apply workspace capacity through an optimistic, exactly confirmed operator command
  that records every version in an immutable revision ledger.
- Reason: dynamic tenant sizing needs an operable production path; direct ad-hoc policy writes
  would lose actor/version evidence and could race another change.
- Logged in: `tasks/DECISION_LOG.md` D084.

## Known limitations or unverified items

- The current tree is dirty and is neither a release commit nor production evidence.
- Scale values are one-machine local regression measurements, not availability, autoscaling,
  multi-region, capacity, or latency SLOs.
- One catalog-indexer process currently receives one global DataHub server/token binding.
  Per-connection credential resolution and governed connector routing remain M28 work.
- Active-generation promotion recomputes counts and the catalog fingerprint in O(N) database work.
  It is bounded away from API memory, but its larger-scale operational curve is not yet measured.
- Tenant policy permits up to 100,000,000 assets, but the operated load proof stops at 5,434
  assets; the safety ceiling is not a performance claim.
- Asset and field search GIN indexes are global rather than tenant-prefixed. PostgreSQL still
  applies exact workspace/connection/generation predicates, but production tenant-distribution
  tuning remains unmeasured.
- `QueryPlan` has no catalog connection identifier. Execution is structurally single-source and
  cross-connection planning is rejected, but an explicit catalog-to-plan connection binding and
  federation contract remain M28 work.
- `platform_instance`, `database_name`, and `normalized_type` are reserved schema-v4 columns and
  remain `NULL`/unadvertised in M25.
- Cooperative indexer shutdown waits for the current bounded source call; source timeout is the
  final upper bound. A lost lease/capability/fence fails closed and recovery occurs after expiry.
- The browser panel is acceptance instrumentation. M27 owns catalog-driven Query Studio controls
  and short-description matching.
- M25 did not exercise the user's existing OpenAI key. M27 must use a separately configured cheap
  structured-output model and keep ambiguity human-governed.

## Blockers

- None for local M25 acceptance.
- Global production/release remains NO-GO: the dirty tree is not a reviewed release commit, and
  operated Kubernetes/TLS/NetworkPolicy/external secrets, production traffic/SLOs, supply-chain
  hardening, production security verification, pilot operation, and release sign-off remain later
  milestones.

## Next milestone readiness

- Dependencies satisfied: yes for M26 planning.
- Recommended next prompt: create and review the M26 join-drift and semantic-change-management
  plan/prompt after the repository required reading; neither exists yet in this tree.
- Required operator prerequisites: retain the accepted M25 evidence and do not begin M27 until M26
  is separately implemented and accepted.
