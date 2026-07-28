# ADR 0013: Governed connector routing and cost preflight

- Status: accepted for M28 implementation
- Date: 2026-07-28

## Context

M20 isolates workflow control-plane state, M25 gives catalog resources a tenant/connection identity,
and M26 proves that every mapping and join selected by one executable plan belongs to exactly one
current connection. Execution nevertheless still uses one deployment-wide `DATABASE_URL`;
catalog discovery compares a lease-owned opaque route with one global DataHub token; and the
PostgreSQL compiler and guard assume their dialect without carrying it in the executable contract.

That creates four production blockers:

1. an approved plan is not fingerprint-bound to its source connection or route revision;
2. two allowlisted tenants in one deployment can still share data-plane credentials;
3. connector rotation or disablement cannot invalidate an already approved target explicitly;
4. a syntactically safe query can still exceed a source-specific estimated cost budget.

The existing catalog route is a metadata-discovery capability. Reusing it as a business-data read
credential would collapse least-privilege boundaries. Adding several dialects without independent
compiler, guard, cost, reader, and adversarial evidence would also weaken the accepted SQL safety
model.

## Decision

### Bind the public target before approval

Keep `QueryPlan` as the vendor-neutral, credential-free restricted IR. Once the M26 gate proves one
current connection, bind an immutable public execution target into `ResolvedSemanticPlan` before
its fingerprint is computed.

The target contains workspace, connection, connector kind, PostgreSQL dialect, route revision and
fingerprint, expected read-only identity, type-contract fingerprint, and cost-budget fingerprint.
It contains no secret binding, DSN, endpoint, token, password, or secret path.

Workflow checkpoints, background-job authorization, retry, query recipes, compiler output, and
guard output bind that target-bearing plan. A route or budget change therefore makes the old
workflow stale; it never redirects an existing approval.

### Use separate versioned route capabilities

Create append-only connector-contract and private-route revisions with one compare-and-swap head
and immutable audit. Catalog, query execution, and aggregate profiling use separate route
capabilities. Control-plane state stores only an opaque binding reference.

Route reads are fixed-`search_path`, `SECURITY DEFINER` operations. Refresh, execution, and profile
workers must present their exact current lease owner, capability, fencing token, expiry,
workspace, connection, revision, and target fingerprint before receiving an opaque route handle.
API and public UI responses cannot read private routes.

The concrete adapter resolves the binding immediately before use. Local evidence uses strict
owner-only files addressed by a digest of the opaque reference. Production remote secret-manager
operation, workload identity, network policy, and alerting remain an M29 gate.

### Keep the executable dialect matrix closed

PostgreSQL 16 is the only M28 executable source dialect. The compiler, independent SQLGlot guard,
cost preflight, preview, rejection reporter, and profiler all declare and validate that same
dialect and target fingerprint.

Other dialects may appear as catalog metadata but are typed unsupported. They receive zero
compiler, route, cost, or source calls. Cross-connection federation remains forbidden.

A new dialect requires a new ADR and real service-backed compiler, independent parser/guard,
read-only identity, timeout, cost-preflight, result, type-normalization, and adversarial evidence.

### Preflight estimated cost without executing the query

After independent AST validation and before the execution checkpoint, PostgreSQL runs only bounded
`EXPLAIN (FORMAT JSON, COSTS TRUE, ANALYZE FALSE, BUFFERS FALSE, VERBOSE FALSE, SETTINGS FALSE)`
with the exact validated SQL and parameters.

The operation uses the expected reader, a read-only transaction, an independent planning timeout,
a response-size cap, and rollback. It rejects excessive total cost, estimated rows, width, node
count, depth, bytes, malformed structure, non-finite/negative values, timeout, or unavailability.
It never uses `EXPLAIN ANALYZE`.

Only sanitized aggregate facts and fingerprints may persist or reach the browser. Raw plan JSON,
relation/index names, SQL, parameters, topology, credentials, source values, and rows remain
transient. Route, semantic evidence, and cost are checked again immediately before the preview.

## Consequences

- Managed execution no longer accepts one global source DSN as a routing fallback.
- Two tenants may use identical connection/schema/table/field labels while resolving to distinct
  approved routes and read-only identities.
- Credential rotation requires an immutable route revision and a newly confirmed target-bearing
  plan; this is deliberate safety friction.
- PostgreSQL statistics may change between preflight and execution, so the budget is checked again
  rather than treating an earlier estimate as authorization.
- Cost estimates are safety/admission evidence, not billing forecasts or production SLOs.
- The catalog indexer can resolve multiple lease-owned DataHub bindings dynamically without
  loading the tenant inventory or all routes into memory.
- Schema v9 must reject or require draining non-terminal legacy jobs that lack a target; it cannot
  infer one from names or current configuration.
- M28 local file-backed secret evidence does not satisfy M29's remote secret-manager or operated
  infrastructure gate.

## Rejected alternatives

- **Keep one `DATABASE_URL` per deployment:** preserves the heterogeneous-tenant data-plane leak.
- **Select a route only inside the executor:** leaves human approval and jobs vulnerable to silent
  target substitution.
- **Copy DSNs into jobs or control tables:** expands credential exposure and persistence.
- **Reuse the catalog DataHub binding for source reads:** violates capability separation.
- **Resolve the connection from browser-selected candidates:** trusts presentation state instead
  of the current governed evidence gate.
- **Run `EXPLAIN ANALYZE`:** executes the query and defeats pre-execution cost control.
- **Persist raw plan JSON:** exposes source topology and creates unnecessary sensitive state.
- **Add several dialect renderers without real guards and services:** creates unsupported safety
  claims.
- **Implement federation now:** weakens the one-connection invariant and requires a separate
  engine, authorization, data-movement, fanout, and cost model.
