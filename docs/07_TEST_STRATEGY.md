# Test strategy

## Test pyramid

### Unit tests

No network or containers. Cover:

- value-object validation;
- transformation semantics;
- candidate scoring;
- cardinality and fanout rules;
- analytical-request validation;
- query-plan resolution with fakes;
- deterministic SQL compilation;
- SQL guard policy;
- LLM structured-output validation;
- view-model formatting.
- atomic semantic-registry integrity, manifest security, scope, and canonical fingerprints;
- stale-registry revalidation and zero downstream I/O;
- registry-wide capacity versus per-query table/join limits.

### Integration tests

Marked `integration`. Cover:

- PostgreSQL read-only enforcement and preview;
- compiler + parser + database execution;
- DataHub read adapter against local Core;
- DataHub mutation adapter with explicit approval;
- logical-model/document write and retrieval;
- SQLite draft storage;
- OpenAI adapter only when an opt-in API key is available; deterministic fixtures remain the CI path.
- exact multi-domain planning/compiler/guard/PostgreSQL execution and source rejections;
- deterministic seed-manifest verification and reader DML/DDL denial;
- eleven-dataset DataHub description/profile checks.

### Acceptance tests

Marked `acceptance`. Cover complete user-observable paths:

1. approve semantic mappings;
2. approve a join contract;
3. guided north-star query;
4. natural-language request produces the same typed plan;
5. preview returns the ground-truth rows;
6. rejected identifiers are visible;
7. context is published and reused;
8. malicious request cannot bypass safety.
9. the larger registry is visible while the unchanged north-star result remains exact;
10. registry drift after confirmation fails before preview and requires confirmation again.

## Ground truth

Versioned fixtures under `demo/ground_truth` define expected mappings, joins, request
interpretations, rows, and rejection reasons. `registries/manifest.yml` binds the complete active
semantic registry, and `seed_manifest.yml` binds schemas, constraints, exact per-table row/data
hashes, reader safety facts, and the global corpus fingerprint. Evaluation code must never train or
tune against hidden copies.

## Metrics

- candidate precision, recall, and F1;
- top-k field recall;
- join-path accuracy;
- cardinality accuracy;
- intent exact/semantic match;
- SQL compile success;
- SQL execution success;
- result-set correctness;
- security-case rejection rate;
- context-reuse rate;
- median and p95 demo latency where reproducible.

All reported values must come from a checked-in command and artifact. Do not invent targets as achieved results.

## Fixtures

- small deterministic tables;
- explicit invalid values;
- duplicate relationships to expose fanout;
- fake DataHub payloads recorded without secrets;
- fake LLM responses validated against schemas;
- frozen clocks/IDs when snapshots depend on them.

## M21 deterministic diversity gate

The verified registry fixture contains seven models, 31 approved mappings, and five approved join
contracts. Its canonical fingerprint is
`0710148874049078f751ac96f0a18131cf01daa41f27dc034ca52a8b212dd966`; the registry file SHA-256 is
`4f086f8b9f0679d6405beab45d48c6601520b9b6e0c5f20d7dc0ad987485a723`.
The source fixture contains 465 rows across eleven tables and eight schemas with global fingerprint
`487388495115265d5fac5a17675e05c236fd4eb4c430613ef02050a5f6010654`.

Coverage must include:

- a no-join Product query;
- a SalesOrder/Shipment one-to-many query with exact-key distinct mitigation;
- a Product/SaleLine/SalesOrder three-table, two-join commerce query;
- malformed, negative, empty, and `NULL` identifiers;
- decimals, booleans, timestamps, categorical maps, padding, orphans, and duplicate fanout;
- support-domain homonyms that do not become mappings through name similarity;
- two scoped destructive demo resets with identical per-table and global hashes;
- unchanged north-star `2, 1, 1` rows and rejection codes;
- one Customer-focused natural-language case plus explicit skips for the four registry-wide cases
  that remain M27 scope.

M21 does not use this synthetic corpus to claim production quality or scale. M22 adds complete
live DataHub registry reconstruction; M23 adds PostgreSQL activation, migrations, and
reconciliation.

## M22 live-registry verification

Unit coverage must exercise the immutable publication contract and adversarial read-back without a
network:

- missing approval, wrong fingerprint/workspace/target/decision closure, conflicting immutable
  content, post-write mismatch, audit mismatch, replay, and audit-store failure;
- missing/removed document, malformed or duplicate-key JSON, excessive response/property size,
  unexpected properties, wrong scope/version/fingerprint, stale decisions, wrong related assets,
  reader mutation privileges, redirect, permission denial, and outage;
- a read client that exposes no upsert method and a live loader that never opens the manifest;
- registry outage or drift after confirmation with zero compiler, preview, or rejection I/O.

Service-backed integration and acceptance must load a fresh adapter from the exact DataHub version
document, reconstruct 7 models/31 mappings/5 joins/37 decisions/7 related assets, and plan plus
execute both the north-star and three-table commerce cases through the deterministic compiler,
independent AST guard, `schemabridge_reader`, read-only transactions, and 5000 ms timeout. The test
provides a nonexistent manifest path so any fallback is observable.

The live fingerprint for the current local-demo document is
`ef480eb7370924ff4c94131a2c6c4063d85652cc83aaec9059538cdc2cf9c1b9`. It intentionally differs
from the M21 recorded fingerprint because source and provenance are part of the canonical payload.
The checked-in query-recipe fixture still cites the recorded source; live evaluation must report it
stale rather than count it as reusable until M23 migrates that provenance.

Run the focused surface with:

```bash
pytest tests/unit/test_datahub_semantic_registry.py \
  tests/unit/test_semantic_registry_publication.py \
  tests/unit/test_auth_config.py tests/unit/test_governed_execution.py
make datahub-registry-check
make test-integration
make test-acceptance
make evaluate
```

These automated checks do not by themselves satisfy a UI criterion. The recorded M22 browser run
verifies fixed live-version selection. The separate M23 record below verifies the active
PostgreSQL generation, reconciled projection state, and minimized durable result boundary.

## M23 durable control-plane verification

M23 tests the control plane as a state machine with adversarial persistence boundaries, not as a
collection of happy-path CRUD methods.

### Unit contract

The no-service suite must cover:

- ordered checksum-pinned migration discovery, exact replay, advisory-lock contention, future and
  drifted history, incompatible/partial schema, interrupted transaction, sanitized DSN errors, and
  distinct runtime/reconciler/migrator composition;
- read-only server-observed source/control identity, expected-user validation, DNS alias safety,
  same-database rejection, unavailable-probe fail-closed behavior, and no credential leakage;
- activation proposal and approval binding, initial and later compare-and-swap, exactly one
  concurrent winner, idempotent replay, zero mutation for stale approval, monotonic rollback, and
  rejection of unknown, corrupt, removed, never-active, or legacy-shim-only targets;
- atomic pointer/transition/outbox/audit facts, deterministic IDs, per-workspace HMAC-chain
  verification, altered payload/order/previous hash/key version, and key/DSN redaction;
- active pointer then exact-version loading with wrong scope, source, target, version, registry
  fingerprint, active-pointer fingerprint, decision closure, missing state, and zero
  fixed-version/manifest fallback;
- all nine reconciliation codes, exact report approval, safe repair, exact replay, failed
  projection preserving `pending`, post-write mismatch, and refusal to overwrite ahead,
  conflicting, corrupt, audit-gap, or superseded state;
- workspace-scoped managed-store optimistic concurrency, append-only decisions/publications,
  preview-row removal, tenant isolation, replay, and safe unavailable/conflict errors;
- legacy import exact known schemas, offline-source requirement, dry-run/approval/apply binding,
  changed source, quarantine, no owner inference, idempotency, and absence of preview rows;
- identity dual-key derivation, collision/cycle/cross-workspace/incomplete-owner/stale-policy
  rejection, immutable historical bytes, durable reservation, completion, replay, current-to-old
  workflow/grant resolution, uninitialized identity, bounded aliases, and ambiguous-match refusal;
- signed identity-evidence permissions, symlink/size/duplicate-key rejection, HMAC key version and
  signature, exact fingerprint, 15-minute validity window, changed-file detection, and raw-claim
  field rejection;
- stale-recipe migration only from a newly completed current-registry workflow, with exact
  revalidation before publishing a new version while historical bytes remain unchanged;
- owner-only backup/manifest artifacts, signature/archive tampering, source-target rejection,
  bounded manifests, hidden database password, and typed post-restore evidence;
- registry/version/status/reconciliation read-only CLI commands reporting
  `writes_performed=false`, legacy inspection writing only a metadata dry-run reservation with
  zero target rows, exact fingerprint and closed confirmation enforcement, managed actor/role
  resolution with argv spoof rejection, sanitized failures, no restore DSN option, and managed-web
  composition without migrator/reconciler/restore credentials;
- UI view-model invariants for fixed versus active selection, generation/pointer-fingerprint
  pairing, and every closed projection status.

The focused command is:

```bash
pytest tests/unit/test_control_plane_migrations.py \
  tests/unit/test_control_plane_audit.py \
  tests/unit/test_registry_control.py \
  tests/unit/test_control_plane_operations.py \
  tests/unit/test_control_plane_bootstrap.py \
  tests/unit/test_control_plane_operator_cli.py \
  tests/unit/test_registry_control_cli.py \
  tests/unit/test_database_separation.py \
  tests/unit/test_legacy_control_plane_import.py \
  tests/unit/test_identity_evidence.py \
  tests/unit/test_identity_rotation.py \
  tests/unit/test_identity_resolution.py \
  tests/unit/test_identity_operator_cli.py \
  tests/unit/test_recipe_migration.py \
  tests/unit/test_recipe_migration_cli.py \
  tests/unit/test_query_recipes.py \
  tests/unit/test_ui_view_models.py
```

### PostgreSQL and DataHub integration contract

Service-backed tests must create the schema from empty, reconnect fresh store instances, prove
database grants, race activation with two connections, preserve a pending outbox across process
boundaries, project and read back the exact DataHub document, and exercise active loading with a
nonexistent manifest path. Managed storage, legacy import, and identity rotation must be verified
against the real PostgreSQL schema rather than only fakes.

```bash
make control-plane-reset
make control-plane-migrate
make control-plane-check
pytest tests/integration/test_control_plane_postgres.py \
  tests/integration/test_database_separation_postgres.py \
  tests/integration/test_managed_storage_postgres.py \
  tests/integration/test_legacy_control_plane_import_postgres.py \
  tests/integration/test_identity_rotation_postgres.py \
  tests/integration/test_recipe_migration_postgres.py
make test-integration
make test-acceptance
```

CI must run `control-plane-reset`, `control-plane-migrate`, and `control-plane-check` before the
integration suite, so service tests never pass against an implicit or stale control schema.

Backup acceptance requires a real custom-format archive and a **fresh, distinct** restore database.
The test compares exact migration identity, complete table/sequence state digest, table counts,
every workspace audit chain, active pointer, transitions, pending outbox, and quarantine records.
An altered artifact, source target, or non-empty target must fail before cutover.

### Operator and browser evidence

Automated success is insufficient. The M23 operator sequence records:

1. migration and exact role capabilities;
2. strict version publication and generation-1 activation;
3. activation while projection is unavailable, with PostgreSQL still authoritative and UI status
   visibly `pending`;
4. exact approved reconciliation and `delivered` read-back;
5. stale-workflow blocking and a fresh `2, 1, 1` north-star result plus three rejection classes;
6. rollback as a higher generation;
7. signed backup and verified fresh-target restore;
8. authenticated Streamlit generation/version/registry fingerprint/pointer fingerprint/projection
   state, clean console, and 390x844 layout.

The 2026-07-23 local operated record completed that sequence:

- schema version 1 checksum was
  `65d9447619165a92e9b8d8c6541a76569564931b6dc9c7ccbd73deb0f3fd09cc`, with separate
  runtime/reconciler/migrator roles;
- version 6 activated as generation 1, version 7 as generation 2, pending projection state
  survived independently, explicit reconciliation delivered it, and rollback selected version 6
  as generation 3 before another explicit reconciliation to `delivered`;
- legacy import accepted exactly 2 resources and quarantined 4, with 0 skipped and 3 preview rows
  stripped. The four reason codes were `invalid_payload`, `orphan_access_grant`,
  `orphan_workflow`, and `ownership_not_provable`, once each; replay was exact and durable
  workflow rows remained empty;
- synthetic signed identity evidence initialized v1 and rotated to v2 with 2 verified bindings.
  Approval did not change bindings; completion and replay returned the same result, historical
  payloads/grants were byte-stable, and a v2 principal resolved the v1 workflow;
- live recipe migration created DataHub recipe version 44 from version 43. Preparation/publication
  exposed neither SQL nor preview rows;
- backup and fresh-target restore matched complete state SHA-256
  `22b8fdc5568b81e2bdd8626b3315e525c16651f19d3966c538b87cc7948ec2e5`; both artifacts
  were owner-only and the temporary target was independently confirmed absent after the drill;
- the fresh 1280x720 browser showed generation 3/version 6, projection `delivered`, pointer
  fingerprint `85f131dd53bb5cf2da8538cc171277c4d988b77101e0e5b37da46fed2ef24757`,
  and 7/31/5 counts. Console output was `[]`; at 390x844 document and body width were exactly 390,
  with no horizontal overflow, and the reset desktop console remained `[]`;
- the final post-fix `make check` passed 767 no-service tests plus Ruff, formatting, and strict
  mypy over 160 source files.
- `make test-integration` passed 58 tests with 782 deselected and 6 expected DataHub-overwrite
  warnings; `make test-acceptance` passed 15 with 825 deselected and 1 expected warning;
- `make evaluate` passed 11 tables and 465 rows with global SHA-256
  `487388495115265d5fac5a17675e05c236fd4eb4c430613ef02050a5f6010654`;
- `make coverage` passed 840 tests with 7 warnings and 81.03% coverage, above the 80% gate.

Release audit and diff check are repeated after documentation changes and belong in the milestone
handoff. The operated record is local acceptance, not a production traffic, retention, HA, or
multi-region claim.

## M24 authenticated API and durable-worker verification

M24 separates pure transition coverage, HTTP/authentication boundary coverage, PostgreSQL
concurrency/privilege evidence, real-socket acceptance, and internal-browser acceptance. A green
unit suite alone cannot approve the milestone.

### Unit contract

The service-free suite must cover:

- immutable authorization/request fingerprints, raw-key absence, bounded summaries, every valid
  and invalid lifecycle transition, terminal immutability, deterministic backoff, cancellation,
  lease expiry/reclaim, stale capability/fence, and exhausted-attempt reaping;
- OIDC signature-before-mapping, fixed asymmetric policy, forged/`none`/confused algorithms,
  unknown/duplicate key IDs, wrong issuer/audience/`azp`/tenant/group/time, redirect, attacker key
  URL, malformed/duplicate/oversized JWKS/token, cache refresh/outage, and redacted errors;
- development-only constant-time local bearer verification and secret-strength/configuration
  failures;
- exact workflow permission/grant/checkpoint/revision/plan validation, bounded authorization TTL,
  cross-tenant/owner/role non-disclosure, replay after completion, payload collision, and
  idempotent cancellation races;
- strict HTTP schemas and headers, host/content-type/content-encoding/body bounds, duplicate
  authorization/idempotency headers, sanitized `application/problem+json`, disabled docs, bounded
  health, response models with no protected job fields, and outer-boundary containment of an
  unexpected exception without traceback or ASGI-server leakage;
- worker authorization/workflow/registry revalidation, cancellation before I/O, heartbeat race,
  periodic heartbeat across several lease intervals, final heartbeat, lost-lease failure,
  authorization expiry, completed-workflow recovery, ambiguous `STARTED` recovery without replay,
  functional failure, finite transient retry/dead-letter, and summary-only success;
- closed source-error classification: `QueryCanceled` becomes a retryable timeout; SQLSTATE class
  `08` plus only `40001`, `40P01`, `53300`, `55P03`, `57P01`, `57P02`, and `57P03` become
  retryable unavailability; a SQLSTATE-less `OperationalError` is the bounded connection-failure
  fallback; permanent schema/permission/policy errors and invalid safety evidence are terminal and
  never enter the retry loop;
- workflow-access store failure classified as unexpected external state and dead-lettered, never
  relabeled as a normal authorization mismatch;
- exact rejected-row totals with bounded sampled codes, an exact unclassified residual, and
  completeness/truncation flags, including 25,001 rejected rows represented by two sampled codes
  plus a 24,999 residual;
- composition isolation: API has no source/LLM/DataHub adapters; worker has no OIDC/browser,
  publication/writer, migrator, reconciler, or restore capability; both require exact schema v3.

The focused command remains:

```bash
pytest tests/unit/test_background_jobs.py \
  tests/unit/test_api_authentication.py \
  tests/unit/test_api_workflows.py \
  tests/unit/test_http_api.py \
  tests/unit/test_worker.py
```

Configuration, bootstrap, PostgreSQL-adapter, strict-schema, and disabled-publication regressions
are also included in `make check`.

### PostgreSQL and service contract

`tests/integration/test_background_jobs_postgres.py`,
`tests/integration/test_m24_identity_lineage_postgres.py`, and
`tests/integration/test_m24_process_lifecycle.py` exercise a fresh schema-v3 control plane and
verify:

- atomic job plus initial event and exact concurrent idempotency;
- two-worker `SKIP LOCKED` contention with one owner;
- database-time heartbeat, expiry/reclaim, higher fence, and stale-worker rejection;
- queued versus cooperative leased cancellation;
- finite retry, authorization expiry, and exhausted-lease dead letter;
- current-principal access to the exact historical workspace+submitter pair after verified
  identity rotation for both owner and workspace-wide `platform_admin` grants, with
  unknown/ambiguous lineage failing closed;
- schema-v1 startup refusal without automatic migration, worker schema-only readiness, and
  graceful `SIGTERM`;
- a real `subprocess.Popen` claimant killed by `SIGKILL` while leased, PostgreSQL lease persistence,
  a replacement denied before expiry, post-expiry reclaim with attempt/fence increments, and stale
  capability/fence rejection from a separate process;
- append-only events and durable absence of raw key, token, SQL, parameters, prompts, and rows;
- exact negative grants for API, worker, runtime, reconciler, migrator, source, and role assumption.

The final schema-v3 reset/migration and five-role check passed. Migration
`0002_authenticated_api_jobs.sql` has SHA-256
`4e828aacfc9cc0db35a5b05838556e35360d7e0322c16e1ed008c94faf4f4efc`.
Final M24 schema v3 adds `0003_reject_expired_job_success.sql` to reject success after
authorization expiry in the database. Its SHA-256 is
`fba8bf2fec35c56be95487c21452ac46a45b3598c5b56cf382414aa57c7240f0`.
The local check reports current/expected 3 and pending none for runtime, reconciler, migrator, API,
and worker, with source/control separation intact. Exact final command counts and browser evidence
are recorded in `tasks/M24_HANDOFF.md`.

Run:

```bash
make control-plane-reset
make control-plane-migrate
make control-plane-check
make test-api-integration
make test-worker-integration
make test-acceptance
make evaluate
make check
make coverage
python scripts/release_audit.py
git diff --check
```

The final schema-v3 local automated record is:

- `make check`: Ruff format/lint, strict mypy over 180 source files, and 1,074 tests passed with
  99 deselected;
- `make test-api-integration`: 16 passed;
- `make test-worker-integration`: 21 passed;
- `make test-integration`: 84 passed with 6 expected DataHub overwrite-attribution warnings;
- `make test-acceptance`: 19 passed, including the real-socket synthetic-OIDC path;
- `make evaluate`: passed over 11 tables/465 rows with corpus SHA-256
  `487388495115265d5fac5a17675e05c236fd4eb4c430613ef02050a5f6010654`;
  live LLM remained explicitly `not_run`;
- `make coverage`: 1,173 passed with 7 expected DataHub warnings at 81.62%;
- `make runtime-wheel-smoke`: built and installed the wheel in an empty environment and loaded the
  packaged migrations 1/2/3 successfully;
- `Dockerfile.runtime`: built and ran locally as UID 10001 with packaged migration discovery and
  the schema-v3 worker readiness probe.

The final schema-v3 artifact rerun resolved migrations 1/2/3 from the installed wheel, built the
runtime image, verified UID 10001 plus the same migration set, staged a synthetic DataHub reader
file as a regular UID-10001 mode-0600 non-symlink, and ran
`schemabridge-worker --probe-ready` inside the image against control schema v3 with exit code 0.
Manifest tests also require a unique pod-name worker ID, exec startup/readiness probes, process-only
liveness, and an unprivileged init-container copy into a memory-backed read-only runtime mount.

After the HTTP containment correction, the production-like HTTP plus real-socket focused selection
passed 10 tests. Response bodies and captured logs contained no injected sentinel, traceback, or
`Exception in ASGI application`; Uvicorn access logging remained disabled. This focused result is
supporting evidence, not a substitute for the final gate and internal-browser record.

The identity-lineage/store-classification correction passed Ruff, strict mypy over 179 source
files, 54 focused unit tests, and 10 PostgreSQL integration tests; the PLATFORM_ADMIN case was
stable in 5/5 repetitions. The complete service-free `make check` after that fix passed 1,044
tests with 97 deselected. It covers owner plus
workspace-wide `platform_admin` grants against the exact historical workspace+submitter pair,
rejects arbitrary/mixed-key/cross-lineage/quarantined lineage before workflow/source I/O, and
preserves real `STORE_FAILURE` as dead letter.

### Real-socket and internal-browser contract

Acceptance must start API and worker as separate processes against real control/source PostgreSQL,
use a signed synthetic OIDC token, and prove authenticated submission, exact replay, collision,
successful summary, queued cancellation, cooperative cancellation, dead letter, cross-tenant
denial, restart, and schema mismatch over a real HTTP socket. Database and log scans must confirm
that no protected material was persisted or emitted.

Codex's internal browser must then:

1. open sanitized `/health/ready` for schema v3;
2. use a reviewed synthetic workflow and bearer-authenticated fetches to observe one job from
   `queued` through terminal `succeeded`;
3. inspect only the bounded result summary;
4. repeat the page at 390x844 and measure no horizontal overflow;
5. record a clean warning/error console and scan displayed/network text for secrets, DSNs, SQL,
   parameters, prompts, claims, and preview rows.

The final real-socket acceptance is recorded in the 19-test acceptance suite. It uses a real
authorization-code/PKCE synthetic OIDC flow with RS256 access-token verification and live JWKS
fetch, real control/source PostgreSQL, the deterministic compiler and SQLGlot guard, successful
summary-only execution, replay/collision, queued and cooperative cancellation, dead letter,
cross-tenant denial, and protected-data scans. The periodic-heartbeat and leased-authorization
expiry regressions include the schema-v3 database predicate. The workspace-scope rotation and
workflow-access store classifications also have final regression evidence.

The internal-browser record is complete. It verified sanitized readiness, exact replay and `409`
collision, queued cancellation, a real worker `SIGKILL` followed by post-expiry reclaim and
success, indistinguishable wrong-role/cross-tenant `404` responses, bounded summary-only output,
clean console/network scans, and no horizontal overflow at desktop or 390x844. The same-origin
test relay was ephemeral acceptance instrumentation and kept the bearer server-side; it is not
product UI. Full details are in `docs/14_BROWSER_ACCEPTANCE.md`.

## M25 dynamic inventory and capacity verification

M25 separates correctness, scale, database plans, concurrency, process lifecycle, and browser
evidence. A passing unit suite or a generated 5,434-item fixture is not sufficient on its own.
Every measurement is a reproducible local regression budget, not a production SLO.

### Unit contract

The service-free suite must cover:

- tenant/connection-qualified connection, asset, and field identities, including identical
  `schema.table` and field paths in different scopes;
- bounded public metadata and explicit rejection of credential, row, sample, claim, SQL, prompt,
  and OpenAI material;
- durable tenant policy values and usage/admission boundaries for connection, asset, field,
  per-minute API request, and nonterminal job capacity;
- closed refresh transitions, terminal immutability, page/checkpoint/fingerprint invariants,
  lease expiry/fence rules, and resuming a durably committed terminal page without a source read;
- full versus genuine delta change kinds, duplicate page identities, field/asset bounds, invalid
  checkpoint progress, repeated page fingerprints, and sanitized source failure mapping;
- HMAC cursor round trip plus unsupported version, tampering, overlength, expiry, future issue
  time, another workspace/connection/asset/resource/filter/generation, and changed final sort key;
- list use cases that request at most `page_size + 1`, emit at most 50 items, and never decode a
  cursor after a scope mismatch;
- catalog-indexer source resolution by closed connection kind, page persistence before the next
  source call, heartbeat/fail/complete behavior, ambiguous store failure, stale ownership, and
  bounded polling/`--once`/graceful lifecycle;
- API authorization, exact confirmation/idempotency, public/private route separation, strict
  schemas, safe errors, rate denial, and absence of source calls from the API;
- bounded pool configuration, waiters, acquisition/startup/close timeout, saturation
  sanitization, and dependency `repr`/log redaction;
- unchanged query-plan and SQL-guard rejection of a fourth table, third join, cross-connection
  plan, Cartesian join, DDL/DML, unsafe fanout, excess rows, and timeout bypass.

The focused baseline is:

```bash
pytest tests/unit/test_catalog_inventory.py \
  tests/unit/test_catalog_cursor.py \
  tests/unit/test_catalog_refresh.py \
  tests/unit/test_capacity_limits.py \
  tests/unit/test_control_plane_pool.py \
  tests/unit/test_catalog_indexer.py \
  tests/unit/test_catalog_entrypoint.py \
  tests/unit/test_http_catalog_api.py
```

Exact counts and status are **evidence pending** until this command is rerun against the integrated
M25 tree.

### PostgreSQL schema, privilege, and concurrency contract

A pristine database and a version-3 upgrade must both reach exact schema v4. Integration tests
must verify checksum history, no mutation of migrations 0001–0003, `PUBLIC` revocation, fixed
`search_path`/explicit grants for every `SECURITY DEFINER` function, and the complete six-role
matrix for runtime, reconciler, migrator, API, worker, and catalog indexer.

Database-backed behavior must prove:

- capacity-policy create and revision require `platform_admin`, exact confirmation, and the
  current expected version; only the migrator function may apply the change, every accepted
  version is immutable, stale races perform no write, and retention cannot undercut cursor TTL;
- public connection registration/disable and refresh request are tenant scoped, idempotent, and
  quota checked, while private route bindings are invisible to API/web/worker roles;
- one active refresh per workspace/connection, database-time claim, capability digest, monotonic
  fence, atomic page plus checkpoint, stale-owner rejection, base-generation compare-and-swap,
  exact server-side fingerprint/count/quota verification, atomic promotion, and immutable
  tombstones;
- keyset tuple comparisons for connection/asset/field reads with `LIMIT page_size + 1` and no
  inventory-list `OFFSET`;
- field search finds bounded terms originating from definitions, native types, tags, and glossary
  terms without creating an approved mapping or executable identifier;
- retention never deletes the active generation and preserves retained generations for at least
  the cursor TTL;
- a three-request policy denies request four with bounded `Retry-After` while another principal
  and workspace remain independent;
- a five-job policy admits exactly five under a two-replica race, denies excess work, and releases
  capacity once on terminal transition;
- fair claim gives a workspace with one eligible job service within the documented bound despite
  another workspace having 100;
- API, worker, and catalog pools stay within configured maxima, time out safely under saturation,
  fail startup closed, and close every acquired resource.

Run from a deliberately reset synthetic control plane:

```bash
make control-plane-reset
make control-plane-migrate
make control-plane-check
make test-api-integration
make test-worker-integration
make test-integration
```

Migration v4 has exact SHA-256
`45c7d95e56a336f267cbe29ad390f6fd54a59d826ffd33253d290be0257060bb`.
Focused pristine/upgrade, role, refresh, rate/job-capacity, fair-claim, and pool cuts passed during
implementation. Their final integrated counts, function/index inventory, and six-role output are
now recorded in `tasks/M25_HANDOFF.md`; M25 is accepted locally.

### Scale correctness, index plans, and memory

The lazy fixtures must represent:

- a small tenant with exactly 10 tables;
- a large tenant with exactly 5,434 tables across multiple connections;
- duplicate display/qualified names across connections;
- varied schemas, field counts, native types, definitions, keys, nullability, glossary terms, and
  metadata fingerprints;
- every 997th large-fixture asset expanded to 64 fields with nested paths, Unicode names, source
  type drift, definition gaps, null/key variation, and tags;
- a deterministic delta of 100 updates, 23 additions, and 14 removals, yielding 5,443 active
  tables without Python-side cloning.

For source and interactive page sizes 1, 17, and 50, traverse the complete inventories exactly once
and compare ordered identities against an independently generated ground truth. Record duplicates,
omissions, order drift, last cursor, page count, materialized-item high-water mark, and generation
fingerprint. The maximum interactive materialization is 51 assets or fields.

Review `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` for connection, asset, field, exact lookup,
refresh claim, rate, capacity, and fair-worker queries on the large fixture. Record the actual
index names and plan JSON; do not assert only that an index exists.

The isolated scale runner must record operating system, architecture, Python/PostgreSQL versions,
CPU, memory, cold/warm context, page size, pool settings, and repetition count. Its local budgets
are:

- Python heap delta from small to large at or below 16 MiB;
- process RSS delta at or below 64 MiB;
- a 5,434-table full refresh at or below 60 seconds;
- one source page durably persisted before the next source page is requested.

Required targets are:

```bash
make test-scale-correctness
make benchmark-scale
```

Both targets exist. The latest operated `make benchmark-scale` evidence profile passed and wrote
`reports/m25-scale-report.{json,md}` on Darwin 25.5.0 arm64, CPython 3.13.13, PostgreSQL 16.13,
10 logical CPUs, and 16 GiB physical memory:

- exact 10/5,434 traversal at page sizes 1, 17, and 50;
- 10 assets/75 fields and 5,434 assets/41,028 fields across two active large-tenant connections;
- at most 51 rows read and 50 items materialized;
- heap/RSS deltas 126,601/0 bytes;
- 110 persisted refresh pages in 29.685331 seconds;
- 5,000 concurrency-16 reads, zero unexpected errors, p95/p99 41.762/54.951 ms;
- expected asset/field keyset indexes.

The field plan naturally selects a 64-field asset under the default PostgreSQL planner; neither
reviewed plan uses a planner override. Both contain `Index Scan` plus `Limit` and no forbidden
node. The final integrated correctness and benchmark reruns are recorded in
`tasks/M25_HANDOFF.md`.

### DataHub, load, and acceptance

Real service acceptance must refresh the exact local DataHub catalog using stable
`scrollAcrossEntities` URN order, bounded response bytes/timeouts, exact environment/profile
filtering, and no mutation method. Stop DataHub after a successful refresh: PostgreSQL must continue
serving the completed inventory with visible freshness/staleness, while a new refresh fails safely
without recorded/synthetic fallback.

The load run performs 5,000 indexed reads at concurrency 16 and records errors, throughput, p50,
p95, p99, pool wait, cold/warm state, and database/process settings. The local regression budgets
are zero unexpected errors, p95 at or below 250 ms, and p99 at or below 500 ms. These figures are
not production availability or capacity claims.

The short wall-clock smoke remains mandatory in the hosted service-free quality job with those
limits unchanged; release clean-room likewise runs `make check` before starting project services.
It carries the `performance` marker and coverage runs deselect that marker so the measurement does
not inherit pytest-cov or active DataHub/PostgreSQL service-job contention. The pure deterministic
regression gate remains selected by coverage and proves the exact error, percentile, row, and
materialization boundaries; a failure prints three-decimal sanitized metrics and the exact failed
checks. The authoritative operated check remains the explicit 5,000-read PostgreSQL benchmark
above.

Acceptance must also execute an approved one-, two-, and three-table plan against the large
tenant's governed connection, then prove that inventory size does not weaken the query rejection
matrix. Runtime packaging must discover schema v4 outside the checkout; API, execution worker, and
catalog indexer must start independently, refuse schema mismatch, never auto-migrate, and close
gracefully.

Codex's internal browser must traverse first/middle/final pages for both tenants, exercise
tampered/stale/cross-scope cursor denial, rate denial, refresh status, and stale inventory, then
repeat at 390x844 with clean console, protected-data scan, and no horizontal overflow.

Final commands include:

```bash
make test-acceptance
make evaluate
make check
make coverage
make runtime-wheel-smoke
python scripts/release_audit.py
git diff --check
```

The 5,000-read latency result is recorded above. Live DataHub refreshed the exact local
11-asset/59-field catalog into PostgreSQL; that generation remained readable after DataHub stopped,
and a later refresh failed safely without synthetic fallback. Focused package/process and query
safety cuts also passed during implementation.

Exact final DataHub scroll high-water facts, package/process output, complete test counts,
coverage, release-audit result, secret scan, and browser observations are recorded in
`tasks/M25_HANDOFF.md`; M25 is accepted locally. Those results remain local milestone evidence,
not a production scale or release claim.

## M26 semantic-change verification

M26 acceptance must prove the complete asynchronous chain, not only pure drift classification.
The final counts and every failed/corrected attempt are recorded in `tasks/M26_HANDOFF.md`; all 30
criteria are accepted locally.

### Pure and adapter contracts

The focused unit cut covers:

- canonical bindings, candidate ambiguity, metadata/join change classification, immutable
  fingerprints, complete/incomplete impact sets, decision eligibility, confirmation, replay, and
  stale compare-and-swap;
- dependency-aware gate behavior for affected versus unrelated mappings/joins, missing or
  duplicate projection rows, live catalog mismatch, latest report/profile drift, and more than one
  connection;
- scan/profile-job lifecycle, lease expiry, stale capability/fence, retry/supersession, graceful
  stop, bounded maintenance, aggregate-only payloads, and source-connection mismatch;
- complete workflow and DataHub recipe paging, duplicate/stalled/malformed/outage cases, page-level
  continuation checks, lease loss with no index advance, and exact scope-qualified recipe URNs;
- tenant-scoped report/finding/impact pages, `page_size + 1` reads, signed cursor binding, and
  indistinguishable unknown/cross-tenant denial;
- CLI inspect/prepare/approve/commit/head/audit boundaries and browser-panel protected-data rules.

Run:

```bash
.venv/bin/pytest tests/unit/test_semantic_change.py \
  tests/unit/test_semantic_change_use_cases.py \
  tests/unit/test_semantic_change_gate.py \
  tests/unit/test_http_semantic_change_api.py \
  tests/unit/test_semantic_change_cli.py \
  tests/unit/test_semantic_change_operator.py \
  tests/unit/test_semantic_change_reconciler.py \
  tests/unit/test_semantic_change_scan_runner.py \
  tests/unit/test_semantic_change_scans.py \
  tests/unit/test_semantic_change_schema_migration.py \
  tests/unit/test_semantic_dependency_reconciler.py \
  tests/unit/test_semantic_profile_bootstrap.py \
  tests/unit/test_semantic_profile_jobs.py \
  tests/unit/test_semantic_profile_process.py \
  tests/unit/test_semantic_profile_worker.py \
  tests/unit/test_semantic_reconciler_bootstrap.py \
  tests/unit/test_semantic_reconciler_process.py \
  tests/unit/test_m26_semantic_change_browser_panel.py \
  tests/unit/test_m26_browser_acceptance_runtime.py
```

### PostgreSQL, DataHub, process, and scale contracts

A pristine database and a schema-v4 upgrade must reach exact schema v5 without changing migrations
0001–0004. Integration must verify the six roles, `PUBLIC` revocation, immutable rows, scoped
catalog-generation fan-out to every matching active registry, one registry-transition scan,
idempotent replay, claim/reclaim/fencing, and the connection-qualified profile queue.
The profile claim/reclaim path is additionally workspace-qualified.

Real DataHub acceptance must use exact immutable registry versions and scope-qualified current
recipe documents. It must establish the first baseline, revalidate a compatible metadata-only
change, block type/key/null/removal and unsafe relationship drift, activate a corrected strict
registry, establish its new evidence, and preserve the old report. Coverage must remain visibly
incomplete when either paginated workflow or recipe enumeration fails.

The dynamic-catalog regression uses 10 and 5,434+ tables. Dependency enumeration pages the complete
managed artifact set, while field observation remains bounded by the active registry's 2,000
mapping/500 join maxima. An unrelated change among the other 5,433 assets must not materialize the
catalog or block an unaffected plan. The 5,434-artifact workflow fixture must demonstrate repeated
continuation/heartbeat checks.

The final focused corrections replace both connection-wide initial-candidate lookup and bound
evidence materialization with separate bounded exact-locator functions. Initial requests are
capped at 2,000/2 MB and two witnesses per mapping, while one dependency snapshot is capped at
10,000 artifacts/100,000 edges and writes PostgreSQL edges in batches of 500. The real PostgreSQL
5,434-artifact/5,434-edge dependency regression completed in 5.50 seconds including control-plane
migration/setup. Adversarial tests also cover same-scope stale workflow/recipe conservative
projection, cross-scope or unresolvable incomplete coverage, and two workspaces reusing one
connection ID without cross-claim or source call. The initial-candidate second-inspect plus
`EXPLAIN ANALYZE BUFFERS`/index cut first passed at 5,434 tables in 126.89 seconds. The complete
fixture has 5,458 fields and observes 31 governed fields with at most two candidates each. A clean
base without analyzed statistics then invalidated the warmed plan: 59,615 touched blocks, followed
by 16,333 on retained reproduction, exceeded the unchanged 4,096 bound because the planner filtered
5,433 unrelated assets per mapping. Isolating only the asset phase exposed a second 54,975-block
plan that filtered about 5,457 fields for each of 31 mappings. The final
statistics-independent form adds length-prefixed asset/field expression indexes, equality-only
lateral probes, and exact external rechecks. With autovacuum disabled and both relation estimates
unknown, cold acceptance passes 1 test in 126.66 seconds over 5,434 assets/5,458 fields/31
mappings; the 31-row function plan names both indexes, stays within 4,096 blocks, and remains below
5 seconds. Complete global acceptance then passes 20 tests in 95.49 seconds; neither raising bounds
nor running `ANALYZE` as a correctness precondition is acceptable.

An earlier real PostgreSQL/DataHub M26 lifecycle acceptance passed 1 test in 260.31 seconds
(`/usr/bin/time` real/user/sys 262.27/13.70/2.26 seconds). A later integrated rerun correctly
failed when its connection-wide initial-candidate lookup exceeded the 5-second timeout, so final
acceptance was still open at that point. The locator-first replacement's real focused acceptance
passed 1 test
in one warmed 126.89-second run without raising that timeout, but its later clean-base block-bound
failure means that result is not final evidence. The corrected cold run above supersedes it as the
focused locator proof. The browser-session seed separately passed 1 test in 116.95 seconds and
produced all four required real report states with 32/38 maximum findings/impacts; browser
inspection passed on unchanged presentation bytes, while the final backend bytes pass the full
integration/acceptance, quality, and coverage gates.

Required final matrix:

```bash
make control-plane-reset
make control-plane-migrate
make control-plane-check
.venv/bin/pytest -m integration tests/integration/test_semantic_change_postgres.py
.venv/bin/pytest -m integration tests/integration/test_semantic_change_scans_postgres.py \
  tests/integration/test_semantic_profile_queue_postgres.py
.venv/bin/pytest -m acceptance tests/acceptance/test_semantic_change_acceptance.py
make test-api-integration
make test-worker-integration
make test-integration
make test-acceptance
make evaluate
make runtime-wheel-smoke
python scripts/release_audit.py
make check
make coverage
git diff --check
```

The internal browser must show current, review-required, blocked, and remediated states; bounded
first/middle/final finding and impact pages; stale/cross-scope/cursor denial; absence of mutation
controls; zero compiler/source counters for blocked work; a clean console and protected-data scan;
and no overflow at desktop or 390x844. This real-upstream record passed locally and is preserved in
`docs/14_BROWSER_ACCEPTANCE.md`. The final command matrix also passes; M26 is accepted locally and
M27 is eligible.

## M27 dynamic Query Studio verification — accepted locally on synthetic data

M27 separates provider-free semantic retrieval evidence from live-model evidence. Passing fake or
deterministic tests must never be reported as a provider result, and helper-unit coverage must
never be reported as an internal-browser session.

The final provider-free combined Query Studio regression recorded `454 passed`. Additional focused
attestation/runtime cuts passed, as did Ruff and strict mypy. Counts from overlapping commands are
not summed in acceptance reporting; exact commands and durations belong in `tasks/M27_HANDOFF.md`.
The final boundary pins prompt `m27-openai-prompts-v16`, strict output schema
`m27-query-studio-v10`, expansion contract `m27-expansion-contract-v10`, selection contract
`m27-slot-selection-v4`, proposal normalizer `m27-proposal-defaults-v3`, matcher
`m27-deterministic-v9`, orchestration policy `m27-local-analytical-preflight-v5`, OpenAI SDK
`2.46.0`, and the closed payload-free failure taxonomy. Contract tests prove analytical expansion
is local, unique source spans/owners/types are server-derived, interpretation emits only complete
`slot_id`/`option_index=1` selections plus ambiguity, candidate/filter reconstruction stays
server-side, and top-score ties remain ambiguity before provider I/O.

Control-plane tests cover pristine and populated upgrades through schema v8, fail-closed invalid
usage/audit derivations, bounded settlement, conservative failure charging, lock/lease ordering,
and exact wrapper/core ACL metadata. Catalog-scale tests exercise the same path at 10 assets/
75 fields and 5,434 assets/41,028 fields with 31 governed mappings; executable queries remain one
connection and at most three tables/two joins.

The provider-free JSON report
`reports/m27-query-studio-deterministic-evaluation.json` is a deterministic **PASS** for matcher
`m27-deterministic-v8`, corpus SHA-256
`6bb72e6efd4801bd5042e15a29616d634118d62e5f8a83b2f19d4691b1502ece`, and report SHA-256
`09afcbbc586b60bcbebfcc521d5ee35b86c2ed647513b15c7a1541ebfaed8fbf`. It records:

- top-1 `56/62` (`0.903226`), top-3 and recall@20 `62/62`, and MRR `0.946237`;
- critical no-match specificity `31/31` and ambiguity recall `6/6`;
- 31 governed mappings with stable keyset sizes 1, 17, and 50;
- zero provider calls and zero ungoverned executable results; and
- the 5,434-decoy regression without expanding executable authority.

Equivalent natural and guided input produces the same validated-request and resolved-plan
fingerprints for the five governed query cases. Separate adversarial tests cover unknown/stale
candidate IDs, closure overflow, edit/re-sign, sensitive input, provider faults, opaque tokens,
cross-scope state, qualified-reference fail-closed behavior, and physical
`needs_mapping_review` results.

Current live-model evidence is the immutable signed
`m27-cheapest-first-campaign-v11` result. Nano was evaluated first and passed the complete corpus,
so 5.4 Nano and Luna were not called and no runtime cascade occurred:

| Model | Corpus | Quality | Provider attempts | Input | Output/reasoning | Duration | Cost |
|---|---:|---:|---:|---:|---:|---:|---:|
| `gpt-5-nano-2025-08-07` | 136 cases | PASS | 16 including qualification | 15,715 | 1,204 | 30,016 ms | EUR 0.001394085 |

The full run passed `62/62` positive recall@20, all 31 negative outcomes, `18/18` ambiguity trials,
`15/15` typed core trials, and `10/10` adversarial cases. Top-1 was `56/62`, top-3 `62/62`, and MRR
`0.946237`. The signed whole-file SHA-256 is
`beef3391aef2ea2cedfdfbc6e065ce259f7143861ccc0470197c6edcb88ba30a`; the logical report digest is
`e13a63fd2add281e567e3b4e4086ba3bb8510ec6e554608533a4c532215c0b7f`.
Earlier signed campaigns remain immutable historical results under their own contracts and are not
merged into v11 metrics.

The accepted schema-v2 ledger attestation verifies the signed campaign and one unique compatible
authenticated ordinal correlation with exactly 16 settled interpretation reservations/audits,
identical token totals, and zero missing, orphan, mismatched, or open attempts. Its SHA-256 is
`f4fceb557b8dd0a2ddf3c05a5f670ba2ea759f32273ef515e0daf0231a117053`.
Because the historical campaign did not persist one shared request nonce/fingerprint, this proves
authenticated unique ordinal correlation—not native content-derived case-to-request identity.

Internal-browser fake-mode acceptance covers desktop and 390x844, the 10/75 and 5,434/41,028
profiles, bounded guided pages, short Spanish description matching, all core typed cases,
deterministic SQL/AST/read-only preview, edit/re-sign, ambiguity/no-match/stale, rate/quota/
provider-down, physical `needs_mapping_review`, clean console, no horizontal overflow, and
protected-data scans. The one separately authorized live-UI smoke was blocked by the browser
host's URL policy before submission. It made no provider request and is not counted as a live
browser PASS. Policy v86 restored external AI to disabled immediately afterward.

M27 is accepted locally for this synthetic evidence. The dirty release tree, the blocked
live-browser smoke, M29–M31, production data/traffic evaluation, and external operational/security
sign-off remain explicit blockers to a production or release GO.

## M28 connector-routing and cost-control verification — accepted locally

M28 verification must prove that semantic approval, SQL compilation, cost admission, and every
source read refer to the same immutable public target. A passing single-DSN preview is
insufficient: tests must distinguish two workspaces that reuse the same connection, schema,
table, field, and request labels while resolving different PostgreSQL databases, read-only roles,
route revisions, budgets, and results.

The focused contract matrix covers:

- canonical target, route, source-identity, catalog-identity, type-contract, budget, guarded-query,
  and cost-assessment fingerprints;
- an additive pristine and v8→v9 control-plane migration, immutable 0001–0008 checksums, exact
  role grants, legacy non-terminal drain refusal, and retained targetless terminal history;
- exact create/rotate/disable confirmation, separate proposal and approval artifacts, compare-and-
  swap, idempotent replay, changed-payload collision, append-only audit, and public/private route
  separation;
- four non-substitutable private capabilities (`preflight`, `catalog`, `execution`, and `profile`)
  with lease, capability, fence, expiry, workspace, and connection checks;
- an OIDC v1→v2 identity rotation where job visibility/idempotency remains in the current
  workspace while the claimed worker resolves only the exact historical workflow/connector
  route; direct SQL cross-scope substitution must fail;
- owner-only, no-follow, bounded secret documents; no binding, path, DSN, password, token,
  endpoint, database topology, SQL, parameter, plan JSON, or source value in public state,
  exceptions, logs, responses, or browser text;
- explicit PostgreSQL compiler/guard equality, deterministic PostgreSQL native-type normalization,
  and zero compiler, guard, route, preflight, preview, or rejection-source calls for an unsupported
  dialect;
- exact source/catalog/type identity on every executable catalog generation, including delta-base
  equality, legacy-null denial, identity-changing rotation followed by refresh, and secret-only
  rotation that invalidates an old plan without fabricating new metadata evidence;
- route absence, disable, stale revision, unavailable secret, wrong reader, retargeting, malformed
  plan, timeout, and every configured cost-budget rejection with zero preview, including a raw
  JSON response rejected before decode, duplicate keys, an empty response, and decimal/row-count
  precision attacks that would be rounded by `float`;
- the five governed query cases under two accepted cost checks, followed by the same deterministic
  result/rejection contract; and
- unchanged catalog profiles of 10 assets/75 fields and 5,434 assets/41,028 fields, with bounded
  route lookup, pagination, and memory while one executable request remains one connection,
  three tables, and two joins.

The final scale corrections keep the reviewed safety bounds intact:

- the PostgreSQL type-contract payload recomputes and verifies its exact canonical SHA once during
  module import; field normalization reuses the verified constant in O(1);
- one extreme FULL source page writes all assets and fields under the same transaction and lease,
  batching field inserts by 500 without increasing the statement timeout;
- the statistics-independent first governed browse remains below five seconds, and each of the 34
  pooled steady-state pages remains below five seconds; cold, total-traversal, and maximum-page
  durations are retained as separate diagnostic properties;
- `test_catalog_scale_postgres` alone owns the authoritative 60-second full-refresh budget. The
  second Query Studio fixture setup records elapsed time diagnostically and does not duplicate or
  redefine that budget.

Application cost admission always uses exactly one bounded
`EXPLAIN (FORMAT JSON, COSTS TRUE, ANALYZE FALSE, BUFFERS FALSE, VERBOSE FALSE, SETTINGS FALSE)`
inside a read-only transaction and rolls it back. PostgreSQL engineering tests may separately use
`EXPLAIN (ANALYZE, BUFFERS)` on synthetic fixtures to inspect an index or measure a test budget.
That test-only diagnostic is never application SQL, never runs during a user request, and must be
labelled separately so it cannot be mistaken for the M28 preflight contract.

The final integration selection passed 164 tests with one known skip in 662.06 seconds. The final
acceptance suite passed 47 tests in 133.50 seconds, including the regression for Streamlit's empty
`MAPBOX_API_KEY` placeholder. The placeholder carries no authority and is allowed; any non-empty
sensitive environment value still blocks the browser runtime.

The post-fix final-byte quality and coverage reruns both pass. `make check` completed Ruff
format/lint, strict mypy over 276 source files, and 2,714 tests with 207 deselected in 989.17
seconds. Coverage completed the entire 2,921-test collection with 2,920 passed, one retained M27
fixture skip, and seven expected DataHub attribution warnings in 2,626.35 seconds:

```text
M28_POST_FIX_MAKE_CHECK=PASS_2714
M28_POST_FIX_COVERAGE=PASS_81.76_PERCENT
```

Exact warnings, failed intermediate runs, corrections, migration checksums, role output, scale
postflight, and final-byte identity belong in `tasks/M28_HANDOFF.md`.

The final internal-browser record used the dedicated M28 helper and real product Query Studio
composition. Desktop 1280x720 and mobile 390x844 each passed all nine scenarios. Tenant A returned
`approved_rows=2`, tenant B returned `approved_rows=3`, and their approved readers were distinct.
All seven blocked states exposed neither an action nor a result. Final-tab consoles were clean,
overflow was false, `window.__m28_xss` was undefined with zero injected scripts, and the protected
scan found zero forbidden hits. The sanitized state fingerprint was
`2b854c0596ada31ab5a80f5e6d25c23282f0096ac90a712e953476dff899be33`; cleanup left the state
absent, the port closed, and zero temporary databases or roles.

M28 is accepted locally and M29 is eligible but not started. Neither the local browser record nor
the passing local quality/coverage matrix authorizes a production or release GO.

## Quality gate

`make check` is the base gate. Milestones add focused commands. Before release, run:

```bash
make check
pytest -m integration
pytest -m acceptance
python -m schemabridge.entrypoints.cli.main evaluate --output reports/evaluation.json
```

The exact final command may evolve, but documentation and CI must match the executable interface.

For M21/M22 registry work, also run:

```bash
pytest tests/unit/test_semantic_registry.py tests/unit/test_semantic_planner.py \
  tests/unit/test_datahub_semantic_registry.py \
  tests/unit/test_semantic_registry_publication.py \
  tests/unit/test_governed_execution.py tests/unit/test_ui_view_models.py
make demo-reset-proof
make datahub-catalog-check
make datahub-registry-check
make test-integration
make test-acceptance
make evaluate
make coverage
git diff --check
```

For M23, finish with the focused/unit and service contracts above, then:

```bash
make check
make coverage
python scripts/release_audit.py
git diff --check
```

Record exact counts, digests, migration checksums, activation generations/fingerprints, and any
skips or unavailable services in the handoff. A dirty-worktree warning, missing live service, or
unrun browser/restore drill remains open evidence and is never converted into a pass.

## M29 operations and supply-chain gates

M29 adds focused adversarial tests for remote exact-version secrets and workload identity,
cross-capability denial, TLS/redirect/size/error sanitization, observer role and aggregate-only
reads, the separate read-only backup role, atomic metric snapshots, bounded process exporters,
producer-backed canonical PromQL/SLO contracts, structured logs, explicitly inactive
SIEM/uncomposed signals, Kubernetes rendering, the exact backup CronJob and PrometheusRule,
immutable workflows/images, vulnerability-report coverage,
SBOM/provenance binding, complete resolver-independent dependency audit, reproducible
sdist-to-wheel hashing, isolated audited build tooling, Docker-context secret exclusion, exact
final-stage commands, offline BuildKit installation, no retained wheelhouse, signed retention,
recovery, and rollback decisions.

The backup identity suite is end-to-end rather than a DSN-name assertion. Static migration tests
require the exact v12 role attributes, role settings, membership restrictions, absence of role
repair, and read-only grants. Unit tests mutate every observed posture field and require rejection
before the command runner. PostgreSQL integration then proves a valid backup/restore and read
coverage, write and `SET ROLE` denial, plus fail-closed migration and pre-`pg_dump` behavior for a
temporary `SUPERUSER` posture, a dangerous membership, a role-and-database-specific read-only
override, and an unbounded timeout. Entrypoint tests require path/DSN/credential-free failure
evidence.

Browser auth has both behavior and dependency-direction regressions.
`test_streamlit_auth_secrets.py` exercises HTTPS/origin/audience/secret/token-exposure rejection
through `application/ports/browser_auth.py` and
`adapters/identity/streamlit_auth.py`; `test_web_architecture.py` parses imports and rejects a
concrete-adapter import in the Streamlit entrypoint, an entrypoint import in bootstrap, or any
bootstrap/entrypoint dependency from the adapter.

The active alert validator compares each expression with the canonical PromQL token sequence.
Mutation tests keep the same metric families while changing a comparator, threshold, function, or
vector-matching operator, and also append invalid trailing syntax; every variant must fail.
Separate SLO mutations change an indicator type, remove/add a field, alter the closed outcomes, or
introduce an uncomposed metric. The Kubernetes mutation suite additionally inserts an
OpenTelemetry collector destination on TCP/4317 and requires fail-closed rejection because no OTLP
exporter is composed.

Run the local contract gates before any provider or cluster exercise:

```bash
make supply-chain-static
make m29-recovery-policy-check
kubectl kustomize deploy/kubernetes/m29/overlays/production > /tmp/rendered-m29.yaml
python deploy/kubernetes/m29/validate_rendered.py /tmp/rendered-m29.yaml
pytest -q tests/unit/test_connector_remote_secrets.py \
  tests/unit/test_control_plane_migrations.py \
  tests/unit/test_control_plane_operations.py \
  tests/unit/test_backup_entrypoint.py \
  tests/unit/test_streamlit_auth_secrets.py \
  tests/unit/test_web_architecture.py \
  tests/unit/test_m29_deployment_manifest.py \
  tests/unit/test_m29_prometheus_rule.py \
  tests/unit/test_operational_metrics.py \
  tests/unit/test_process_metrics_http_export.py \
  tests/unit/test_backup_retention.py \
  tests/unit/test_recovery_drill.py \
  tests/unit/test_supply_chain.py
```

The unpatched production overlay must fail locally because its placeholder values are intentional.
For acceptance, render a separately patched operator profile and require both the local validator
and `kubectl apply --server-side --dry-run=server` against the target cluster. A missing cluster
context is recorded as not run, never as a pass.

The final M29 matrix also requires schema-v12 PostgreSQL integration, including the v10 observer,
v11 provider-version, and v12 backup-identity migrations, plus full integration and
acceptance suites, deterministic evaluation, runtime-wheel smoke, release audit, frozen-lock
installation, vulnerability/SBOM/provenance validation, a complete fresh-target restore drill,
`make check`, coverage at or above 80%, scale postflight, and `git diff --check`. The six-state
operations view is tested last in the Codex internal browser at desktop and 390×844 mobile widths,
with hostile text literal, clean console, no horizontal overflow, and exact cleanup. Exact counts,
commands, failures/corrections, and unavailable external services belong in `tasks/M29_HANDOFF.md`.
Managed-profile browser tests additionally require the explicit not-connected/unavailable state;
synthetic operational health may appear only in development/hosted-demo.
The final local runtime candidate must additionally build with BuildKit, run as UID/GID 10001,
pass `pip check` and API/UI imports without the build backend or wheelhouse, and report zero
HIGH/CRITICAL findings under the current Trivy database. That local scan is not a registry or
protected promotion attestation.

PostgreSQL-client supply-chain tests treat the Dockerfile as a closed contract. They require the
exact Alpine 3.24 `TARGETARCH` mapping (`amd64` → `x86_64`, `arm64` → `aarch64`) and the complete
official URL, version, and architecture-specific SHA-256 matrix for
`postgresql16-client=16.14-r0`, `libpq=18.4-r0`, `lz4-libs=1.10.0-r1`,
`zstd-libs=1.5.7-r2`, and `postgresql-common=1.3-r0`. Mutating a URL, hash, version, architecture,
package set, mount mode, `--no-network`, or signature enforcement must fail static policy. The
final install must be exactly one `apk add --no-cache --no-network` over a read-only BuildKit mount;
copying or retaining the APKs, allowing an untrusted package, or adding another network action is
rejected. Runtime CycloneDX verification must find all five named Alpine package/version
components, so successful file copies without package-manager inventory are not acceptable.
The same verifier exercises both pinned base-image leaves and requires the virtual
`.python-rundeps` record to be exactly `20260616.002547/noarch` on `aarch64` or
`20260616.002554/noarch` on `x86_64`. Cross-platform substitution, a non-`noarch` purl, real APK
version drift, extra packages, and missing graph or hash evidence are negative regressions.

The release-workflow regression suite hashes and semantically validates the complete reviewed
seven-job program. The hash is a tripwire, not the only defense: mutation tests rewrite a security
property, recompute or monkeypatch the expected hash, and still require the dedicated semantic
finding. They reject extra or reordered jobs/steps/actions, dead branches, forbidden opcodes,
repository-code execution in a privileged job, authority drift, and any external mutation before
its inline boundary verifier. State and mutation tests parse the actual workflow scripts and their
control-flow branches; a standalone toy state machine is not release evidence.

The accepted topology is exactly:

1. protected GET-only `audit` runs before build, uses no checkout/action/repository code, rejects
   drafts, conflicting registry state, or a newer stable Release for a fresh dispatch, and accepts
   an existing tag only as an exact immutable file/OCI-attested historical no-op;
2. read-only `prepare` validates the stable annotated tag, exact source/CI/tag-ruleset metadata,
   requires the remote default-branch HEAD to equal `SOURCE_REVISION`, proves clean external
   public/basic state, builds and locally gates once, and emits the run-scoped prepared artifact;
   it has no protected environment or audit token;
3. protected `candidate` has package-write only and no `uses` actions, consumes the actual
   `prepare` outputs, and either creates `candidate-${{ github.sha }}` or, on a same-job retry,
   adopts only the bounded remote manifest with the exact sealed config digest;
4. read-only `scan` authenticates a pull-only GHCR credential, supplies it to pinned Trivy, removes
   it even on failure, binds the temporal scanner/DB snapshot, and emits the canonical payload;
5. protected `attest` has attestations/OIDC plus the package authority needed for OCI attestation,
   consumes the actual canonical-payload outputs, verifies first, checks the sentinel, creates the
   file/OCI attestations; a rerun may add only another exact source-bound bundle;
6. protected `promote` has package-write only, verifies first, checks the sentinel, and creates or
   verifies the same-digest stable tag; and
7. protected `release` has contents-write only plus package/attestation reads, verifies every
   boundary first, checks the sentinel, and reconciles the canonical Release.

The five protected jobs must declare `production-release`, which means five sequential
environment approvals. Tests explicitly retain the platform fact that `GITHUB_TOKEN` exists at job
start: protection comes from minimal jobs with no checkout, dependency setup, or unsealed
repository code and from first-step inline verification, not from claiming that the later sentinel
delays token issuance.

Static tests prove exact default-branch equality at every source boundary but cannot prove that
operators keep `main` unchanged between five sequential approvals. Release acceptance therefore
also requires external change-window evidence: source SHA, freeze start/end, ticket,
administrator, independent reviewer, and an approval-by-approval confirmation that no push or
merge was permitted until post-publication verification completed. The incident drill advances
`main` after a simulated mutation, expects the next boundary to fail, inventories the partial
candidate/attestation/image-tag/draft state, and proves that neither failed-job rerun nor a new
dispatch deletes, clobbers, overwrites, or bypasses it.

The acceptance record additionally proves that the whole five-approval mutation window completed
within seven calendar days of protected `audit` start. A boundary at or beyond that deadline must
stop approvals and select the preserved-state incident procedure. Static regression tests bind
both run-scoped artifacts to 35-day retention; reviewers treat the remaining 28 days as a bounded
investigation/recovery buffer, never as permission for a late rerun, redispatch, or publication.

Rerun tests simulate failure after candidate publication, draft creation, and image promotion
against the actual extracted job scripts. “Re-run failed jobs” must consume the exact upstream
artifact IDs/digests and reuse canonical bytes without rebuilding or regenerating Trivy evidence.
It may add another attestation bundle only when subject, source repository, revision, and tag are
identical. A newly dispatched workflow must detect any existing candidate, SemVer image tag,
draft/Release state, or newer stable version in `audit` before build. Tests distinguish registry
config digest from manifest digest and require both exact.

Runner tests require every job to select `ubuntu-24.04` while retaining the explicit limitation
that the hosted image and toolchain are mutable. `SOURCE_DATE_EPOCH=1730470033` is required only as
timestamp normalization and must not be described as a complete-rebuild guarantee.
`DOCKER_BUILD_RECORD_UPLOAD=false` is mandatory so the build action cannot create an undeclared
artifact.

Each privileged inline `run` script must begin exactly with `set -euo pipefail`; mutation tests
reject `set +e`, `|| true`, and a published-Release branch that invokes any release mutation. All
four privileged archive verifiers must bind the API size and digest before extraction, then use
the fixed ZIP allowlist/count, flat-path, duplicate, absolute/dot-segment, backslash/NUL,
encryption, CRC, external-attribute, symlink/device, per-file/total-size, and compression-ratio
checks. They must extract into a new directory and recheck every result as a regular,
non-symlink file. Every internal/public checksum is size-bounded and must contain the exact unique
lowercase digest/two-space/canonical-basename set before `sha256sum --check`; mutation tests inject
`../`, `/proc`, omissions, duplicates, extra lines, and oversized manifests. Resealed
workflow-hash mutations must still produce the dedicated semantic failure.

Ruleset tests reject the nonexistent `/rules/tags/$tag` contract and require the list endpoint
`GET /repos/{owner}/{repo}/rulesets?includes_parents=true&targets=tag` followed by the exact-ID
`GET /repos/{owner}/{repo}/rulesets/{id}?includes_parents=true`. `prepare` may inspect only
metadata with its normal token. Fresh `audit` and every mutation-capable privileged boundary use
the environment-only
`SCHEMABRIDGE_RELEASE_AUDIT_TOKEN`, validate its safe shape without printing it, and use it only
for `GET`; the exact rule target/include/exclude/enforcement/rules and empty `bypass_actors` are
mandatory. Documentation and validation retain the platform constraint that the token needs
repository Administration write plus Contents write scopes to see bypass actors, drafts, and
assets despite its workflow-enforced GET-only use. Operator acceptance also verifies that it is an
environment-only, repository-scoped fine-grained PAT with expiry beyond the maximum approval
window plus a recorded buffer, a tested rotation/revocation procedure, and no log exposure. A
static GitHub App installation token is rejected because the workflow does not mint it per job.

Every source-identity boundary must require `SOURCE_REVISION` to equal the current remote
default-branch HEAD; replacing equality with ancestry fails mutation tests. Immutable Releases
must be checked as enabled before the first publication mutation and at later privileged
boundaries. The draft-to-published path must re-fetch the exact Release ID and latest Release and
verify exact tag, target/source, title, body, ten assets, `draft=false`, `immutable=true`, and no
newer stable version. A replay that reaches an already published exact immutable Release must be a
strict `audit`-only no-op after hosted/OCI attestation and exact asset/body verification. This
historical path deliberately remains valid when no longer current/latest or when a newer stable
version exists, and it does not require current `main`.

Scanner-snapshot mutation tests require pip-audit 2.10.1 with explicit `pypi` service/source and
observation time, Trivy 0.69.3 at the pinned action revision, bounded registry manifests, and the
DB schema/update/download times plus exact hashes of `metadata.json` and `trivy.db`. These values
identify the temporal database used; tests reject any claim that they make the scan reproducible.

Stable-tag tests require non-prerelease canonical SemVer exactly equal to `v$project_version`.
Release tests require exactly these ten public assets:
`direct-licenses.json`, `pip-audit.json`, `provenance.intoto.json`,
`release-assets.sha256`, `release-body.md`, `release-metadata.json`,
`runtime-image.cdx.json`, `schemabridge-0.1.0-py3-none-any.whl`, `trivy-image.json`, and
`wheel.cdx.json`. They also require the GitHub UI body to equal `release-body.md` byte for byte and
require that file in the checksum manifest, metadata, and attestation subjects.

Global concurrency must remain the repository-wide non-cancelling FIFO queue with `queue: max`.
That key is supported by current GitHub Actions even though actionlint 1.7.12 predates its schema;
the local actionlint gate uses one exact narrow ignore for that diagnostic and must report no other
finding. Shell syntax and ShellCheck still run over every extracted inline script. The suite also
keeps the external NO-GO boundary visible: the API can verify the ruleset and immutable-Releases
settings but cannot prove the global absence of competing GHCR `PUT` or GitHub Release contents
writers. A current external administrator audit and a custom deployment-protection rule remain
mandatory operated prerequisites, not claims made by a static test.
