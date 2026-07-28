# ADR 0011: Dynamic tenant catalog inventory and bounded capacity

- Status: implemented; M25 local acceptance pending final gate and browser record
- Date: 2026-07-23

## Context

SchemaBridge's governed semantic registry and query safety already support multiple domains, but
the discovery/catalog surfaces still contain deployment-sized assumptions. A real company may
have 10 tables, 5,434 tables, or more across several connections. A tuple loaded in one request,
an integer offset, or `schema.table` as a global catalog identity cannot represent that safely.
The same qualified name may legitimately exist in another tenant or connection.

Catalog size is also different from executable query width. Increasing the number of discoverable
assets must not weaken the existing semantic approvals, fanout controls, or maximum of three
physical tables and two joins in one request.

Long DataHub scans do not belong in the authenticated API. Partial discovery must not replace the
last complete inventory, and a crash must resume without downloading or cloning the full catalog
in Python. Managed deployments additionally need bounded database pools, distributed request
admission, tenant job capacity, and fair worker selection.

## Decision

1. Give catalog metadata a separate identity: workspace, connection, catalog asset, optional field
   path, and generation. Retain `PhysicalDatasetRef(schema.table)` only as the SQL-facing identity
   inside a resolved, governed single-connection plan.
2. Persist tenant capacity, public connection metadata, private opaque route bindings, refresh
   runs, generations, assets, fields, and typed tombstones in PostgreSQL control schema v4.
   Migrations 0001–0003 remain immutable.
3. Store only bounded public metadata and an opaque credential-binding reference. DSNs, tokens,
   passwords, secret paths, raw claims, source rows, and samples are forbidden from catalog state,
   API payloads, events, and logs.
4. Run discovery in a separately credentialed `schemabridge-catalog` process. The API may register
   or disable public metadata and request a refresh, but it has no DataHub/source credential. The
   indexer may resolve a private route and read metadata, but has no source-write, workflow
   execution, publication, LLM, reconciliation, migration, restore, bearer, or identity-
   pseudonymization capability.
5. Claim refreshes with database time, a transient capability stored only as a digest, expiry, and
   monotonic fence. Commit each bounded source page and its next checkpoint atomically before
   requesting another page. Expired ownership returns safely to `requested`; a higher fence
   resumes the exact checkpoint.
6. Build full refreshes in an invisible generation. Completion verifies source completion, base
   generation, tenant quota, counts, and a server-computed canonical fingerprint, writes immutable
   removals, and changes the active-generation pointer in the same transaction. A crash or failure
   leaves the prior complete generation visible. If the final page committed before a crash,
   restart completes it without another source read.
7. Support typed upsert/delete delta pages only for a source with a real change feed. Clone/apply
   delta state inside PostgreSQL. DataHub's M25 integration is a full reconciliation and must not
   be relabeled incremental.
8. Read DataHub with `scrollAcrossEntities` in stable URN order, exact environment/profile scope,
   bounded timeouts and response bytes, and no mutation surface. Missing progress, repeated pages,
   malformed metadata, excessive responses, permission denial, and outage fail with closed
   sanitized codes.
9. Serve connections, assets, and fields from PostgreSQL with compound keyset comparisons and
   `LIMIT page_size + 1`; return at most 50 items and forbid deep `OFFSET`. Never materialize a
   tenant's complete catalog to produce one interactive page.
10. Authenticate continuation cursors with an API-only HMAC-SHA-256 key. Bind version, workspace
    digest, resource, connection/asset scope, exact generation, normalized filter digest, issued/
    expiry time, and final sort key. Limit cursors to 1,024 bytes and 15 minutes. Tampering,
    overlength, expiry, scope/filter/generation drift, and pruned generations share one safe
    unavailable boundary.
11. Keep connection, asset, field, per-minute API request, and nonterminal-job limits as durable
    workspace policy. Missing policy fails closed. PostgreSQL enforces cross-replica fixed-window
    request admission, transactional job-capacity reservation/release, and workspace-rotating fair
    claims before oldest-job selection. Apply policy through an expected-version,
    exact-confirmation operator command backed by a migrator-only fixed-`search_path` function;
    append every accepted actor/value/version set to an immutable revision ledger.
12. Inject one bounded lifecycle-managed PostgreSQL control pool into each API, execution-worker,
    and catalog-indexer process. Size, waiters, acquisition/startup/close timeouts, idle/lifetime,
    and replica counts are explicit. Saturation is sanitized. Deployment capacity is the sum of
    each per-process maximum multiplied by its bounded replicas.
13. Preserve query safety independently of inventory cardinality. A 5,434-table tenant may browse
    and later retrieve candidates, but one query remains within one governed connection, three
    physical tables, two approved joins, deterministic compilation, independent AST validation,
    fanout policy, row cap, and statement timeout. Cross-connection execution remains rejected.
14. M25 makes no OpenAI request. Names, native types, definitions, and glossary terms are context,
    not proof of semantic equivalence. M27 may match a slight description against a bounded
    candidate set, but ambiguity still requires explicit confirmation and mappings/joins remain
    approval governed.
15. Include field names, definitions, native types, tags, and glossary terms in the stored
    PostgreSQL full-text document and GIN index. This is bounded retrieval infrastructure, not a
    scoring, approval, or SQL-generation decision.
16. Deploy the catalog indexer as its own bounded workload. The reference Kubernetes Deployment
    uses two replicas, pod-derived lease identity, a dedicated catalog DSN and read-only DataHub
    token, schema-only startup/readiness, process-only liveness, and a hardened non-root
    filesystem/capability profile.

## Consequences

- Catalog capacity becomes tenant data instead of a deployment constant. The reference scale proof
  uses one 10-table tenant and one 5,434-table tenant across multiple connections.
- Interactive reads remain available from the last complete PostgreSQL generation while DataHub is
  unavailable; freshness/staleness must be visible and a new refresh fails without fallback.
- Cursors are generation snapshots, so retention must keep non-active generations long enough for
  the configured cursor TTL while never deleting the active generation.
- Full refresh storage temporarily includes active and staging generations; tenant quota and
  retention policy must budget that explicitly.
- Exact schema checks make v3 binaries incompatible with schema v4. Rollout is coordinated, not
  falsely zero downtime, and rollback requires a v4-compatible release or separately verified
  restore.
- Durable policy prevents silent overload but requires an approved tenant-policy provisioning
  procedure before traffic. There is no compile-time default when policy is missing. Optimistic
  versioning rejects concurrent stale changes, while the immutable revision ledger preserves the
  accepted operator history.
- Local latency, memory, and pool thresholds are regression budgets only. Production autoscaling,
  alert thresholds, HA/failover, and SLOs remain separate decisions.

## Rejected alternatives

- Use `schema.table` as the catalog primary identity: it collides across tenants/connections and
  confuses metadata identity with SQL identity.
- Load the whole DataHub catalog into API memory: it couples request latency to source size,
  exposes the API to source credentials, and cannot prove bounded behavior.
- Use `OFFSET` pagination: deep pages become progressively expensive and are unstable under
  generation changes.
- Expose unsigned page numbers or cursors: they can be replayed across tenant, filter, connection,
  or generation boundaries.
- Upsert directly into the active inventory: a failure exposes a mixed partial generation.
- Clone a delta generation in Python: memory grows with catalog size and violates the bounded-page
  contract.
- Give DataHub credentials to the API or execution worker: it collapses least-privilege boundaries
  and lets long metadata scans interfere with authenticated request or query execution.
- Define tenant table counts as environment/code constants: it cannot represent different
  organizations or change policy without deployment.
- Increase query table limits because more inventory is available: discovery cardinality is not
  semantic approval or query safety.
- Use an LLM to infer mappings during indexing: names/descriptions are insufficient evidence, and
  M25 has no need for an OpenAI credential.

## Verification state

The implementation now exists and migration
`0004_dynamic_catalog_inventory.sql` has SHA-256
`45c7d95e56a336f267cbe29ad390f6fd54a59d826ffd33253d290be0257060bb`.
The operated PostgreSQL scale report passed locally with:

- exact traversal of 10 and 5,434 assets at page sizes 1, 17, and 50;
- 10 assets/75 fields for the small tenant and 5,434 assets/41,028 fields across two active
  large-tenant connections;
- one 64-field asset every 997 large-fixture assets, with nested paths, Unicode names,
  heterogeneous native types, definitions, null/key variation, and tags;
- no more than 51 rows read and 50 items materialized for a bounded page;
- a 110-page full refresh in 29.685331 seconds;
- 126,601 bytes of Python heap delta and 0 bytes of process RSS delta;
- 5,000 indexed reads at concurrency 16 with no unexpected errors, p95 41.762 ms and p99
  54.951 ms;
- expected asset/field keyset indexes under the default PostgreSQL planner, without an override;
  the field probe naturally selects one 64-field asset.
- a version-checked operator path with immutable policy revisions, a searchable tag/glossary field
  document, and the hardened independent Kubernetes catalog workload in the current tree.

These are local regression results, not production SLO or availability evidence. Local milestone
acceptance remains pending. `tasks/M25_HANDOFF.md` must still consolidate:

- pristine and v3→v4 paths and the final consolidated six-role matrix;
- the final rerun of page-size and scale targets against the integrated tree;
- atomic crash/resume, terminal-page restart, stale fence, concurrent refresh, full/delta,
  tombstone, quota, fingerprint, retention, and promotion evidence;
- reviewed PostgreSQL claim/rate/capacity/fairness plans beyond the already passing natural
  asset/field keyset plans;
- DataHub stable-scroll, bounded-response, malformed/stalled/outage, and stopped-source behavior;
- pool maximum/saturation/startup/close, three-per-minute rate, five-job race, and fair-claim proof;
- final package/process, full-suite, coverage, release-audit, and diff-gate output;
- unchanged three-table/two-join query rejection matrix on the large tenant;
- internal-browser small/large/DataHub traversal, genuine cursor expiry, rate denial, stale
  inventory, protected-data/console scan, and desktop/390x844 acceptance.

No remaining item is considered passed merely because the contract or test exists. M26–M31 remain
blocked until the final gate and browser placeholders in the handoff are replaced.
