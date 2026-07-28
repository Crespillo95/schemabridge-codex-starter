# M25: Scale, indexing, pagination, and tenant capacity

- Status: complete; accepted locally
- Timebox: 36 hours
- Recommended Codex: GPT-5.6 Sol — Extra High (xhigh)
- Dependencies: accepted M24 authenticated API/durable-worker baseline

## Objective

Replace deployment-sized catalog assumptions with a tenant-scoped, dynamically sized connection,
table, and field inventory. One organization may expose 10 tables and another 5,434 or more,
distributed across any configured number of connections. Discovery must stream bounded pages into
PostgreSQL, promote complete generations atomically, expose signed keyset pagination, and preserve
constant application-memory behavior.

M25 also closes the capacity gaps intentionally left by M24: bounded PostgreSQL pools, distributed
per-principal request limits, tenant execution-job admission quotas, fair worker scheduling, queue
depth, and reproducible local load measurements. These measurements are local regression budgets,
not production SLO evidence.

Catalog capacity remains independent from query capacity. A compiled request still uses one
governed connection, at most three physical tables and two joins, and the existing allowlist,
fanout, row-limit, timeout, deterministic compiler, and independent AST guard. Cross-connection
execution is rejected until M28 defines an explicit connector/federation contract.

## Scope assumptions

1. `PhysicalDatasetRef` remains the SQL-facing `schema.table` identity. M25 introduces a separate
   inventory locator containing workspace, connection, and catalog asset identity.
2. PostgreSQL control schema v4 is additive. Migrations 0001–0003 remain byte-for-byte unchanged.
3. Connections persist only public metadata plus an opaque external credential-binding reference.
   DSNs, tokens, passwords, secret paths, raw claims, samples, and source rows are forbidden.
4. API callers may register or disable connection metadata and request refresh only as
   `platform_admin`; no API process receives a DataHub token or source credential.
5. A separately credentialed catalog-indexer process resolves the external binding, reads metadata,
   and writes invisible refresh generations. It receives no source write, LLM, publication,
   execution-worker, migrator, or identity-pseudonymization capability.
6. DataHub discovery uses `scrollAcrossEntities` with stable URN ordering, bounded response bytes,
   timeouts, and cursor progress checks. Deep interactive reads never use DataHub `OFFSET`.
7. DataHub provides a paged full reconciliation in M25. The generic port also supports real delta
   pages for change-feed-capable sources; the product must not relabel a full scan as incremental.
8. M25 indexes both assets and fields so M27 can retrieve a small candidate set without catalog-wide
   all-pairs matching. M27 still owns semantic description matching, ambiguity, and Query Studio.
9. Tenant capacity policies are durable data, not code constants. A missing policy fails closed.
10. M25 uses no OpenAI request. The existing key remains reserved for M27 and is forbidden from the
    API, execution worker, and catalog indexer.
11. Schema-v4 columns `platform_instance`, `database_name`, and `normalized_type` are reserved for
    later governed connector/type contracts. M25 writes them as `NULL`, does not expose them in
    domain or HTTP models, and makes no claim that they are populated discovery features.

## Public contracts

### Authenticated HTTP

- `GET /v1/catalog/connections` returns one bounded tenant-scoped page.
- `POST /v1/catalog/connections` registers or exactly replays approved public connection metadata
  plus an opaque credential-binding reference; it accepts no credential material.
- `POST /v1/catalog/connections/{connection_id}/disable` performs a logical disable only.
- `GET /v1/catalog/connections/{connection_id}/assets` returns a signed keyset page from one active
  generation.
- `GET /v1/catalog/connections/{connection_id}/assets/{asset_id}/fields` returns a signed keyset
  field page.
- `POST /v1/catalog/connections/{connection_id}/refreshes` requests one full or delta-capable
  refresh without performing catalog I/O in the API.
- `GET /v1/catalog/refreshes/{refresh_id}` exposes status, generation, bounded counts, fingerprint,
  and safe reason codes only.

Every list page is at most 50 items and reads at most 51 rows. Cursors are URL-safe opaque values,
at most 1,024 bytes, expire after 15 minutes, and are HMAC-bound to format version, workspace
digest, resource scope, generation, normalized filter digest, and final sort key. Tampered,
cross-tenant, cross-connection, cross-filter, expired, or unavailable-generation cursors fail with
one sanitized boundary.

### Catalog refresh

The closed refresh states are:

`requested → leased → staging → completed`

`requested | leased | staging → failed`

`leased | staging → requested` after an expired lease is safely reclaimed

Only one active refresh may exist per workspace/connection. Pages and their source checkpoint are
committed atomically and replay idempotently. A full refresh writes a new invisible generation. A
delta refresh clones the current generation inside PostgreSQL and applies only typed upsert/delete
events. Completion verifies base generation, quota, counts, fingerprints, and source completion,
creates append-only tombstones, then changes `active_generation` in the same transaction. Failure
or crash never exposes a partial generation.

### Capacity and pools

- PostgreSQL-backed fixed-window admission limits authenticated API requests across replicas and
  returns `429` plus bounded `Retry-After`.
- Tenant execution-job capacity is reserved/released transactionally and cannot be exceeded by two
  API replicas racing.
- Worker claim rotates fairly among workspaces before choosing the oldest eligible job inside the
  selected workspace.
- API, execution-worker, and catalog-indexer control-plane pools have explicit min/max size,
  acquisition timeout, maximum waiters, startup check, graceful close, and sanitized saturation.
- Pool limits are process limits; deployment-wide capacity is the configured pool maximum times the
  bounded replica count. The scale report records both.

## Deliverables

- Add pure domain values for connection/asset/field locators, capacity policies, refresh modes and
  lifecycle, source changes, generations, page keys, and sanitized inventory summaries.
- Add application ports/use cases for connection management, refresh request/claim/page/checkpoint/
  completion, tenant-scoped listing, cursor encoding, quota admission, and scale reporting.
- Add migration `0004_dynamic_catalog_inventory.sql` with tenant policies, API rate state, job
  capacity/fair-scheduling state, connections/routes, refresh runs, versioned assets/fields,
  tombstones, constraints, partial unique guards, and compound indexes.
- Add a dedicated `schemabridge_catalog` PostgreSQL role and independent catalog-indexer process.
  Preserve the five M24 roles and prove the complete six-role negative grant matrix.
- Add SECURITY DEFINER functions only where direct table grants would exceed a role's contract.
  Every function has fixed `search_path`, explicit grants, input checks, and no PUBLIC access.
- Implement PostgreSQL adapters with keyset tuple comparisons and `LIMIT page_size + 1`; no
  inventory list SQL may use `OFFSET` or materialize a total collection.
- Implement a DataHub GraphQL scroll source with stable URN sort, skip-highlighting/aggregates,
  bounded timeouts/response sizes, exact environment/profile filtering, and no mutation surface.
- Add a lazy synthetic source with heterogeneous schemas, duplicate names across connections,
  varied field counts/types/descriptions, deterministic full/delta pages, and sparse 64-field
  assets with nested/Unicode/type-drift cases.
- Add bounded Psycopg pools as an explicit runtime dependency, inject them at the composition root,
  and close them through API/worker/indexer lifecycle hooks.
- Extend the authenticated API with strict schemas, RBAC, cursor/rate-limit handling, safe errors,
  and no protected routing or credential output.
- Add `schemabridge-catalog --once` and continuous polling entrypoints; neither auto-migrates.
- Add a version-checked, exactly confirmed capacity-policy operator with immutable per-workspace
  revisions; policy changes must not require a code release or direct ad-hoc SQL.
- Include field tags and glossary terms in the indexed full-text document for M27's bounded
  retrieval, without treating search hits as semantic approval.
- Add a bounded, hardened Kubernetes catalog-indexer Deployment with independent credentials,
  pod-derived lease identity, and schema-only readiness.
- Add a scale marker, correctness/load Make targets, reproducible JSON/Markdown report, index-plan
  evidence, local latency/heap envelopes, and documented hardware/test conditions.
- Update architecture, security, query pipeline, test strategy, deployment, runbook, UI handoff,
  ADR, project state, decision log, current task, work queue, and milestone handoff.

## Implementation sequence

1. Specify M25 domain, cursor, quota, refresh, and pagination contracts with adversarial unit tests.
2. Add schema v4 and six-role PostgreSQL integration tests before concrete inventory behavior.
3. Implement connection management, refresh staging/finalization, keyset reads, tombstones, and
   quota/fair-scheduling adapters.
4. Add lazy synthetic full/delta sources and prove 10/5,434-table correctness and bounded memory.
5. Implement DataHub GraphQL scrolling and exact live 11-table/field reconciliation.
6. Add bounded pools and inject one lifecycle-managed pool per API/worker/indexer process.
7. Add authenticated catalog routes, distributed rate limits, job admission quotas, and fair claim.
8. Run concurrent load/index/heap tests, then full integration, acceptance, evaluation, package,
   release-audit, and browser gates.
9. Consolidate documentation, exact evidence, limitations, and handoff before accepting M25.

## Acceptance criteria

Every criterion below has final automated or operated evidence. M25 is accepted locally.

- [x] Tenant A with 10 tables and tenant B with 5,434 tables across multiple connections are
      generated and refreshed without a hardcoded inventory total or full Python collection.
- [x] The same `schema.table` and field path may exist in different tenant connections without
      identity collision or cross-tenant disclosure.
- [x] Connection count and asset/field quotas are tenant-scoped durable policy values; changing a
      policy changes capacity without code, restart, or schema migration.
- [x] A platform administrator can create or revise that policy through expected-version and
      exact-confirmation controls; only the migrator database capability applies it and an
      immutable revision row preserves every accepted version.
- [x] No catalog connection, route, refresh, asset, field, tombstone, API body, response, event, or
      log contains a DSN, token, password, raw claim, OpenAI key, source row, or sample value.
- [x] Registration, disable, and refresh request require current `platform_admin`, exact tenant
      scope, explicit confirmation, and idempotency; read roles receive only public metadata.
- [x] The API process performs no DataHub/source I/O and the catalog indexer has no source-write,
      workflow-execution, LLM, publication, migration, or cross-role capability.
- [x] Full discovery uses stable cursor scrolling and bounded pages/responses. Missing cursor
      progress, repeated pages, oversized payloads, malformed assets, and source outage fail typed
      and sanitized.
- [x] Page sizes 1, 17, and 50 traverse every 10- and 5,434-table inventory exactly once with no
      duplicate, omission, or order drift.
- [x] Inventory SQL uses keyset tuple comparison and reads at most `page_size + 1`; source and
      interactive pagination contain no deep `OFFSET`.
- [x] Field full-text indexing covers names, definitions, native types, tags, and glossary terms
      for bounded later retrieval without creating an approved semantic mapping.
- [x] Signed cursors reject tampering, unsupported version, expiry, another tenant/connection/
      asset/filter/generation, excessive length, and changed sort context without protected reads.
- [x] Refresh pages plus checkpoints are atomic and idempotent; a crash after any page leaves the
      prior active generation completely visible and can resume after lease expiry.
- [x] Two concurrent refreshers cannot own one connection. Capability/fence and compare-and-swap
      generation checks reject stale completion.
- [x] Full completion promotes one exact generation atomically. Delta completion applies 100
      updates, 23 additions, and 14 removals as 137 events, yielding 5,443 active tables without
      Python-side cloning.
- [x] Table and field removals create immutable typed tombstones; disabled connections and stale
      generations cannot become query candidates.
- [x] Generation retention is bounded, never removes the active generation, and retains enough
      snapshot history for the declared cursor TTL.
- [x] Compound asset, field, exact lookup, refresh, rate, capacity, and fair-claim indexes are used
      by reviewed PostgreSQL plans on the large fixture.
- [x] Isolated scale measurement materializes at most 51 assets or fields per interactive page,
      reports Python heap delta at or below 16 MiB and process RSS delta at or below 64 MiB between
      the small and large fixtures, and records the platform.
- [x] A 5,434-table full refresh completes in bounded pages and no more than 60 seconds on the
      recorded local environment; a source page is persisted before the next page is requested.
- [x] API, worker, and indexer pools never exceed their configured maxima under concurrency;
      acquisition timeout returns a sanitized `503`, startup fails closed, and shutdown closes all
      pool resources.
- [x] A test policy of three authenticated requests per minute returns `429` plus `Retry-After` on
      the fourth request while another principal and tenant remain unaffected.
- [x] A tenant limit of five nonterminal execution jobs admits exactly five under a two-replica
      race; excess submissions return `429`, and terminal transition releases capacity exactly once.
- [x] Fair claim prevents a tenant with one eligible job from starving behind another tenant's 100
      jobs within the documented claim bound.
- [x] Local load executes 5,000 indexed reads at concurrency 16 with zero unexpected errors,
      p95 at or below 250 ms and p99 at or below 500 ms; cold/warm context and pool wait are reported.
- [x] Inventory cardinality never changes query safety: the 5,434-table tenant can execute an
      approved one-to-three-table plan, while four tables, three joins, cross-connection plans,
      Cartesian joins, DDL/DML, unsafe fanout, excess rows, and timeout bypass remain rejected.
- [x] Schema v3→v4 upgrade and pristine migration both pass; a v3 binary's incompatibility with v4
      is documented and rollout/rollback remains coordinated rather than falsely zero-downtime.
- [x] Runtime wheel/image discover schema v4; API, execution worker, and catalog indexer start
      independently, refuse schema mismatch, and shut down gracefully.
- [x] Real PostgreSQL/DataHub acceptance refreshes the exact local catalog, serves it from the
      PostgreSQL index after DataHub is stopped, and labels freshness/staleness without fallback.
- [x] Internal-browser desktop and 390x844 acceptance visibly traverses small and large tenant
      pages, stale/tampered cursor denial, rate-limit denial, refresh status, and clean protected
      data/console/overflow scans.

Final evidence is consolidated in `tasks/M25_HANDOFF.md`: `make check` passed 1,412
tests with Ruff/mypy clean; integration passed 111 tests, acceptance 19, API 18, worker 24, scale
correctness 20 plus the 8-pass PostgreSQL cut and synthetic PASS report; deterministic evaluation
completed over the exact 11-table/465-row seed; wheel smoke found migrations 1–4; release audit
passed 555 candidate files/23 direct licenses with only `release_tree_dirty`; and desktop/mobile
internal-browser traversal, genuine cursor expiry, rate denial, short-description field search,
DataHub-offline staleness/failure, console, overflow, and protected-data scans passed. The clean
coverage run passed all 1,538 collected tests at 81.98% against the 80% threshold, including 4/4
socket acceptance and 4/4 M24 lifecycle tests.

## Required automated checks

```bash
pytest tests/unit/test_catalog_inventory.py \
  tests/unit/test_catalog_cursor.py \
  tests/unit/test_catalog_refresh.py \
  tests/unit/test_capacity_limits.py \
  tests/unit/test_control_plane_pool.py \
  tests/unit/test_http_catalog_api.py
make control-plane-reset
make control-plane-migrate
make control-plane-check
make test-scale-correctness
make benchmark-scale
make test-api-integration
make test-worker-integration
make test-integration
make test-acceptance
make evaluate
make check
make coverage
make runtime-wheel-smoke
python scripts/release_audit.py
git diff --check
```

## Manual internal-browser test

1. Start source/DataHub, migrate the control plane to v4, and start API plus catalog indexer as
   separate processes.
2. Authenticate as the small tenant and traverse its 10-table connection/asset/field pages.
3. Authenticate as the large tenant and traverse first, middle, and final pages of the 5,434-table
   inventory; verify stable counts, generation, freshness, and no full list download.
4. Reuse a cursor under another tenant, change its filter, tamper with it, and expire it; verify the
   same safe unavailable boundary.
5. Request a refresh, stop the indexer after a committed page, verify the old generation remains
   visible, restart, and observe exact resumed completion.
6. Stop DataHub and verify the completed PostgreSQL inventory remains readable but clearly stale;
   a new refresh must fail safely without recorded fallback.
7. Exercise the configured per-principal rate limit and tenant job quota.
8. Inspect browser/API text, control tables, and logs for credentials, claims, DSNs, SQL,
   parameters, prompts, source values, rows, or OpenAI material.
9. Repeat at 390x844 and verify no warning/error console entries or horizontal overflow.

## Explicit non-goals

- Semantic equivalence from names/descriptions, LLM matching, natural-language multi-domain
  selection, or dynamic Query Studio controls; M27 owns them.
- Join-drift detection, reapproval, and blast radius; M26 owns them.
- Querying arbitrary catalog assets before approved mappings/contracts exist.
- Cross-connection joins, multiple SQL dialects, connector routing, federated execution, or query
  cost estimation; M28 owns them.
- Production autoscaling, Prometheus/SIEM dashboards, HA/failover, remote secret manager operation,
  lockfile/SBOM/provenance, or production SLO claims; M29–M31 own them.

## Handoff requirements

Update `tasks/PROJECT_STATE.md`, `tasks/CURRENT_TASK.md`, `tasks/DECISION_LOG.md`, the M25 ADR,
architecture/security/query/test/deployment/runbook/UI docs, `.env.example`, service/deployment
examples, scale reports, and the exact browser-acceptance record. Return
`tasks/HANDOFF_TEMPLATE.md` with migration/checksum, six-role matrix, cursor and refresh contracts,
capacity/pool settings, index plans, 10/5,434-table measurements, latency/heap conditions, DataHub
scroll evidence, commands, warnings, omitted drills, limitations, and one proposed commit message.
