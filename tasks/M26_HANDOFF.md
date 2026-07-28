# Milestone handoff

## Summary

- Milestone: M26 — Join drift and semantic change management
- Status: complete; accepted locally on 2026-07-24
- Recommended operator decision: proceed to M27; global production/release remains NO-GO
- Proposed commit message: `feat: add governed semantic drift and blast-radius controls`

## Implemented

- Immutable exact governed-resource bindings across dynamic tenant catalogs. Each mapping binds
  workspace, catalog scope, registry, connection, asset, field path, generation, mapping
  decision/version, and metadata fingerprints; name or definition similarity never establishes
  equivalence.
- Deterministic metadata and aggregate join-change classification with current,
  review-required, blocked, revalidated, rejected, and superseded states.
- Mapping/join/workflow/query-recipe impact model with dependency completeness, watermark,
  deduplicated counts, and impact-set fingerprint. Same-scope stale artifacts project
  conservatively to exact current logical fields/contracts; cross-scope or unresolvable artifacts
  make coverage incomplete and blocking.
- Registry source identity may change between immutable v1 and v2 documents. The same exact scope
  fingerprint lets stale v1 workflows/recipes project conservatively to v2 dependencies;
  missing/cross-scope identity still makes coverage incomplete.
- Exact establish-baseline, compatible-revalidation, and rejection prepare/approve/commit flow
  with actor/time/confirmation/report/pointer/catalog/dependency/head binding, immutable history,
  compare-and-swap, and exact replay.
- Additive schema-v5 candidate with immutable generation changes, bindings, aggregate profiles,
  reports, findings, impacts, resolutions, dependencies, heads, scoped scan queue, and
  workspace/connection-qualified aggregate profile queue.
- Catalog-generation scan fan-out to every matching active registry scope and registry-transition
  scans for their exact pointer generation. Claims use database time, transient capability
  digests, leases, fences, retry/supersession, and immutable terminal state.
- Paginated workflow and scope-qualified DataHub recipe dependency reads with at-most-50 pages,
  stable source identities, complete/incomplete manifests, and continuation checks around every
  page so lease loss cannot advance the index. One atomic snapshot is capped at 10,000
  artifacts/100,000 edges and persists PostgreSQL edges in batches of 500.
- DataHub query-recipe current/version identities include the active semantic scope fingerprint
  prefix; publication, current lookup, inventory, approval/audit, and version read-back validate
  the full scope.
- Aggregate join profiles are queued with exact workspace and `connection_id`. The reconciler
  enqueues the complete required batch before waiting; a worker claims/reclaims only its configured
  pair, revalidates it before heartbeat/source I/O, and persists aggregate-only read-only evidence.
- Exact catalog observation uses fixed-`search_path`, `SECURITY DEFINER`
  `load_semantic_bound_catalog_evidence` over the bounded workspace/scope/connection/asset/field
  locator array plus a separate connection-generation projection; it does not materialize every
  field in the connection.
- Initial binding review uses fixed-`search_path`, `SECURITY DEFINER`
  `load_semantic_initial_catalog_candidates`. It resolves length-prefixed asset/field locator keys
  through `catalog_assets_semantic_lookup_idx` and unique
  `catalog_fields_semantic_lookup_idx`, then rechecks every raw workspace/connection/generation/
  qualified-name/asset/field value outside the equality-only lateral probes. It accepts at most
  2,000 exact six-key requests/2 MB, requires contiguous ordinals and all-null/all-present selected
  locators, and returns at most two witnesses per mapping. `PUBLIC` is revoked; only the reconciler
  executes the main security-definer lookup, while the catalog role alone may execute the locator
  helpers and the migrator remains their owner.
- Dependency-aware PostgreSQL gate over live catalog evidence, exact live dependency state, latest
  affected report, and current join safety. Runtime and worker
  require exactly one current row per selected dependency and one common connection before
  compiler, preview, rejected-source, or source I/O. Accepted baselines use the resulting head
  revision. Unrelated drift remains eligible.
- Tenant-scoped read-only authenticated report/detail/finding/impact HTTP pages with signed bounded
  keyset cursors and indistinguishable protected denial.
- Separate semantic reconciler, aggregate profile worker, exact operator CLI, schema-only probes,
  graceful process boundaries, Kubernetes reference, and loopback-only read-only acceptance panel.
- M26 makes no OpenAI request, executes no LLM SQL, writes no source data, and performs no DataHub
  mutation.

## Files changed

- `src/schemabridge/domain/semantic_change.py`: bindings, observations, baselines, reports,
  findings, impacts, decisions, and gate contracts.
- `src/schemabridge/domain/semantic_change_scans.py`: durable scoped scan lifecycle, leases,
  fencing, retry, and supersession.
- `src/schemabridge/domain/semantic_profile_jobs.py`: workspace/connection-qualified aggregate
  profile jobs.
- `src/schemabridge/application/semantic_change*.py`: inspect/decision/gate/operator/reconciler use
  cases.
- `src/schemabridge/application/semantic_dependency_reconciler.py`: paginated complete dependency
  reconciliation with lease continuation.
- `src/schemabridge/application/semantic_profile_worker.py`: one-workspace/connection
  aggregate-only worker.
- `src/schemabridge/config.py`, `src/schemabridge/bootstrap.py`, `.env.example`, `Makefile`:
  mandatory non-secret profile workspace route plus connection route composition.
- `src/schemabridge/application/ports/semantic_*.py`: narrow evidence, dependency, scan, profile,
  read, and gate ports.
- `src/schemabridge/adapters/semantic_change/`: PostgreSQL stores, projections, cursors,
  dependencies, scans, profiles, and runner adapters.
- `src/schemabridge/adapters/datahub/query_recipes.py`,
  `src/schemabridge/adapters/datahub/recipe_inventory.py`: scope-qualified recipe identity and
  bounded dependency inventory.
- `src/schemabridge/application/governed_execution.py`,
  `src/schemabridge/application/query_recipes.py`: pre-I/O gate and scoped recipe reuse.
- `src/schemabridge/entrypoints/semantic_change/`,
  `src/schemabridge/entrypoints/semantic_reconciler/`,
  `src/schemabridge/entrypoints/semantic_profile_worker/`: isolated CLIs/processes; semantic-change
  help parses before runtime composition.
- `migrations/control_plane/0005_semantic_change_management.sql`: additive schema v5, exact bound
  and initial-candidate catalog-evidence functions/indexes, minimized generation projection,
  live-index gate, and six-role privileges.
- `scripts/m26_semantic_change_browser_panel.py`: loopback-only read-only acceptance
  instrumentation.
- `scripts/m26_browser_acceptance_runtime.py`: owner-only real-seed prepare/API/panel/status/cleanup
  lifecycle for the final browser record.
- `tests/unit/test_m26_browser_acceptance_runtime.py`,
  `tests/unit/test_m26_semantic_change_browser_panel.py`,
  `tests/acceptance/test_semantic_change_acceptance.py`: helper isolation and the retained real
  browser-state seed.
- `tests/unit/test_semantic_change_schema_migration.py`,
  `tests/integration/test_semantic_change_postgres.py`,
  `tests/integration/test_dynamic_catalog_schema_postgres.py`: six-role, fail-closed locator, cold
  statistics-independent plan, and exact-index regressions.
- `examples/query-recipe-secondary-holders.yml`, `demo/ground_truth/evaluation.yml`, and their
  evaluation tests: governed recipe v3 regenerated from the current typed plan fingerprint.
- `deploy/kubernetes/m24-runtime.yaml`, `deploy/kubernetes/README.md`: structural M26 workloads and
  secret separation.
- `plans/M26_JOIN_DRIFT_CHANGE_MANAGEMENT.md`,
  `docs/adr/0012-governed-semantic-change-management.md`: authoritative milestone and lasting
  decisions.
- `docs/01_SCOPE.md`, `docs/02_ARCHITECTURE.md`, `docs/03_DOMAIN_MODEL.md`,
  `docs/04_DATAHUB_INTEGRATION.md`, `docs/05_QUERY_PIPELINE.md`, `docs/06_SECURITY.md`,
  `docs/07_TEST_STRATEGY.md`, `docs/12_RUNBOOK.md`, `docs/13_UI_SPEC.md`,
  `docs/14_BROWSER_ACCEPTANCE.md`, `docs/14_DEPLOYMENT.md`: M26 behavior and operator contract.
- `tasks/PROJECT_STATE.md`, `tasks/DECISION_LOG.md`, `tasks/CURRENT_TASK.md`,
  `tasks/WORK_QUEUE.md`: durable accepted state and M27 eligibility.

## Commands executed

Final technical results are consolidated below. Failed attempts remain recorded beside their clean
reruns. The operator accepted the locally passing milestone; this is not a production/release GO.

| Command | Result | Notes |
|---|---|---|
| Workspace/connection profile focused cut | passed | 43 unit + 6 PostgreSQL integration |
| Browser helper/panel/schema focused cut | passed | 52 passed in 3.74s |
| M26 PostgreSQL focused integration | passed | 2 passed in 16.20s |
| Failed-integration correction cut | passed | 2 passed in 6.95s; directed Ruff format/check passed |
| Recipe-v3 targeted cut | failed on first attempt | 3 failed, 4 passed in 2.90s; strict manifest/test version still expected v2 |
| Recipe-v3 targeted clean rerun | passed | 7 passed in 7.99s after manifest and unit expectation advanced to v3 |
| `make test-acceptance` second attempt | failed | 1 failed, 19 passed, 1,745 deselected, 1 warning in 91.63s; unbounded initial-candidate lookup hit its 5s timeout |
| Isolated real M26 acceptance after timeout | passed but non-closing | 1 passed in 104.03s; confirms intermittent query shape, not final acceptance |
| Locator-first initial-candidate migration cut | passed | 12 unit migration tests in 0.20s |
| Locator-first exact/fail-closed E2E cut | passed | 1 passed in 11.97s after strict request validation |
| Locator-first ACL targeted cut | passed before final hardening | 1 passed; must rerun on final migration bytes |
| Locator-first final ACL cut | passed | Six-role targeted test: 1 passed in 1.20s |
| Locator-first real scale/acceptance cut | passed on first warmed run, later invalidated | 5,434 tables, second inspect, and plan assertions: 1 passed in 126.89s; not final evidence |
| Locator-first clean-base scale rerun | failed | 1 failed in 22.82s; 59,615 touched blocks exceeded the 4,096 bound |
| Locator-first retained reproduction | failed | 16,333 touched blocks exceeded the 4,096 bound |
| Locator-first field-phase reproduction | failed | 54,975 touched blocks; field probe filtered about 5,457 fields for each of 31 mappings |
| Final statistics-independent cold acceptance | passed | Autovacuum off, both relation estimates unknown: 1 passed in 126.66s |
| Final locator schema/dependency unit cut | passed | 23 tests |
| Independent cold-audit second run | non-closing failure/error | Scale assertion passed, then database was removed externally; run ended after 54.35s with `AdminShutdown` |
| Locator-first directed Ruff | passed | Format/check over 7 changed implementation/test files |
| `make type` after locator fix | passed | Strict mypy over 225 source files |
| Directed Ruff lint/format and `git diff --check` | passed | Profile-binding slice |
| Helper/integration directed Ruff, strict mypy, and `git diff --check` | passed | Browser-runtime slice |
| `mypy src` | passed | 225 source files at the focused snapshot |
| Required full focused unit cut | passed | 190 passed in 6.39s |
| `make control-plane-reset` | passed on current locator bytes | Included in the 11.72s final schema sequence |
| `make control-plane-migrate` | passed on current locator bytes | Exact schema v5 and migrations 0001–0004 unchanged |
| `make control-plane-check` | passed on current locator bytes | Six credentials current/expected v5, pending none, source/control separation verified |
| Current-byte reset/migrate/check | passed | 11.72s; v5 current for runtime/reconciler/migrator/API/worker/catalog and source/control separation verified |
| Migration-v5 checksum | passed | SHA-256 `8e311f27c881844a1c67a0281391ddc1eb7b863f1d2f8edff382933397412285` |
| M26 PostgreSQL report integration | passed | 2 passed in 15.90s |
| M26 scan/profile integration | passed | 12 passed in 87.00s |
| M26 real PostgreSQL/DataHub acceptance | passed | 1 passed in 260.31s; real/user/sys 262.27/13.70/2.26s |
| Browser retained-state `prepare` | passed | 1 passed in 116.95s |
| Internal-browser real-upstream record | passed | Desktop 1440x900 and mobile 390x844; four states, bounded pages, same-boundary denials, clean console, no overflow/protected data/mutations |
| Browser helper exact cleanup | passed | Dedicated database/state removed; 8510/8520 have no listeners |
| psycopg cleanup verification | passed | Retained browser database count 0 |
| Secondary `psql` cleanup verification | unavailable | `psql` was not installed (`exit 127`); recorded psycopg check replaced it |
| `make test-api-integration` | passed | 18 passed in 34.32s |
| `make test-worker-integration` | passed | 24 passed in 39.67s |
| `make test-integration` | failed on first integrated attempt | 2 failed, 126 passed, 1,637 deselected, 6 warnings in 241.71s; later corrected and rerun below |
| `make test-integration` clean rerun | passed | 128 passed, 1,637 deselected, 6 expected DataHub `IngestionAttributionWarning` warnings in 248.82s |
| `make test-integration` final-byte rerun | passed | 128 passed, 1,638 deselected, 6 expected DataHub upsert warnings in 234.83s |
| `make test-acceptance` | failed on first integrated attempt | 1 failed, 19 passed, 1,745 deselected in 138.65s; resolved by final rerun below |
| `make test-acceptance` final-byte rerun | passed | 20 passed, 1,746 deselected, 1 expected DataHub document-upsert warning in 95.49s |
| `make evaluate` | passed | 11 tables/465 rows; deterministic completed, live LLM not run; expected SQLGlot `CALL`/`DO` warnings |
| `make runtime-wheel-smoke` | failed | Wheel installed, but `schemabridge-semantic-change --help` returned 1 because composition ran before argument parsing; resolved by clean rerun below |
| Wheel-help correction cut | passed | 18 unit tests in 2.02s; directed Ruff passed |
| `make runtime-wheel-smoke` clean rerun | passed | 23.98s; installed outside checkout, migrations 1–5 and three M26 commands validated |
| `python scripts/release_audit.py` | passed | 617 candidate files, 23 direct dependency licenses, 0 external links; expected `release_tree_dirty` warning |
| `make check` first final attempt | failed | Ruff format found 11 files; corrected mechanically |
| `make check` clean rerun | passed | Ruff format 419, Ruff lint, mypy 225, pytest 1,625 passed/143 deselected in 63.22s |
| `make coverage` | passed | 1,768 passed, 7 expected DataHub upsert warnings in 471.77s; 81.64% >= 80% |
| `git diff --check` | passed | No output |

## Automated test results

- Focused profile-binding tests: 43 unit and 6 PostgreSQL integration passed.
- Browser helper/panel/schema focused command: 52 passed in 3.74 seconds; the helper-only subset
  passed 5 in 1.07 seconds and schema-migration subset passed 11 in 0.25 seconds.
- Focused `tests/integration/test_semantic_change_postgres.py`: 2 passed in 16.20 seconds.
- Focused real PostgreSQL dependency regression: 5,434 artifacts/5,434 mapping edges persisted in
  5.50 seconds including control-plane migration/setup; the test budget is 30 seconds.
- Exact locator, conservative stale-artifact/incomplete-coverage, live-index gate, and resulting
  baseline-head revision regressions pass in the final 190-test focused selection.
- Current-byte schema-v5 reset/migrate/check passed for all six credentials with no pending
  migration and source/control separation verified. Final migration SHA-256 is
  `8e311f27c881844a1c67a0281391ddc1eb7b863f1d2f8edff382933397412285`.
- Final real PostgreSQL/DataHub M26 acceptance: 1 passed in 260.31 seconds; `/usr/bin/time`
  real/user/sys was 262.27/13.70/2.26 seconds.
- The browser-session `prepare` acceptance seed passed 1 test in 116.95 seconds. Retained effective
  state has `current=1`, `review_required=1`, `blocked=1`, `revalidated=3`, maximum 32 findings,
  maximum 38 impacts, directory mode 0700, and state/bearer/cursor modes 0600.
- Real-upstream internal-browser record passed at 1440x900 and 390x844 with exact
  client/scroll/body widths 1440/1440/1440 and 390/390/390. It exposed four allowlisted
  authenticated GET operations, zero mutations/approval controls, no console warnings/errors,
  no protected-data hits, and no dangerous controls.
- Exact browser report/fingerprint pairs were
  `report_3cfed1ece1359de33af7939a66a554604ff5f2236f1af08388c6559d58df92da` /
  `3cfed1ece1359de33af7939a66a554604ff5f2236f1af08388c6559d58df92da`,
  `report_c5e911cf752f8e186e9c8b24d7a41f45b22f4d569e3963a8ff6206fada9c08ba` /
  `c5e911cf752f8e186e9c8b24d7a41f45b22f4d569e3963a8ff6206fada9c08ba`,
  `report_f79ad25956bebce92c6c4e67098f1c0f94dfebecc9ebc5f3cc4fb5bb3bba5d4a` /
  `f79ad25956bebce92c6c4e67098f1c0f94dfebecc9ebc5f3cc4fb5bb3bba5d4a`,
  and
  `report_e23879381781f8f0255dfd2b21cc73ff7ebba6d69711b5be0086c766d4de6110` /
  `e23879381781f8f0255dfd2b21cc73ff7ebba6d69711b5be0086c766d4de6110`.
- Their exact current/review/blocked/remediated impact-set fingerprints were, respectively,
  `982ffa89060c844b3ff3519c00be0fa2ad46a4625cdca6d137e3caa80b5f348a`,
  `ce4cbca2394fe0d13e91ce7e02560abd9429d07e1a7da2747143d1c94291b152`,
  `4be5fdc599b69282e8140cf72a51d9dc2f07c5fa6feb7562b2e6848a8ca0a2ba`,
  and `be581a4e98562ca318a12b6b4b096d7fecfe60857043760454c03b3941ace9b4`.
  All had dependency watermark 3 and complete coverage. Review and blocked each showed one
  mapping/join/workflow/recipe impact; remediated showed 31 findings and 38 total impacts.
- Findings pages 1/16/31 and impact pages 1/19/38 exercised first/middle/final cursor boundaries.
  Unknown, provisioned cross-tenant, tampered-cursor, and wrong-filter cursor requests returned the
  same 404 `semantic_change_resource_unavailable`; automated API tests retain stale/expired
  boundary coverage.
- Exact-confirmation cleanup removed the dedicated retained database and owner-only state, left
  8510/8520 without listeners, and a psycopg administrative query returned retained database count
  0. The secondary `psql` check was unavailable with exit 127 and was not represented as passing.
- The internal-browser session preceded the final statistics-independent locator and CLI-help
  corrections. Panel/API presentation bytes did not change afterward; the final backend bytes were
  separately covered by cold PostgreSQL acceptance, full integration/acceptance, `make check`, and
  coverage. The browser record is therefore presentation evidence, not a claim that its retained
  database contained the final migration bytes.
- The first `make test-integration` attempt failed with 2 failed, 126 passed, 1,637 deselected, and
  6 warnings in 241.71 seconds. One DataHub recipe integration exposed legacy/global current-marker
  contamination across semantic scope; one dynamic-catalog gate fixture lacked the exact live
  dependency status now required by the fail-closed gate. These are recorded failures, not accepted
  skips.
- The two corrected integration tests then passed in 6.95 seconds with directed Ruff format/check.
  The recipe test now publishes, reads, reuses, and audits a unique 64-hex scope fingerprint while
  proving another scope returns no current recipe. The dynamic-catalog test now seeds the exact
  complete dependency-index state bound to its pointer/report. Neither correction relaxes the
  production scope or live-dependency gate.
- The complete `make test-integration` rerun passed all 128 selected integration tests with 1,637
  deselected and no failures in 248.82 seconds. Its six warnings were the existing expected DataHub
  `IngestionAttributionWarning` notices about partial overwrite of an existing entity; both former
  failing areas passed inside the global integration selection.
- On the final locator bytes, the required focused unit selection passed 190 tests in 6.39
  seconds; semantic report integration passed 2 in 15.90 seconds; scan/profile integration passed
  12 in 87.00 seconds; API integration passed 18 in 34.32 seconds; and worker integration passed
  24 in 39.67 seconds.
- The final-byte complete `make test-integration` rerun passed 128 tests with 1,638 deselected and
  six expected DataHub document-upsert warnings in 234.83 seconds.
- The first `make test-acceptance` attempt failed 1 test with 19 passed and 1,745 deselected in
  138.65 seconds. `test_release_evaluation_measures_every_boundary_and_detects_wrong_rows`
  correctly classified checked-in recipe v2 as `stale|plan_changed`: M26's new optional binding
  fields change the typed plan fingerprint even when null. The artifact was regenerated as
  governed recipe v3 from the current saved execution through `QueryRecipe.create`, with workflow
  lineage `m26` and creation date 2026-07-24. A focused proof and complete acceptance rerun were
  required and later passed; the expectation was not weakened to treat stale context as reusable.
- The first recipe-v3 targeted run then failed 3 tests with 4 passed in 2.90 seconds because strict
  version checks in the ground-truth manifest and unit expectation still declared recipe v2. After
  advancing both declarations to query-recipe version 3, the same targeted selection passed all 7
  tests in 7.99 seconds. The complete acceptance rerun remained required at that stage and later
  passed.
- The second complete `make test-acceptance` attempt still failed 1 test with 19 passed, 1,745
  deselected, and 1 warning in 91.63 seconds. The real M26 lifecycle's second
  `_load_initial_candidates` call was cancelled by its existing 5-second statement timeout.
  Retained facts showed 5,434 active assets, 5,458 fields, and 31 governed mappings; the candidate
  query shape scanned the full catalog and issued thousands of probes rather than resolving the
  31 exact physical locators.
- An isolated retained-state rerun then passed 1 test in 104.03 seconds. This proves the timeout is
  intermittent, not acceptable. Typical measurements around 54–115ms do not make the global query
  structurally bounded. The planned correction is an indexed, bounded, fixed-`search_path`
  `SECURITY DEFINER` locator-first candidate capability with maximum two witnesses per mapping and
  reconciler-only execution; increasing the timeout is explicitly rejected.
- The locator-first replacement is now implemented. Migration validation passes 12 unit tests, and
  one exact/fail-closed PostgreSQL end-to-end test passes in 11.97 seconds after strict request
  validation, including an ordinal gap and selected-field mismatch. Final-byte six-role ACL
  targeting passes 1 test in 1.20 seconds. The real 5,434-table acceptance with a second inspect,
  `EXPLAIN ANALYZE BUFFERS`, and index assertions passed one warmed run in 126.89 seconds but was
  later invalidated by the clean-base result below. Directed Ruff format/check over the seven
  changed implementation/test files, strict `make type` over 225 source files, and
  `git diff --check` pass. No timeout was increased and migrations 0001–0004 remain unchanged. The
  complete `make test-acceptance` rerun remains required.
- The focused final scale fixture retained generation 2 with 5,434 assets and 5,458 fields and
  observed exactly 31 governed fields, each with `candidate_count <= 2`. Its function plan asserted
  `Function Scan`, 31 actual rows, at most 4,096 shared hit+read blocks, and execution below
  5,000ms. Locator plans required both `catalog_assets_semantic_lookup_idx` and
  the then-current field index. Exact final-run buffer/time values were not printed, and the later
  clean-base failure below invalidated this warmed attempt as release evidence.
- A fresh clean-base repetition invalidated that first warmed scale pass: it failed 1 test in
  22.82 seconds with 59,615 touched blocks, and a retained reproduction still touched 16,333,
  both above the 4,096 bound. Fresh bulk-loaded tables had no analyzed statistics, so PostgreSQL
  selected the asset-ID index and filtered 5,433 unrelated assets per mapping. The earlier
  126.89-second pass is retained as a non-final attempt, not accepted evidence. A deterministic
  lookup independent of `ANALYZE`, followed by clean-base plan and complete acceptance reruns, was
  required and later passed; limits and timeouts were not raised.
- The final correction uses length-prefixed asset and field locator helpers, expression indexes
  `catalog_assets_semantic_lookup_idx` and unique `catalog_fields_semantic_lookup_idx`,
  equality-only lateral probes with `OFFSET 0`, and external exact raw-value rechecks. The cold
  acceptance deliberately disabled autovacuum at migration and verified `reltuples < 0` for both
  asset and field tables before inspection. It passed 1 test in 126.66 seconds over 5,434 assets,
  5,458 fields, and 31 mappings; the plan was a 31-row `Function Scan`, named both expression
  indexes, stayed within 4,096 touched blocks, and executed below the unchanged 5-second bound.
  Exact block/time values were not printed, so the asserted bounds are the evidence. The combined
  final locator schema/dependency unit cut passes 23 tests; ACL/E2E, directed Ruff over seven files,
  and strict mypy over 225 files remain passing. The full global gates are still required.
- The independent audit identified both cold phases: the asset probe first filtered 5,433 assets
  per mapping; after isolating it, the field probe selected `catalog_fields_exact_lookup_idx` and
  filtered about 5,457 fields for each of 31 mappings, producing an intermediate 54,975-block
  failure. The final helpers are immutable, strict, parallel-safe invoker functions with
  `search_path=pg_catalog`; `PUBLIC` is revoked and only the catalog role has helper execution for
  maintenance. The reconciler receives only the main security-definer lookup. A second independent
  run passed its scale assertion but then ended FAIL/ERROR after 54.35 seconds when another process
  removed the database (`AdminShutdown`); it is recorded, not counted as a full pass.
- The final-byte `make test-acceptance` rerun passed all 20 selected acceptance tests with 1,746
  deselected and one expected DataHub document-upsert warning in 95.49 seconds. This closes the
  earlier recipe and locator acceptance failures without erasing them from the record.
- `make evaluate` passed on the deterministic 11-table/465-row demo with seed SHA-256
  `487388495115265d5fac5a17675e05c236fd4eb4c430613ef02050a5f6010654`; deterministic evaluation
  completed, optional live-LLM evaluation remained explicitly `not_run`, and SQLGlot warnings for
  adversarial `CALL`/`DO` inputs were expected.
- `make runtime-wheel-smoke` failed after installing the wheel because
  `schemabridge-semantic-change --help` returned exit 1: the entrypoint composed runtime services
  before argument parsing.
- The CLI now parses `--help` before composing runtime. Its focused correction passed 18 tests in
  2.02 seconds plus directed Ruff. The clean `make runtime-wheel-smoke` rerun passed in 23.98
  seconds from a wheel installed outside the checkout and discovered migrations 1–5 plus all three
  M26 commands. This closes the packaging failure while preserving its first failed attempt above.
- Current-byte control-plane reset/migrate/check passed in 11.72 seconds. All six runtime,
  reconciler, migrator, API, worker, and catalog credentials reported exact schema v5 and
  source/control separation.
- `python scripts/release_audit.py` passed over 617 candidate files and 23 direct dependency
  licenses with zero external links; `release_tree_dirty` is the expected warning for this
  uncommitted workspace.
- The first final `make check` attempt failed only because Ruff format identified 11 files. They
  were formatted mechanically; the clean rerun passed Ruff format across 419 files, Ruff lint,
  strict mypy across 225 source files, and pytest with 1,625 passed/143 deselected in 63.22 seconds.
- Integration and acceptance tests: final-byte required selections pass.
- `make coverage` passed all 1,768 collected tests with seven expected DataHub upsert warnings in
  471.77 seconds. Total coverage is 81.64%, above the 80% gate.
- Migration v5 SHA-256:
  `8e311f27c881844a1c67a0281391ddc1eb7b863f1d2f8edff382933397412285`. Current-byte
  reset/migrate/check passed; the earlier candidate checksum is obsolete.
- Six-role current-schema/separation matrix and final-byte ACL regression: passed.
- Runtime wheel smoke: passed after the recorded first failure; broader process probes remain part
  of the remaining final matrix.
- Release audit: passed with the expected dirty-tree warning.
- Final cleanup found zero databases matching `schemabridge_m26_%`. Only the local diagnostic
  `.local/m26-evaluation-current.json` and `.local/m26-evaluation-current.md` files were removed,
  and their absence was verified.

## Operator manual test

1. Back up schema v4, apply pristine and v4→v5 migration paths, and record exact migration/checksum,
   role, and source/control-separation evidence.
2. Start one semantic reconciler and one aggregate profile worker per governed
   workspace/connection pair under distinct reconciler/worker roles; prove probes perform no
   queue/source/DataHub I/O.
3. Process the initial scoped scan, review exact binding candidates, and establish the first
   baseline only through inspect → prepare → approve → commit.
4. Promote an unrelated change among the 5,434-table tenant and prove the selected plan stays
   current without a full catalog scan.
5. Promote a definition/tag-only change, observe `review_required`, and complete exact compatible
   revalidation.
6. Promote type/key/nullability/removal and unsafe join-profile drift; prove affected planning,
   compilation, preview, rejection inspection, and source I/O remain blocked.
7. Publish/activate a corrected strict registry, process its scope-qualified scan, approve new
   evidence, and prove eligibility returns without rewriting prior state.
8. Break either workflow or DataHub recipe pagination and prove coverage is visibly incomplete and
   blocks approval/execution; prove lease loss cannot advance the watermark.
9. Prove identical recipe intent in another scope uses a different current URN and cannot enter
   this blast radius; prove the same connection ID in two workspaces cannot cross-claim a profile
   job.
10. Run `scripts/m26_browser_acceptance_runtime.py prepare`, then start its `api` and `panel`
    commands. Use Codex's internal browser against the real upstream API at desktop and 390x844;
    traverse
    current/review/blocked/remediated reports and first/middle/final finding/impact pages, exercise
    stale/tampered/cross-tenant denial, scan protected data/console, and measure overflow.
11. Re-run helper `status`, stop both processes, then invoke `cleanup` with the exact confirmation;
    record that only the dedicated retained database and expected state files were removed.

Expected result:

```text
schema v5 exact; six roles least privilege; source/control distinct
baseline requires explicit approval
compatible change requires exact revalidation
blocking change performs zero compiler/source I/O
corrected registry restores eligibility without rewriting history
dependency coverage complete only after both paginated sources reach validated EOF
profile jobs and workers remain bound to one exact workspace/connection pair
unrelated 5,434-table change does not block an unaffected plan
read-only browser pages are bounded, protected, clean, and responsive
```

Manual result: **passed and accepted locally**. The exact real-upstream desktop/mobile record and
cleanup passed as documented above and in `docs/14_BROWSER_ACCEPTANCE.md`; the final technical
matrix also passes.

## Architecture and security review

- Dependency direction: M26 domain remains pure; application uses protocols; PostgreSQL, DataHub,
  process, HTTP, and panel details stay in adapters/entrypoints/composition root. Full-tree Ruff,
  mypy, tests, coverage, and release audit pass.
- Source database writes: none implemented or authorized. Aggregate profiling uses the existing
  allowlisted read-only reader and stores counts/fingerprints only. Focused final-byte six-role ACL
  and the current-schema/source-separation check pass.
- SQL/LLM validation: M26 invokes no LLM and introduces no raw-SQL input. Eligible execution still
  uses typed plan → deterministic compiler → independent AST guard; M26 gate runs before compiler
  and protected source I/O.
- DataHub mutation approval: semantic-change processes are read-only to DataHub. Corrected registry
  publication remains the existing separately approved M22/M23 flow.
- Secrets/proprietary data: only synthetic fixtures are permitted. Reports/jobs/API/panel omit
  rows, values, SQL, parameters, DSNs, tokens, claims, keys, prompts, and OpenAI material. Final
  browser protected-data scan found zero hits; release audit passes with only the expected
  dirty-tree warning.
- Fanout/semantic risks: exact workspace/connection-bound aggregate profiles and dependency-aware
  gating block changed cardinality/FK/overlap/null/invalid/multiplicity safety. A blocking change
  cannot be waived against the same registry.
- Final-audit corrections: locator-first catalog evidence, fail-closed bounded dependency
  reconciliation, conservative same-scope stale projection, exact live-index gating,
  resulting-head baseline revision, and workspace-plus-connection profile routing are implemented.
  Initial candidates now also use a separate bounded locator-first function instead of the
  connection-wide query exposed by integrated acceptance. The v1→v2 registry-source identity
  change retains same-scope dependency impact rather than disappearing; missing/cross-scope
  identity remains incomplete. Cold locator scale/acceptance and the final full-tree gate pass.

## Decisions made

- Decision: exact mapping observations and gate scope by workspace/catalog/registry/connection.
- Reason: physical paths collide across connections and unrelated catalog drift must not cause a
  tenant-wide outage.
- Logged in: ADR 0012, D086.
- Decision: profile workers bind and claim by workspace plus `connection_id`.
- Reason: equal connection labels can exist in two tenants, and a global source DSN cannot safely
  execute either tenant's evidence.
- Logged in: ADR 0012, D087.
- Decision: catalog-generation scans fan out per active scope and dependency traversal renews the
  lease between pages.
- Reason: singleton scope and whole-scan heartbeats can miss registries or allow stale coverage.
- Logged in: ADR 0012, D088.
- Decision: active query-recipe DataHub IDs include semantic scope fingerprint.
- Reason: intent-only current markers collide across tenants and corrupt dependency/reuse state.
- Logged in: ADR 0012, D089.
- Decision: one atomic dependency snapshot is capped at 10,000 artifacts/100,000 edges and never
  silently truncated.
- Reason: blast-radius completeness must remain bounded and fail closed at enterprise variance.
- Logged in: ADR 0012, D090.

## Known limitations or unverified items

- Final automated, integration, acceptance, evaluation, package, release-audit, quality, coverage,
  browser, cleanup, checksum, role, and diff gates pass after the recorded failed/corrected
  attempts.
- Current-byte schema-v5 reset/migrate/check and all-six-credential current/expected checks pass.
- M26 records multiple connections but does not dynamically route credentials. Deploy one isolated
  profile worker per workspace/connection pair; M28 owns connector routing/federation.
- Dependency reconciliation intentionally materializes one bounded atomic snapshot in memory:
  maximum 10,000 artifacts and 100,000 edges. Larger managed workflow/recipe populations fail
  coverage closed and need a later streaming/sharded design or reviewed policy change.
- Legacy intent-only recipe documents require governed republishing to enter an active scoped
  dependency index; no automatic rewrite occurs.
- The locator-key SQL/Python formula is proven equal for all accepted ASCII fixture identities
  (31/31 requested locators and 5,458/5,458 fields). No Unicode-equivalence claim or global catalog
  CHECK is made; exact raw-value rechecks preserve fail-closed behavior, while M25's Unicode catalog
  support remains unchanged.
- The M26 panel is acceptance instrumentation, not production Query Studio.
- The working tree remains dirty/uncommitted and is not a release candidate.
- Kubernetes/TLS/NetworkPolicy/external-secret/HA/monitoring/SLO operation remains later work.

## Blockers

- None for local M26 acceptance.
- Global production/release remains blocked by the dirty tree and the later infrastructure,
  observability, supply-chain, HA, and operator-security milestones listed above.

## Next milestone readiness

- Dependencies satisfied: yes; M26 is accepted locally and M27 is eligible.
- Recommended next prompt: execute M27 dynamic Query Studio with bounded short-description matching
  and human-confirmed ambiguity.
- Required operator prerequisites: retain synthetic-only services, keep the existing OpenAI key
  isolated through environment configuration, and preserve the accepted M26 fail-closed boundary.
