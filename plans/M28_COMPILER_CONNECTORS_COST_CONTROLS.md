# M28: Governed connector routing, explicit dialects, and query cost controls

- Status: complete; accepted locally 2026-07-28
- Timebox: 48 hours across four sequential phases
- Recommended Codex: GPT-5.6 Sol — Extra High (xhigh)
- Dependencies: accepted M27 Dynamic Query Studio and accepted M26 semantic-evidence gate

## Objective

Bind every managed executable plan to one exact tenant-scoped source connection, one immutable
connector-route revision, one explicit SQL dialect, and one governed cost budget before execution
approval. Resolve only an opaque secret binding inside the connector adapter, compile and reparse
for the exact supported dialect, run a bounded read-only `EXPLAIN` without `ANALYZE`, and execute
the guarded preview only when the current semantic evidence, route, reader identity, and cost
budget all still match.

The same worker and catalog-indexer binaries must route dynamically across any configured number
of tenant connections. A tenant may expose 10, 5,434, or more catalog tables; route lookup remains
an indexed single-connection operation and query width remains one connection, at most three
tables, and at most two approved joins.

## Product boundary

M28 makes the current compatibility boundary explicit:

| Capability | Connector | Dialect | M28 status |
|---|---|---|---|
| governed query preview | PostgreSQL 16 | PostgreSQL | supported |
| rejected-source inspection | PostgreSQL 16 | PostgreSQL | supported |
| aggregate join profiling | PostgreSQL 16 | PostgreSQL | supported |
| catalog discovery | DataHub GraphQL | metadata only | supported |
| catalog discovery | synthetic fixture | metadata only | test/demo |
| query execution | MySQL, SQL Server, Snowflake, BigQuery, other | any | typed unsupported |
| cross-connection query/federation | any | any | forbidden |

PostgreSQL-only execution preserves D005 and the accepted SQL safety model. A new executable
dialect requires a new ADR plus real compiler, independent guard, cost-preflight, read-only
identity, timeout, result, and adversarial evidence. An unsupported dialect may be cataloged but
cannot compile, obtain an execution checkpoint, or reach a source adapter.

M28 does not add raw SQL, LLM SQL, automatic semantic approval, source writes, cross-connection
joins, federated query engines, production secret-manager operation, observability/SIEM, HA/DR,
SBOM/provenance, pentesting, release thresholds, pilot SLOs, or GA sign-off. M29–M31 own those
later gates.

## Architectural decision

Keep `QueryPlan` as the credential-free, vendor-neutral restricted IR. Add an immutable governed
execution target to `ResolvedSemanticPlan` only after the current M26 evidence proves that every
selected mapping and join belongs to exactly one connection.

The target contains:

- workspace and exact `CatalogConnectionId`;
- closed connector kind and SQL dialect;
- immutable route revision and public route fingerprint;
- expected read-only database identity;
- immutable type-contract and cost-budget fingerprints;
- no DSN, endpoint, username password, token, secret path, or credential-binding reference.

The resolved-plan fingerprint includes the complete public target, so workflow checkpoints,
execution approvals, background-job authorization, retries, recipes, and audit references cannot
silently move a query to another connection or route revision. `CompiledQuery` and
`ValidatedQuery` also carry the exact dialect and target fingerprint; the guard rejects any
mismatch before source I/O.

`SemanticContextGateAssessment` returns the one connection it already proves internally. Eligible
assessments require exactly one connection; ineligible assessments expose no substitute. The
browser's candidate locator is never the routing authority.

## Pure domain contract

Add a pure `domain/connectors.py` contract containing:

- `SourceConnectorKind` and `SourceDialect` closed enums;
- immutable `GovernedExecutionTarget`;
- immutable `QueryCostBudget` and sanitized `QueryCostAssessment`;
- route lifecycle states, confirmations, proposals, approvals, revisions, and fingerprints;
- closed connector/type/dialect capability matrix;
- stable route/cost rejection codes;
- PostgreSQL native-type normalization into the existing `PhysicalValueType`.

All text, counts, decimals, timestamps, and collections are bounded. Cost values must be finite,
non-negative, and canonical. A target fingerprint covers workspace, connection, connector,
dialect, route revision, expected reader, type contract, and cost budget. Secret references are
private adapter values and are forbidden from public domain models.

Unknown native types remain explicit `unknown`/unsupported evidence. They never trigger an
implicit cast or mapping approval. Identifier floats retain the existing finite, integral, and
exact-range rejection policy.

## Durable route and control-plane contract

Add immutable migration `0009_tenant_connector_routing.sql`; migrations 0001–0008 remain
byte-for-byte unchanged.

The additive v9 schema provides:

- append-only public connector-contract revisions per workspace/connection;
- append-only private route revisions containing only an opaque secret-binding reference;
- one compare-and-swap current route head;
- immutable activation/rotation/disable audit;
- exact target identity on new execution jobs and semantic profile jobs;
- a drained-queue preflight for legacy non-terminal jobs lacking a target;
- fixed-`search_path`, `SECURITY DEFINER` route reads bound to the current connection and, where
  applicable, the exact live refresh/job/profile lease owner, capability digest, fence, and expiry.

Route create, rotate, and disable use an expected revision, exact operation confirmation, trusted
operator role, actor, time, immutable fingerprint, and audit record. The API never accepts DSNs,
tokens, passwords, endpoints, or secret files. It cannot read private routes or activate route
heads.

The runtime, execution worker, profile worker, and catalog indexer receive only the minimum route
capability for their operation. They receive no direct table `SELECT` over private routes.
Cross-role calls, stale fences, expired leases, wrong workspace/connection, disabled connections,
stale route revisions, and future schema versions fail through one sanitized boundary.

## Secret-resolution boundary

Application ports work with public targets and opaque route handles only. Concrete connector
adapters resolve the private binding immediately before use.

For local/integration evidence, an owner-only connector-secret directory maps the SHA-256 of the
opaque binding reference to a strict bounded JSON document. The reader:

- rejects symlinks, traversal, non-regular files, group/world permissions, ownership changes,
  oversized files, duplicate/unknown keys, malformed URLs, embedded query credentials where
  forbidden, incompatible dialects, and expected-reader mismatch;
- reads with no-follow semantics and verifies metadata before and after reading;
- returns secret material only inside the adapter, with `repr=False`;
- never logs, persists, fingerprints, serializes, or exposes the path, document, DSN, token,
  password, endpoint, or binding reference.

M29 must operate the equivalent remote secret-manager integration, rotation, workload identity,
network policy, and alerting before production GO. The local owner-only adapter is evidence of the
port and safety contract, not a remote secret-manager claim.

Catalog discovery uses its existing lease-bound catalog route but resolves the binding dynamically
instead of comparing it with one global DataHub token. Query, profile, and catalog bindings are
separate capabilities; one cannot be substituted for another.

## Planning, compilation, and execution flow

```text
Confirmed ValidatedAnalyticalRequest
    → load exact active registry
    → pure semantic resolution
    → derive exact M26 dependencies
    → M26 gate proves one current connection
    → resolve current public connector target
    → bind target into ResolvedSemanticPlan fingerprint
    → select exact dialect compiler
    → compile typed IR
    → independent same-dialect AST guard
    → load exact current private route
    → bounded EXPLAIN (FORMAT JSON, ANALYZE FALSE)
    → persist sanitized assessment only
    → human execution approval bound to target-bearing plan
    → repeat semantic gate + route resolution + EXPLAIN
    → bounded read-only SELECT
    → repeat gate + same target for rejected-source inspection
```

A changed semantic head, connection, route revision, expected reader, type contract, cost budget,
dialect, compiler, guard, or query fingerprint after confirmation is stale. It requires a new
prepared workflow and approval; it never redirects an approved job.

Every execution job persists the public target identity in its authorization fingerprint. Worker
route resolution must match the exact current leased job, worker, capability, fence, expiry,
workspace, connection, dialect, route revision, and target fingerprint. Profile jobs use their
separate lease-bound capability. Direct local demo composition may use an explicit single recorded
target; staging/production may not fall back to global `DATABASE_URL`.

## SQL dialect contract

`PostgresQueryCompiler` emits `SourceDialect.POSTGRESQL`. `SqlGlotPolicyGuard` is configured for
and reparses the same dialect. A compiler output without a dialect, a guard invoked for another
dialect, a query/target fingerprint mismatch, or an unregistered compiler/guard pair fails before
route or source access.

All accepted controls remain:

- exactly one `SELECT` or `WITH ... SELECT`;
- no DDL, DML, utility statement, concealed statement, comments, locking read, wildcard, recursive
  CTE, unknown asset/column/function, self join, Cartesian join, or unbound parameter;
- one connection, at most three tables and two approved joins;
- exact mapping/join predicates and transformations;
- fanout mitigation, result limit, statement timeout, expected reader, read-only transaction,
  fetched-row cap, and rollback.

The existing SQL attack fixture is executed against every supported dialect entry. Unsupported
entries have zero compiler, guard, explain, preview, or rejected-source calls.

## PostgreSQL cost preflight

The connector runs only:

```sql
EXPLAIN (
  FORMAT JSON,
  COSTS TRUE,
  ANALYZE FALSE,
  BUFFERS FALSE,
  VERBOSE FALSE,
  SETTINGS FALSE
) <independently validated SELECT>
```

It never runs `EXPLAIN ANALYZE`. It uses the exact validated SQL and bound parameters in a
read-only transaction, a smaller independent timeout, the expected reader, a response-byte cap,
and rollback. The parser rejects duplicate/unexpected structure, non-finite/negative numbers,
missing required fields, excessive depth/nodes/bytes, or multiple plans.

One route's immutable budget bounds:

- planning timeout;
- JSON response bytes;
- total cost;
- estimated root rows;
- maximum node count and depth;
- maximum plan width.

The assessment records only accepted/rejected, stable reasons, total cost, root rows, width,
node count, depth, observed reader, read-only fact, timeout, target fingerprint, budget
fingerprint, and its own fingerprint. Raw plan JSON, relation/index names, SQL, parameters,
values, credentials, and topology are transient and never stored or returned to the browser.

Preflight runs once before the execution checkpoint so the operator can see the sanitized result,
and again immediately before the preview. Any rejection or failure performs zero preview I/O.

## Dynamic catalog and type normalization

The catalog indexer resolves each lease-owned route by workspace/connection instead of selecting
one global adapter by source kind. A single process can serve bounded pages from multiple
connections without materializing all routes or inventories.

Catalog asset/field values populate the already-reserved `platform_instance`, `database_name`, and
`normalized_type` only from a connector-specific deterministic contract. PostgreSQL aliases,
arrays, domains, timestamps/time zones, numeric precision, binary, JSON/struct, and unknown types
have explicit cases. Metadata names and types remain evidence; they do not create mappings.

The generation fingerprint covers newly populated values. A type-contract version change creates
new evidence and triggers the existing M26 review/blocking policy rather than silently rewriting
an approved registry.

## Failure taxonomy and observability boundary

Stable external states include:

- `connector_target_unavailable`;
- `connector_route_stale`;
- `connector_route_disabled`;
- `connector_secret_unavailable`;
- `connector_dialect_unsupported`;
- `connector_type_unsupported`;
- `query_cost_rejected`;
- `query_cost_timeout`;
- `query_cost_unavailable`;
- `query_cost_invalid`;
- existing semantic, SQL-policy, source-timeout, and source-unavailable states.

Messages and logs contain operation, component, public target fingerprint, dialect, route revision,
cost decision/reason, duration, and counts only. They contain no secret binding, DSN, endpoint,
database name, username beyond the approved public reader identity, SQL, parameters, plan JSON,
source value, row, prompt, token, or claim.

## Acceptance criteria

- [x] Plan, prompt, and ADR 0013 are reviewed before source changes.
- [x] `QueryPlan` remains vendor-neutral and credential-free.
- [x] Pure bounded connector/target/budget/assessment values have canonical fingerprints and no
      secret surface.
- [x] Eligible M26 gate assessments expose exactly one `CatalogConnectionId`; ambiguous or missing
      connections remain ineligible.
- [x] Managed `ResolvedSemanticPlan` carries one target, and its fingerprint changes for workspace,
      connection, dialect, route revision, reader, type contract, or budget changes.
- [x] Workflow checkpoint, job authorization, retry, and recipe compatibility bind the
      target-bearing plan and reject stale target substitution.
- [x] PostgreSQL is the only executable dialect; unsupported dialects cause zero compiler/route/
      explain/source calls.
- [x] Compiler output and independent guard both carry and verify the exact dialect and target
      fingerprint.
- [x] Existing SQL safety/fanout/table/join/result/timeout controls remain unchanged.
- [x] Migration v9 is additive; pristine and v8→v9 upgrades pass while 0001–0008 checksums remain
      unchanged.
- [x] Connector contracts/routes/audits are immutable, versioned, expected-revision/CAS guarded,
      exactly confirmed, and contain no credential material.
- [x] Legacy non-terminal jobs without public target identity block upgrade until drained or
      explicitly cancelled; history is not guessed.
- [x] API, web, worker, profile, catalog, reconciler, and migrator route privileges are exact;
      `PUBLIC` and cross-capability reads fail.
- [x] Execution and profile route reads require the exact current lease owner, capability, fence,
      expiry, workspace, connection, revision, and fingerprint.
- [x] Secret resolution rejects unsafe files/documents and leaks no binding, path, DSN, token,
      endpoint, or password through exceptions, repr, logs, state, responses, or browser.
- [x] Two workspaces using the same `connection_id`, schemas, tables, columns, and request resolve
      to distinct source roles/databases/results with zero cross-tenant read.
- [x] Route absence, disable, stale revision, rotation, wrong target, wrong reader, and secret
      outage fail before preview.
- [x] Exact replay is idempotent; changed route payload with reused idempotency identity conflicts.
- [x] Catalog DataHub credentials resolve per exact lease-owned route; no global binding comparison
      or silent recorded fallback remains in managed mode.
- [x] Profile workers route dynamically and cannot claim or resolve another workspace/connection
      job.
- [x] `platform_instance`, `database_name`, and governed normalized types populate deterministically
      where supported; unknown/incompatible types remain non-executable evidence.
- [x] `EXPLAIN` uses `ANALYZE FALSE`, the validated SQL, parameters, expected reader, read-only
      transaction, independent timeout, response cap, and rollback.
- [x] Cost assessment rejects total-cost, row, width, node, depth, byte, timeout, unavailable,
      malformed, non-finite, negative, and target/budget mismatch paths.
- [x] A rejected/failed preflight performs zero preview; a route or statistics change causes a
      second preflight and cannot bypass the current budget.
- [x] No raw PostgreSQL plan, relation/index topology, SQL, parameter, source value, or result row
      enters durable cost state or public responses.
- [x] Five governed query cases retain exact deterministic compilation/results/rejections under an
      accepted preflight.
- [x] Inventory profiles at 10/75 and 5,434/41,028 retain bounded route lookup, pagination, memory,
      and query-width behavior.
- [x] Internal-browser desktop and 390x844 acceptance shows connection label, PostgreSQL dialect,
      route revision, and sanitized cost decision; route/cost/unsupported states expose no execute
      action.
- [x] Browser console is clean, no horizontal overflow exists, hostile metadata is escaped, and
      scans find zero secret, DSN, endpoint, SQL parameter, raw plan, identity, or source-value
      leakage.
- [x] Focused unit/schema/API/worker/profile/catalog tests, PostgreSQL integration, acceptance,
      evaluation, runtime wheel, release audit, `make check`, coverage >=80%, scale postflight, and
      `git diff --check` pass on final bytes.
- [x] State, decision log, architecture/security/query/test/deployment/runbook/browser docs, exact
      commands, failures/corrections, checksums, limitations, and M28 handoff match actual evidence.

## Implementation sequence

1. Add plan/prompt/ADR and pure connector target, dialect, route, type, and cost values with tests.
2. Return exact connection identity from the M26 gate and bind a public target into the resolved
   plan, workflow checkpoint, query metadata, recipe compatibility, and job authorization.
3. Add schema v9 route contracts/revisions/heads/audit, lease-bound reads, role separation,
   operator flow, migration/ACL tests, and target-bearing jobs.
4. Add owner-only secret resolution and exact PostgreSQL/DataHub connector factories; remove
   managed global source/catalog binding fallback.
5. Make compiler/guard dialect explicit and add PostgreSQL type normalization.
6. Add bounded PostgreSQL cost preflight and sanitized workflow/UI assessment; rerun before
   preview and route rejection inspection through the same target.
7. Route execution worker, profile worker, catalog indexer, local UI, and tests through the new
   ports; keep recorded/demo paths explicitly labeled.
8. Run two-tenant source isolation, route rotation/disable, cost, scale, API/worker/profile/catalog,
   package, evaluation, and security gates.
9. Complete the internal-browser matrix on final bytes, then consolidate docs/state/handoff.

No phase is accepted independently and M29 cannot start until every criterion passes.

## Required automated checks

```bash
pytest -q tests/unit/test_connector_domain.py
pytest -q tests/unit/test_semantic_change_gate.py tests/unit/test_governed_execution.py
pytest -q tests/unit/test_compiler.py tests/unit/test_sql_guard.py
pytest -q tests/unit/test_postgres_cost_preflight.py tests/unit/test_postgres_preview.py
pytest -q tests/unit/test_connector_schema_migration.py tests/unit/test_connector_routing.py
pytest -q tests/unit/test_worker.py tests/unit/test_semantic_profile_worker.py
pytest -q tests/unit/test_catalog_indexer.py tests/unit/test_datahub_scroll_source.py
pytest -q tests/integration -k "connector or route or cost or query_preview or background_job"
pytest -q tests/acceptance -k "connector or query_studio or governed"
make control-plane-reset
make control-plane-migrate
make control-plane-check
make test-integration
make test-acceptance
make evaluate
make runtime-wheel-smoke
python scripts/release_audit.py
make check
make coverage
git diff --check
```

Record exact commands, durations, warnings, failures, and corrections in `tasks/M28_HANDOFF.md`.
Focused success never substitutes for the complete final gate.

## Manual internal-browser test

1. Prepare two synthetic workspaces with the same connection/table/field names but distinct
   PostgreSQL databases, read-only roles, route revisions, budgets, and results.
2. Start the authenticated Query Studio and route/cost acceptance services without exposing
   connector secrets to the browser process.
3. At desktop size, confirm a governed request and inspect its public connection label,
   `postgresql` dialect, route revision, and accepted sanitized preflight before approval.
4. Execute and verify the exact database role/result belongs to that tenant; repeat for the other
   tenant and prove no cross-scope result.
5. Exercise `cost_rejected`, `route_disabled`, `route_stale`, `route_unavailable`,
   `explain_timeout`, and `unsupported_dialect`; none may show or enable execution.
6. Rotate the route after confirmation and verify stale state requires a new plan/approval rather
   than redirecting the old workflow.
7. Repeat at 390x844; inspect console, widths, rendered hostile metadata, browser/network text,
   control state, and logs for protected-data leakage.
8. Use only Codex's internal browser for the final manual OK. If URL policy blocks navigation
   before the application loads, record `blocked_before_application`, do not bypass it, and do not
   accept M28 until a permitted internal-browser path is available.

## Completion contract

M28 is complete only when all acceptance criteria and automated/manual gates pass on the same final
bytes; `tasks/PROJECT_STATE.md`, `tasks/DECISION_LOG.md`, `tasks/CURRENT_TASK.md`, and
`tasks/M28_HANDOFF.md` are accurate; the operator manual test is documented; and the final response
uses `tasks/HANDOFF_TEMPLATE.md`.

Local M28 acceptance still is not production/release GO. M29 operations/supply-chain, M30
production evaluation/security verification, M31 pilot/GA, a reviewed clean release identity,
remote provider/secret governance, operated infrastructure, and external sign-off remain blockers.
