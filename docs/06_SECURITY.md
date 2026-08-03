# Security and safety model

## Threats in scope

- prompt injection attempting to bypass query policy;
- LLM hallucination of assets, columns, joins, or transformations;
- SQL injection through filter values or identifiers;
- statement smuggling and multiple statements;
- DDL/DML or utility commands disguised in a CTE;
- Cartesian joins and unbounded scans;
- fanout-induced metric corruption;
- identifier precision loss;
- accidental source writes;
- DataHub mutations without approval;
- secrets or proprietary data in repository/logs;
- denial of service through expensive queries.
- actor spoofing, horizontal workflow access, and confused-deputy publication;
- stale or malformed OIDC sessions and untrusted provider groups.
- partial, corrupt, path-escaped, out-of-scope, or stale semantic-registry state;
- name/definition similarity being mistaken for approved semantic equivalence.
- concurrent or stale registry activation, rollback-history rewriting, and projection split-brain;
- control-plane migration drift, privilege escalation, and web-process DDL;
- control-audit deletion/reordering, outbox loss, and replay under different content;
- unsafe legacy-state adoption, cross-workspace ownership inference, and identity-rotation
  collisions/cycles;
- backup/manifest tampering, secret leakage through process arguments, and restore into the active,
  source, or non-empty database.
- forged bearer signatures, JWT algorithm confusion, attacker-selected key URLs, stale/oversized
  JWKS material, replayed idempotency identities, and protected-resource enumeration;
- duplicate delivery, expired/stolen worker leases, stale fencing, cancellation races,
  retry storms, queue poisoning, and SQL/result/credential leakage through durable job state.
- cross-tenant catalog collisions, forged/stale continuation cursors, deep-offset denial of
  service, unbounded metadata materialization, and inventory cardinality being mistaken for query
  authorization;
- partial generation exposure, repeated/stalled source pages, refresh races, stale promotion,
  quota bypass, unfair queue starvation, pool exhaustion, and leakage of credential-binding
  references or DataHub tokens through the public API.
- connector-route substitution after approval, cross-tenant source aliasing, stale or disabled
  routes, capability reuse between catalog/preflight/execution/profile, source retargeting behind
  an unchanged secret handle, unsafe local secret files, catalog generations surviving a changed
  source/type identity, unsupported-dialect execution, forged cost evidence, and planner-output
  or topology leakage.
- secret-version substitution caused by conflating a public route revision with an external
  provider version, reading a provider's latest version, or inventing version pins for historical
  routes during migration.
- advanced-intent authority escalation through forged output aliases, window frames, aggregate
  stages, CTE references, unsupported-query approximation, or a keyword/length route bypass;
- model-context overexposure through an unbounded catalog dump, and unapproved physical search
  results being mistaken for executable semantic mappings;
- copy-artifact injection, placeholder/value reordering, unsafe literalization, or leakage of
  standalone SQL and embedded values through persistence, telemetry, provider payloads, or logs.

## Defense in depth

### Source database

- dedicated `schemabridge_reader` role;
- `default_transaction_read_only=on`;
- explicit `SELECT` grants only;
- statement and lock timeouts;
- application opens read-only transactions;
- no admin credentials in runtime configuration.

### Connector secret versions

- every executable remote binding carries an explicit positive immutable provider version;
- provider versions remain private and are approval-bound through one-way route fingerprints;
- `route_revision` is never used as a provider version and the provider's latest version is never
  read;
- schema upgrades do not infer versions for legacy routes; loaders return no executable route
  until a new explicitly versioned rotation is approved;
- capability loaders return the opaque reference and exact version atomically, while repr, public
  models, logs, metrics, traces, and errors expose neither.

### Query construction

- no executable raw SQL from the user or LLM;
- identifiers selected from an allowlist;
- values bound as parameters;
- restricted typed IR;
- deterministic compiler;
- deterministic v1/v2 routing by typed representability rather than input length or keywords;
- parameterized SQL guarded before any standalone literal rendering;
- quote-aware textual placeholder indexing, typed AST literals, and a second zero-binding guard
  for copy/download output;
- unsupported operations fail closed.

### Governed semantic-registry boundary

- logical definitions, physical mappings, join contracts, approval references, and provenance load
  as one immutable snapshot rather than independently current fragments;
- the runtime binds each load to an explicit workspace, catalog scope, and registry ID;
- the manifest is limited to 128 KiB and a registry file to 2 MiB; files must be regular,
  non-symlinked, contained under the manifest directory, and free of duplicate YAML keys;
- only the exact active manifest entry is accepted, with matching registry identity, version,
  catalog scope, file SHA-256, and canonical full-snapshot fingerprint;
- the current synthetic registry fingerprint is
  `0710148874049078f751ac96f0a18131cf01daa41f27dc034ca52a8b212dd966` and its file SHA-256 is
  `4f086f8b9f0679d6405beab45d48c6601520b9b6e0c5f20d7dc0ad987485a723`;
- every active field must have an approved current mapping; every join summary/contract and exact
  key transformation must agree; one physical field cannot silently acquire two meanings;
- corrupt, unavailable, checksum-mismatched, unsupported, or differently scoped registry state
  fails closed without a legacy or live-to-recorded fallback;
- execution and retry reload and re-resolve against the current registry. Approval or fingerprint
  drift stops before compilation, preview, or rejected-source PostgreSQL I/O and requires new
  confirmation;
- rejected-source allowlists contain only exact approved registry join keys;
- registry capacity does not weaken the maximum of three physical tables and two joins per query.

The checked-in adapter remains synthetic recorded evidence. M22 also supplies complete live
DataHub read-back through the same port; M23 supplies PostgreSQL activation, migration, rollback,
and reconciliation controls. A selected managed active mode never consults the configured fixed
version or recorded adapter.

#### M22 immutable live-document controls

- one configured registry ID, version, catalog scope, and workspace determine one exact DataHub
  document URN; the target uses a 24-character SHA-256 prefix of the opaque workspace ID instead of
  interpolating the raw workspace value;
- publication changes the root, logical-context, and provenance sources to the immutable
  `datahub:` target before fingerprinting, then requires an approval bound to that fingerprint,
  target, workspace, actor/time, and all 37 semantic decision IDs;
- the writer rejects a different pre-existing immutable target, performs exact post-write
  read-back, and returns a per-target audit that the application validates before appending it to
  the common ledger;
- the reader protocol and HTTP client expose no mutation method. They read only the deterministic
  document/status aspects and exact privilege checks—never search, arbitrary URNs, or a manifest;
- credential files must be owner-only regular files under 64 KiB. Server URLs reject embedded
  credentials, query/fragment components, redirects, and non-loopback plain HTTP;
- GraphQL privilege responses are capped at 64 KiB, the document response at 3 MiB, status at
  16 KiB, the embedded registry at 2 MiB, approval at 256 KiB, and audit at 64 KiB. HTTP reads use
  a 15-second timeout and duplicate JSON keys are rejected;
- the document must be published and active, expose only the closed registry property set, carry
  the canonical fingerprint and complete ordered decision closure, and link exactly the seven
  assets implied by approved mappings;
- the reader must have no exact-target edit capability or mutation grant. The stock local DataHub
  policy currently exposes `generatePersonalAccessTokens`; this residual platform privilege is
  tolerated because it does not edit the target and SchemaBridge never invokes it. Any other
  observed platform mutation privilege fails closed;
- missing, malformed, oversized, removed, wrong-scope/version/workspace/fingerprint, bad-approval,
  bad-audit, stale-decision, wrong-asset, permission, and outage states return sanitized typed
  errors without a recorded fallback.

For the current local-demo principal, the verified live source ends in
`synthetic_enterprise-v1-w3f92b9304d7d1f8f0cbf410c` and its fingerprint is
`ef480eb7370924ff4c94131a2c6c4063d85652cc83aaec9059538cdc2cf9c1b9`. These values describe the
synthetic local document only; they are not universal production identifiers.

#### M23 durable control-plane controls

- `schemabridge_control` is a separate PostgreSQL database from every source database. Managed
  URLs require verified TLS. A configuration preflight compares normalized URL identities, then a
  read-only transaction on each endpoint verifies PostgreSQL-observed server address, server port,
  current database, and expected user; DNS/hostname aliases cannot satisfy separation;
- runtime, reconciler, and migrator credentials must use distinct database roles. `PUBLIC` is
  revoked. The runtime cannot execute DDL, the reconciler receives only bounded pointer/history,
  outbox, and audit capabilities, and only the migrator can apply the reviewed schema;
- migration files are ordered regular non-symlink UTF-8 files, bounded to 1 MiB, contiguous from
  version 1, and identified by SHA-256 over exact bytes. Migration is explicit, transactional,
  advisory-lock serialized, and rejects checksum drift, future/partial history, incompatible
  pre-existing schema, and concurrent ownership. Web/UI startup only calls the read-only exact
  version check;
- activation approval binds workspace/catalog/registry scope, expected generation and active
  fingerprint, exact strict immutable version/URN/fingerprint/decisions, actor, time, action,
  closed confirmation, and deterministic proposal/approval IDs. Compare-and-swap makes one
  concurrent proposal the winner; a stale proposal performs zero pointer, audit, outbox, or
  DataHub mutation;
- managed operator commands resolve the pseudonymous actor and closed roles from
  `SCHEMABRIDGE_CONTROL_OPERATOR_ACTOR_ID`/`SCHEMABRIDGE_CONTROL_OPERATOR_ROLES` and reject
  `--actor`. Local mode derives its principal from trusted local configuration; an optional argv
  actor can only assert exact equality. Publisher-only actions require `publisher` or
  `platform_admin`;
- pointer, immutable transition, pending outbox, and audit event commit in one transaction.
  Audit events form a per-workspace HMAC-SHA-256 chain that covers operation, transition, prior
  hash, payload fingerprint, key version, and time. A per-workspace advisory lock serializes chain
  append; database immutability triggers prevent update/delete of history and ledgers;
- the HMAC signing key and identity-migration key are independently generated, at least 32 UTF-8
  bytes with adequate diversity, not placeholders, and distinct from each other and from the OIDC
  pseudonymization key. Keys are secret-manager inputs; only inert key-version labels persist;
- DataHub projection is non-authoritative. Reconciliation is read-only until an exact
  fingerprint/timestamp/actor confirmation is supplied. An unchanged report with no blocking
  finding may project missing/behind state or close a pending already-in-sync delivery; a
  superseded outbox is marked superseded without a projection write. Ahead, conflict, corruption,
  and audit gaps are never overwritten. Exact post-write read-back is mandatory. DataHub's
  omission of an empty `relatedAssets` property is normalized only to the required empty set;
  explicit null/non-list values, duplicates, unhashable entries, excessive entries, and any
  non-empty set remain invalid;
- managed workflow/access/publication/request/review state is workspace-scoped in PostgreSQL.
  New durable workflow rows retain preview count/fingerprint/summary but no result rows.
  Streamlit may retain rows only in a transient envelope bound to the exact actor, workspace,
  workflow, revision, registry fingerprint, activation generation, active-pointer fingerprint,
  and recomputed preview fingerprint. Any mismatch or tampering purges the envelope instead of
  merging it with durable state.
  Optimistic-concurrency and append-only decision/publication semantics remain enforced. The
  reconciler may update only the closed projection-delivery columns, and legacy import item rows
  are immutable after insert;
- legacy SQLite import opens only an offline, exact known schema; active journals,
  unknown/partial/modified schemas, oversized/corrupt payloads, changed source fingerprints, and
  stale approvals fail closed. Orphan, ambiguous, identity-mismatched, fake, and invalid resources
  are quarantined rather than assigned to an inferred owner;
- identity rotation derives old and new opaque IDs from the same verified OIDC identity under
  explicit key/policy versions. It preserves historical actors and rejects cross-workspace
  bindings, owner collisions, lineage cycles, incomplete new-owner coverage, stale policy, or
  mismatched approval. The CLI accepts only a regular non-symlink owner-only signed evidence file
  of at most 2 MiB, valid for at most 15 minutes, with unique JSON keys, exact HMAC key version,
  payload fingerprint, and no raw claim fields. Inspection returns bounded metadata, never the
  opaque derivations. First initialization, rotation approval reservation, and completion are
  separate exact timestamped operations within the evidence window. Post-rotation workflow access
  follows only verified same-lineage workspace/actor aliases, requires the active identity to be
  initialized, caps resolution, and rejects an unknown, ambiguous, or multiply matching historical
  resource without rewriting it;
- stale recipes are never edited or fingerprint-copied. Migration requires a newly completed
  current-registry workflow, independent SQL guarding and bounded read-only execution, then
  publication as a new approved version;
- backup exports one repeatable-read snapshot and writes an owner-only custom-format archive plus
  bounded HMAC-signed manifest. The manifest binds database fingerprint, exact migration
  version/checksum, archive size/hash, state digest, table counts, key version, and timestamp;
- restore has no target-DSN command-line option. Its dedicated target credential comes only from
  `SCHEMABRIDGE_CONTROL_RESTORE_DATABASE_URL`; the target must be distinct and empty. Signature,
  permissions, archive hash, exact migration history, complete state digest/table counts, every
  workspace audit chain, pointer/history/outbox, and quarantine counts are verified before
  cutover.

The current backup baseline produces local owner-only artifacts intended for encrypted platform
storage. Remote retention, key escrow, SIEM export, automated disaster-recovery schedules, and
multi-region failover are later operational work, not M23 claims.

The 2026-07-23 local operated acceptance verified the version-1 schema checksum
`65d9447619165a92e9b8d8c6541a76569564931b6dc9c7ccbd73deb0f3fd09cc`, distinct
runtime/reconciler/migrator roles, PostgreSQL-authoritative activation and rollback through
generation 3/version 6, explicit projection repair to `delivered`, and a distinct fresh-target
restore with matching state SHA-256
`22b8fdc5568b81e2bdd8626b3315e525c16651f19d3966c538b87cc7948ec2e5`.
Legacy import accepted exactly two resources, quarantined four, and stripped three preview rows.
Identity v1→v2 rotation was replay-safe without historical rewrites, and live recipe
migration created DataHub version 44 from version 43 without exposing SQL or preview rows. These
facts close the local M23 security drill; they do not provide remote retention, SIEM, HA,
multi-region failover, or production traffic evidence. The drill required no OpenAI request; no
API key was read, printed, logged, or persisted.

#### M24 authenticated API and durable-worker controls

- The API verifies the JWT signature before mapping claims. Managed configuration fixes the
  asymmetric allowlist to `RS256` or `ES256`, rejects `alg=none`, symmetric confusion, missing or
  excessive `kid`, `jku`/`x5u`/`crit`, wrong issuer/audience/`azp`, malformed claims, and every
  invalid tenant/group/time boundary already enforced by the OIDC mapper.
- Bearer tokens are limited to 16 KiB. JWKS responses are limited to 64 KiB and 32 public signing
  keys, reject duplicate JSON keys/private or symmetric keys and redirects, use a bounded timeout
  and current-key cache, and refresh fail closed. Staging/production require a same-origin HTTPS
  JWKS URL. Plain HTTP is available only for an explicitly configured loopback development
  provider.
- The fixed local bearer exists only in development. Configuration requires at least 32 bytes and
  eight distinct byte values. Only its SHA-256 digest is retained, and presented tokens are
  compared in constant time; neither token nor digest is returned or logged.
- The API accepts exactly one `Host`, one bearer header, one `Idempotency-Key`, identity content
  encoding, and JSON request bodies within the configured byte bound (65,536 by default).
  Request models forbid extras. CORS is absent by default; proxy headers and server/date banners
  are disabled; Uvicorn access logging is disabled; interactive documentation is forbidden in
  staging/production.
- All errors are bounded `application/problem+json` responses with stable codes and a generated
  request ID. Unknown, cross-tenant, wrong-owner, and unauthorized resources share the same safe
  unavailable boundary. Logs contain operation/outcome/error type only, never tokens, claims,
  identifiers from failed protected lookups, exception text, DSNs, SQL, or rows.
- The outer API boundary buffers the response and catches unexpected exceptions before
  Starlette/Uvicorn. It logs only request ID plus error type and returns a sanitized `500` problem
  without re-raising; real-socket regression confirms that sentinel text, traceback text, and
  `Exception in ASGI application` are absent from bodies and logs.
- Submission requires the current `workflow:execute` permission, exact owner/workspace grant,
  execution-approval checkpoint, workflow revision, plan fingerprint, and exact confirmation.
  The durable authorization expires at the earliest of token expiry, five-minute default job TTL,
  and one hour from original authentication. Authorization is checked again before source I/O.
- The raw idempotency key exists only long enough to compute its SHA-256 digest. Job identity and
  payload/request fingerprints bind workspace, workflow owner, submitter, operation, revision, and
  plan. Exact replay returns the same job; changed content under the same key is a conflict with no
  protected workflow/source I/O.
- Migration `0002_authenticated_api_jobs.sql` creates constrained job state plus append-only event
  history. Its current SHA-256 is
  `4e828aacfc9cc0db35a5b05838556e35360d7e0322c16e1ed008c94faf4f4efc`.
  Final schema v3 adds `0003_reject_expired_job_success.sql` to make authorization expiry part of
  the database success predicate; its SHA-256 is
  `fba8bf2fec35c56be95487c21452ac46a45b3598c5b56cf382414aa57c7240f0`.
  `schemabridge_api` may submit/read/cancel but cannot claim or complete. `schemabridge_worker` may
  claim/heartbeat/complete and update the exact workflow execution fields but cannot submit,
  migrate, activate/reconcile the registry, publish to DataHub, or write the source. Both roles are
  `NOSUPERUSER`, `NOCREATEDB`, `NOCREATEROLE`, and `NOINHERIT`.
- Claims use database time, `FOR UPDATE SKIP LOCKED`, a transient high-entropy lease capability
  stored only as a digest, expiry, attempt count, and monotonic fencing token. Heartbeat, success,
  failure, and cancellation acknowledgement require the current capability and fence. A stale
  worker cannot mutate a reclaimed or terminal job. A separate heartbeat supervisor renews the
  lease periodically throughout synchronous governed work, joins before the caller can transition
  the job, performs one final fenced heartbeat, and sanitizes heartbeat-thread failures.
- Authorization expiry must be enforced again by PostgreSQL on every success transition. Final
  audit found the schema-v2 predicate incomplete: an application path could complete after expiry.
  Schema-v3 migration `0003_reject_expired_job_success.sql` adds the database guard; the final
  unit, PostgreSQL, real-socket, packaging, and browser regressions pass.
- Queued cancellation becomes terminal before worker/source I/O. Leased cancellation stays
  `cancel_requested` until the current worker acknowledges it; success may win a race only when the
  read already completed. The source statement timeout remains the final bound after read I/O
  begins. The worker checks cancellation before preview, before rejection inspection, and before
  each governed source statement.
- Only registry/source unavailability and source timeout are automatically retryable. Backoff is
  deterministic, exponential, bounded, and finite. Functional, stale, policy, authorization, and
  human-retry failures stop immediately; exhaustion and ambiguous/unexpected external state are
  dead-lettered. An ambiguous `STARTED` preview/rejection trace is first recovered to explicit
  human retry and is never blindly replayed.
- Worker execution reloads the exact grant, owner-scoped workflow, and PostgreSQL-selected active
  registry, then recompiles from the typed plan and applies the independent AST guard. The job has
  no SQL field, and source safety remains one allowlisted read-only `SELECT`/`WITH ... SELECT`,
  maximum three tables, no Cartesian join, fanout control, row cap, and timeout.
- Durable success includes only workflow stage/revision, row count, preview fingerprint, exact
  rejected-row total, bounded rejection-code counts, an exact unclassified residual,
  completeness/truncation flags, and completion time. The total is capped at 2,147,483,647 and the
  persistence-only residual sentinel is never serialized by the API. Bearers, claims, raw
  idempotency keys, lease capabilities, secrets, DSNs, SQL, parameters, prompts, source values,
  and preview rows have no job/event/API representation.
- Identity rotation preserves authorization continuity without rewriting historical workflow
  state. Jobs bind the current submitter and original workflow workspace/owner scope. The API may
  resolve verified OIDC lineage; staging/production workers require `verified-oidc` and read only
  persisted opaque bindings, without OIDC metadata, claims, tokens, JWKS material, or the
  pseudonymization key. The worker validates the exact persisted workspace+submitter pair across
  the complete verified linear chain for owner and workspace-wide `platform_admin` grants before
  workflow/source I/O. API/grant creation remain active-only; arbitrary, mixed-key, cross-lineage,
  quarantined, unknown, or ambiguous pairs fail closed.
- `WorkflowAccessErrorCode.STORE_FAILURE` is an unexpected external-state ambiguity. It must become
  `dead_lettered`, not a normal `authorization_mismatch`/`failed` result that could misstate an
  unavailable authorization store as a valid denial. The corrected worker preserves that
  classification.

M24 uses no LLM path. Neither component requires or receives `OPENAI_API_KEY`; the worker's
composition disables publication and uses only deterministic typed workflow state. The later M27
slice keeps key reuse in its separately configured, tenant-approved Query Studio runtime; M24
services still receive no provider capability.

The schema-v3 control check reports current/expected 3 and pending none for all five roles with
source/control separation intact. The five-role denial matrix, source
DDL/DML/role-assumption denial, process lifecycle, real-socket synthetic-OIDC journey, and
response/log/control-state secret scans pass. Periodic heartbeat regressions fail closed. The
final local gates pass `make check` with 1,074 tests/99 deselected and strict mypy over 180 source
files, 84 integration tests, 19 acceptance tests, and 1,173 tests at 81.62% coverage. Wheel/image
packaging, the schema-v3 worker probe, and internal-browser desktop/390x844 acceptance also pass.
M24 is locally accepted; production deployment, HA, monitoring, and SLO claims remain out of
scope.

#### M25 dynamic catalog and capacity controls

- Catalog identities are tenant and connection qualified. The same `schema.table` or field path
  in two connections is never an equality proof and cannot collide in storage, cursors, or API
  authorization. Public reads derive workspace from the verified principal and scope every store
  call before returning whether a resource exists.
- Connections persist only bounded public metadata plus an opaque credential-binding reference.
  DSNs, passwords, tokens, secret paths, raw claims, source rows, and sample values are forbidden
  from connection, route, refresh, generation, asset, field, tombstone, event, API, and log
  payloads. The binding reference is indexer-only and is never a public response field.
- Registration, logical disable, and refresh request require current `platform_admin`, an exact
  operation confirmation, tenant scope, and idempotency. Disable does not delete history or modify
  a source. Readers receive only public metadata and active/explicitly retained generations.
- The API receives no DataHub token or source credential and performs no source discovery. The
  dedicated `schemabridge_catalog` process receives only its catalog control credential plus the
  mutation-free source reader. It receives no source-write, bearer/OIDC, pseudonymization, OpenAI,
  workflow-execution, publication, reconciliation, migration, backup, or restore capability.
- Refresh ownership uses database time, a transient high-entropy capability stored only as a
  digest, an expiry, and a monotonic fence. Begin, heartbeat, page persistence, failure, and
  completion require the exact workspace, refresh, capability, and fence. A stale indexer cannot
  write or promote after reclaim.
- Page changes and the next source checkpoint commit atomically. Staged rows remain invisible.
  Completion verifies source completion, base generation, exact counts, tenant asset/field quota,
  and a server-computed catalog fingerprint, creates typed append-only tombstones, and changes
  `active_generation` in the same transaction. Failure or crash leaves the previous generation
  fully visible.
- DataHub discovery uses `scrollAcrossEntities` with stable URN ordering, exact environment/profile
  filtering, bounded timeouts/response bytes, and no mutation operation. Missing progress,
  repeated page fingerprints, malformed assets, oversized responses, permission denial, and
  outage map to closed sanitized failure codes. DataHub's M25 source is full reconciliation; a
  full scan is never labeled delta.
- Interactive connection, asset, and field reads use compound keysets and fetch at most
  `page_size + 1`, with `page_size <= 50`; deep `OFFSET` is forbidden. The application never
  materializes all 10 or 5,434 assets merely to serve one page.
- Cursor material is URL-safe and bounded to 1,024 bytes. HMAC-SHA-256 authenticates format
  version, workspace digest, resource, connection/asset scope, exact generation, normalized filter
  digest, issue/expiry time, and final key. The signing key is API-only, strong, and distinct from
  bearer, pseudonymization, control-audit, and identity-migration keys. Tampered, expired,
  cross-tenant, cross-connection, cross-filter, overlong, or pruned-generation cursors return one
  safe unavailable boundary before a protected read.
- Tenant connection, asset, field, per-minute request, and nonterminal-job limits are durable
  policy rows. A platform administrator applies them through an exact-confirmation,
  expected-version operator command; only the migrator role may invoke its fixed-`search_path`
  database function, and every accepted version is appended to an immutable revision table.
  Missing policy fails closed. PostgreSQL admits rate windows across API replicas and returns
  bounded `429`/`Retry-After`; job capacity reservation/release is transactional, and fair claim
  rotates workspaces before choosing their oldest eligible job. Lowering a policy may expose
  existing over-capacity state but never silently deletes catalog or job history.
- API, execution worker, and catalog indexer each receive one bounded lifecycle-managed control
  pool with finite size, acquisition timeout, waiter bound, startup check, maximum idle/lifetime,
  and graceful close. Saturation is a sanitized `503`, not an exception/DSN leak. Pool maxima are
  per process and must be multiplied by bounded replicas when budgeting the database.
- Catalog size is not query permission. The 5,434-table tenant remains subject to one governed
  connection, at most three physical tables/two joins, approved mappings/contracts, fanout checks,
  parameterized deterministic compilation, independent AST validation, row cap, and statement
  timeout. Cross-connection, fourth-table, third-join, Cartesian, DDL/DML, and unsafe-fanout plans
  remain rejected.
- M25 invokes no LLM and its dedicated API, worker, and indexer environments must not contain
  `OPENAI_API_KEY`. Names and definitions are catalog evidence only; M27's bounded implemented
  description-matching lane treats a unique result as evidence, not standalone approval. It can
  become executable only inside a complete typed request with exact fingerprint-bound
  confirmation; ambiguity must first be resolved.
- Field search indexes names, definitions, native types, tags, and glossary terms. Searching those
  values does not change status, establish semantic equivalence, or authorize SQL.
- The Kubernetes catalog workload has its own control DSN and read-only DataHub token, pod-derived
  lease identity, schema-only readiness, process-only liveness, non-root identity, dropped
  capabilities, and read-only root filesystem. The manifest is structural reference evidence and
  has not been operated in a cluster.

The M25 controls are implemented and focused adversarial cuts passed during development. Migration
v4 is bound to SHA-256
`45c7d95e56a336f267cbe29ad390f6fd54a59d826ffd33253d290be0257060bb`.
The operated local PostgreSQL report proves bounded 10/5,434 traversal, 75 small-tenant and 41,028
large-tenant fields, at-most-51-row reads, bounded memory, one 29.685331-second full refresh, and
5,000 concurrent indexed reads with no unexpected error. Asset and 64-field keyset probes used
their expected indexes under the default PostgreSQL planner without an override. The live
11-asset/59-field DataHub generation remained readable from PostgreSQL after DataHub stopped, and
a later refresh failed closed without synthetic fallback.

M25 is accepted locally after the complete six-role output, reviewed
claim/rate/capacity/fairness index-plan inventory, secret/log/control-state scan,
package/process gate, genuine expired-cursor browser proof, rate denial, clean console, and
desktop/390x844 protected-data/overflow record passed. Exact evidence remains in
`tasks/M25_HANDOFF.md`; this local result is not a production or release GO.

#### M26 semantic-change and join-drift controls

- Observation identity is workspace/catalog/registry and connection qualified. A mapping baseline
  records one exact asset/field locator, catalog generation, mapping version/decision, and metadata
  fingerprints. Equal names, definitions, tags, terms, or physical paths in another connection
  cannot establish or replace it.
- Catalog-generation triggers fan out only to matching active registry pointers; registry
  transitions enqueue their own exact scope. Durable requests use idempotent event keys,
  database-time claims, transient capability digests, leases, monotonic fencing, bounded retry,
  supersession, and immutable terminal evidence.
- Dependency reconciliation pages both managed PostgreSQL workflows and scope-qualified DataHub
  recipe current markers at no more than 50 per page. It renews the current scan lease around every
  page. Lease loss aborts before the index watermark changes. Missing EOF, duplicate/malformed
  artifacts, source failure, or wrong scope persists incomplete coverage, which blocks approval
  and execution.
- Active query-recipe current/version URNs include a deterministic prefix of the full registry
  scope fingerprint. Publication and lookup validate the complete scope binding. Identical intent
  in another tenant cannot collide with or overwrite the marker used by this dependency index.
- Join profiles are queued before the reconciler declares evidence unavailable, so a registry with
  more joins than one retry budget can make progress across attempts. Every job carries its exact
  workspace and `connection_id`; a worker claims/reclaims only its configured pair and validates
  the same binding again before heartbeat and before opening the source. Cross-workspace jobs and
  cross-connection endpoints fail closed.
- Relationship profiling accepts only the exact approved join keys and normalization plans. The
  source role, read-only transaction, allowlist, timeout, aggregate counts, and no-raw-values
  boundary remain unchanged. Jobs, reports, logs, and API responses contain no source keys, rows,
  SQL, parameters, DSNs, credentials, prompts, claims, or OpenAI material.
- The schema-v5 gate joins the approved head to live catalog evidence, latest affected report,
  dependency completeness, and current aggregate join safety. It requires exactly one row per
  selected dependency and exactly one common connection. Affected review/blocking state rejects
  before compilation, preview, rejected-source inspection, or source I/O; an unrelated change does
  not cause a tenant-wide outage.
- Runtime and worker may read only the minimized gate projection. API may read only tenant-scoped
  sanitized report/finding/impact views. Reconciler owns scan/report/dependency/profile-job writes;
  the worker owns only fenced profile-job transitions; catalog can only cause the reviewed
  generation trigger; migrator alone owns DDL. `PUBLIC` receives no privilege.
- Baseline/revalidation/rejection remains an exact operator prepare/approve/commit flow bound to
  actor, timestamp, confirmation, report, pointer, catalog vector, dependency watermark, and head
  revision. Blocking structural or join-safety change cannot be waived against the same registry.
- M26 makes no LLM request, executes no model SQL, writes neither source databases nor DataHub, and
  gives API/UI/indexer no reconciler signing key or profile-source credential.
- Browser acceptance prepares one dedicated retained database and confines state beneath
  `.local/m26-browser-acceptance`; bearer/cursor/state files are owner-only, the API/panel receive
  minimal environments, stdout contains only a bounded safe summary, and cleanup requires the
  exact confirmation plus an exact file set before dropping only that database.

The final audit corrections keep those controls fail-closed:

- initial binding candidates are loaded through fixed-`search_path`, `SECURITY DEFINER`
  `load_semantic_initial_catalog_candidates`, not a connection-wide projection. It accepts at most
  2,000 exact six-key requests/2 MB, requires contiguous ordinals and all-null/all-present selected
  locators, uses length-prefixed asset/field locator helpers plus two exact expression indexes,
  applies raw-value rechecks outside the equality-only probes, and returns at most two witnesses
  per mapping. It revokes `PUBLIC` and grants the main lookup only to the reconciler; helper
  execution is catalog-only under migrator ownership. Helpers are immutable, strict, parallel-safe
  invoker functions with `search_path=pg_catalog`;
- exact bound evidence is loaded through one fixed-`search_path`, `SECURITY DEFINER` function that
  joins the bounded workspace/scope/connection/asset/field locator set directly to active catalog
  rows and exposes no arbitrary predicate;
- dependency reconciliation accepts at most 10,000 artifacts/100,000 edges, batches PostgreSQL
  insertion by 500, conservatively projects same-scope stale artifacts to exact current logical
  fields/contracts, and marks cross-scope or unresolvable artifacts incomplete;
- gate eligibility requires the exact current live dependency-index generation, version,
  fingerprint, transition, completeness, watermark, and report/head binding; and
- profile claim, reclaim, pre-heartbeat validation, and pre-source validation bind the configured
  workspace plus connection, so equal connection IDs in two workspaces cannot cross-claim.

Final schema-v5 checksum, six-role matrix, package, release audit, coverage, complete acceptance,
cold locator, and internal-browser protected-data gates pass. M26 is accepted locally with exact
evidence in `tasks/M26_HANDOFF.md`; the dirty working tree and later infrastructure/operations
milestones still block a global production/release GO.

#### M27 Query Studio and external-AI controls — accepted locally on synthetic data

- Governed executable search and physical discovery use different ports, result types, cursors,
  and UI lanes. A physical-only result is always `needs_mapping_review` and cannot become a
  planner input.
- Server-observed catalog counts are dynamic, but interactive authority remains bounded:
  governed pages are at most 50 rows plus a keyset continuation, shortlists at most 20 candidates,
  and model-visible closure at most three models, twelve fields, and two approved joins.
- Opaque candidate IDs and signed previews are HMAC-bound to tenant scope and current semantic
  state. Tokens expire within ten minutes and contain digests, timestamps, and a nonce rather than
  request text, definitions, candidates, raw identities, or credentials.
- Confirmation reloads registry, pointer, M26 head, catalog generations, retrieval, and proposal
  state. Editing a natural interpretation invalidates its previous signature and requires a
  deterministic server recomputation and new signature before confirmation.
- Model output has no SQL, physical-identifier, join-predicate, plan, tool, approval, execution, or
  DataHub-mutation field. It may reference only opaque IDs supplied in the bounded closure; the
  existing typed request, M26 gate, deterministic compiler, and independent AST guard remain
  downstream.
- Analytical expansion is local and performs no reservation or provider egress. It derives
  numbered slots, unique source spans, semantic owners, roles/types, and compatible operations.
  Optional interpretation returns only complete `selections[{slot_id, option_index: 1}]` plus
  closed `ambiguity_kinds`. Strict schemas reject every extra field. The server maps that option
  to the scoped opaque candidate, derives any governed filter value, and owns selection counts,
  semantic state, primary selection, ordering, limit, joins, and proposal defaults.
- Exact numbered-slot coverage, unique source grounding, strict compatible top-1 leadership,
  option membership, typed filter derivation, and deterministic reconstruction are checked after
  parsing. A tied top score is typed ambiguity before interpretation. Partial/duplicate slots,
  mixed ambiguity, an option other than `1`, changed numeric signs/decimals/dates, conflicting
  governed labels, or incompatible operations fail closed. Audit retains only the closed
  `output_limit`, `schema_validation`, `grounding`, or `semantic_contract` reason, never a
  provider payload.
- Before model egress, the application rejects email, credential/DSN, token, private-key, payment,
  secret-path, high-entropy, and control-character patterns. A block leaves guided/manual mode
  available and does not silently rewrite the business request.
- Catalog text also requires an `ApprovedPublicMetadataSurface` bound to one public-metadata
  policy, pseudonymous semantic scope, active registry revision, and exact vocabulary. The adapter
  compares all of those fingerprints with the active provider configuration before serialization;
  restricted/secret-like screening is an additional fail-closed control, not the classification
  authority. A scope, registry, provider-contract, or vocabulary change cannot reuse stale egress
  evidence.
- The selected Nano configuration fingerprint is
  `f5fb0b20d85edca1a9780d76161342760f7d25ee4dc829fb686ec737f05cb0e7`.
  It is a non-secret evidence digest and confers no authority by itself.
- `OPENAI_API_KEY` is read only from the process environment in explicitly configured live mode.
  It is not part of docs, fixtures, state, logs, browser output, or process arguments. Unit/CI and
  recorded modes remain key-free.
- The managed Responses boundary uses one pinned allowlisted snapshot, fixed regional HTTPS
  origins, TLS verification, no redirects, strict structured output, `store=false`, no tools,
  bounded output and timeout, and one application-governed transient retry. `store=false` is not
  represented as Zero Data Retention. OpenAI SDK `2.46.0` is the reviewed structured-output
  transport; prompt `m27-openai-prompts-v16`, schema `m27-query-studio-v10`, expansion contract
  `m27-expansion-contract-v10`, selection contract `m27-slot-selection-v4`, normalizer
  `m27-proposal-defaults-v3`, matcher `m27-deterministic-v9`, and orchestration policy
  `m27-local-analytical-preflight-v5` are fingerprint-bound. Startup fails closed when the
  installed SDK version differs.
- Schema v6 makes external AI a versioned tenant policy that defaults off. Its atomic
  request/token/concurrency admission, fenced reservation/settlement/expiry, and append-only
  sanitized usage audit expose no prompt, response, definitions, candidates, source values, SQL,
  parameters, credentials, or raw identity.
- Additive schema v7 rejects a successful settlement unless both observed token counts are between
  one and their reserved estimates. Failure and crash paths still charge the complete reservation.
  Migration locks and validates retained reservations/audits first and aborts on incompatible
  history rather than rewriting it; exact fenced replay and the runtime-only provider grant remain.
- Additive schema v8 preserves the deployed v7 checksum, validates deterministic audit scope/ID/
  retention derivations, refreshes lease time after lock waits, and serializes provider accounting
  as reservation → admission state → daily usage. Replaced cores are security-invoker and have no
  direct service grant; only migrator-owned, fixed-search-path wrappers are executable by runtime.
- Only the migrator policy path may inspect, prepare, and exactly apply a revision. `prepare` is
  read-only; `apply` requires the proposal fingerprint and `APPLY TENANT AI POLICY`. Runtime,
  migrator, and non-provider service grants remain distinct.
- Nano, 5.4-nano, and Luna form a cheapest-first evaluation order only. There is no runtime model
  cascade or silent fake fallback.

The signed `m27-cheapest-first-campaign-v11` selected `gpt-5-nano-2025-08-07`, the first and
cheapest evaluated snapshot, without a runtime cascade. Its complete 136-case synthetic result
passed 62 positive cases, 31 negative outcomes, 18 ambiguity trials, 15 typed core trials, and
10 adversarial cases. Qualification plus the full run used 16 provider attempts, 15,715 input
tokens, 1,204 output/reasoning tokens, 30,016 ms, and EUR 0.001394085. The immutable signed
whole-file SHA-256 is
`beef3391aef2ea2cedfdfbc6e065ce259f7143861ccc0470197c6edcb88ba30a`.

The accepted schema-v2 campaign/ledger attestation proves one unique compatible authenticated
ordinal correlation with exactly 16 settled interpretation reservations/audits, exact token
totals, and zero missing, orphan, mismatched, or open attempts. It does not claim native
content-derived case-to-request identity: the historical campaign did not retain one shared
request nonce/fingerprint. This limitation prevents an ordinal witness from being overstated as a
stronger identity proof.

Fake-mode internal-browser acceptance covers both dynamic cardinalities, the matching/typed-plan/
deterministic-SQL/AST/read-only-preview path, negative and operational states, clean console,
mobile overflow, and zero-hit protected-data leakage scans. The separately authorized live-UI
smoke was blocked by the browser host's URL policy before submission and therefore made no
provider call. External AI was immediately returned to disabled policy v86. M27 is accepted
locally for synthetic evidence only; this is not production-security approval.

#### M28 governed connector-routing and cost controls

- The M26 gate, not browser state or a model, is the routing authority. Managed planning requires
  one eligible assessment with exactly one connection. Missing, ambiguous, stale, or
  cross-connection evidence rejects before target resolution.
- `QueryPlan` remains credential-free. The target-bearing `ResolvedSemanticPlan` binds workspace,
  connection, PostgreSQL dialect, route revision/fingerprint, expected reader, source/catalog/type
  identity fingerprints, and the complete cost budget. Compiler output and guard output must carry
  the same dialect and target fingerprint.
- PostgreSQL 16 is the only executable connector/dialect. Unsupported dialects have zero compiler,
  route, cost, preview, rejection, or profile calls. Cross-connection federation remains
  forbidden.
- Route contracts, route revisions, private capability revisions, and audit records are
  append-only. The current head changes only through an expected-revision, exact-state,
  operation-confirmed compare-and-swap. Exact idempotent replay is allowed; changed payload reuse
  and concurrent head movement fail closed.
- `preflight`, `catalog`, `execution`, and `profile` are separate private capabilities. Their
  fixed-`search_path`, `SECURITY DEFINER` loaders validate the exact role and, where applicable,
  current lease owner, raw transient capability digest, fencing token, expiry, workspace,
  connection, contract version, route revision, and target fingerprint. Service roles have no
  direct private-table read.
- Public models contain no opaque binding reference. Catalog routing uses an application-port-only
  `ManagedCatalogConnectorRoute` whose binding is `repr=False`; query routes expose only an
  `OpaqueConnectorSecretRef` after the database has accepted the exact operation. Neither value
  enters API, UI, cursor, job, generation, audit, fingerprint, or log payloads.
- Managed execution/profile/catalog components reject a global source `DATABASE_URL` or global
  DataHub endpoint/token as a routing fallback. Their secret directories are capability-specific;
  merging them would invalidate least privilege.
- Local PostgreSQL secret documents are addressed by SHA-256 of the opaque reference, not by the
  raw reference. Directories must be absolute, real, owner-owned mode `0700`; files must be real,
  owner-owned mode `0600`, bounded, exact-schema JSON, and stable before/after a no-follow read.
  Traversal, symlinks, hard-to-interpret permissions/ownership changes, duplicate/unknown keys,
  unsafe URL forms, wrong dialect, and wrong reader fail with a sanitized code.
- Local DataHub catalog documents follow the same no-follow/ownership rules with their own exact
  schema and bounds. Server origin/platform are normalized and fingerprinted; the transient token
  is never fingerprinted or represented publicly. Non-loopback plain HTTP, URL credentials,
  query/fragment/path injection, wrong platform, and catalog-origin mismatch fail closed.
- The PostgreSQL source identity is observed inside each cost/preview/rejection/profile
  transaction from server IP, real port, current database, and current user. Its canonical
  fingerprint accepts a host address (including PostgreSQL's `/32` or `/128` textual form), not a
  network range, local socket, port zero, scoped address, hostname, wrong database, or wrong
  reader. A mismatch is the single sanitized stale-route boundary and occurs before business data
  or `EXPLAIN`.
- Every new catalog refresh is immutably bound to the route's source, catalog, and type-contract
  identity trio. The staging generation inherits that trio, delta requires an identically bound
  base, and the generation fingerprint includes it. Activation rechecks the still-current route
  under ordered row locks. Legacy generations with all three values `NULL` are preserved as
  history but cannot authorize managed execution.
- The type member of that trio must be the reviewed PostgreSQL native-type contract v1 with its
  canonical fingerprint. Well-formed but unknown versions or SHA values fail before route
  mutation, job creation, catalog binding/activation, or protected route loading.
- A secret-only route rotation may preserve catalog evidence only when all three semantic
  identities remain unchanged. The route revision and target fingerprint still change, so all
  earlier plans, approvals, jobs, and leases are stale. Any source/catalog/type identity change
  additionally requires a new completed catalog generation before execution.
- Execution jobs preserve the current verified submitter workspace separately from the immutable
  historical workflow/connector workspace. Visibility, idempotency, and lease checks use the
  current job scope; target foreign keys, route/generation resolution, and the private execution
  capability use the historical connector scope. Domain equality, immutable columns, triggers,
  and a two-scope loader reject cross-tenant substitution after OIDC identity rotation.
- Cost admission executes only the already guarded statement under
  `EXPLAIN (FORMAT JSON, COSTS TRUE, ANALYZE FALSE, BUFFERS FALSE, VERBOSE FALSE, SETTINGS FALSE)`.
  It applies an independent timeout, parameters, expected reader, source identity, read-only
  transaction, one-plan envelope, and rollback. A connection-local loader bounds raw response
  bytes before JSON decoding; duplicate keys/non-finite values fail, decimal tokens remain exact
  `Decimal` values, and structure/numeric precision are bounded. Query Studio never uses
  `EXPLAIN ANALYZE`.
- The public/durable assessment retains only accepted/rejected, stable reasons, finite bounded
  total cost/root rows/width/node/depth/response bytes, reader/read-only/timeout facts, and
  fingerprints. Raw plan JSON, relation/index names, SQL, parameters, source values, rows,
  endpoint, database name, and topology have no model field.
- Admission runs before the execution checkpoint and again immediately before preview after
  semantic and route reload. Any rejection or failure causes zero preview I/O. Rejected-source
  inspection repeats the semantic/target check and resolves its execution capability anew.
- Migration v9 drains non-terminal legacy execution jobs, semantic profile jobs, and catalog
  refreshes before adding target requirements; it never guesses a target for retained history.
  Terminal legacy records may remain explicitly targetless and cannot be reclaimed as new work.

The retained local two-tenant and internal-browser fixtures use synthetic databases only. Their
database/role/canary material is removed before the browser process starts; the browser receives
one owner-only sanitized aggregate state and a clean allowlisted environment. This validates the
presentation boundary but is not remote secret-manager, workload-identity, network, alerting, or
production-tenant evidence.

The final internal-browser session exposed one real availability defect in that presentation
boundary: Streamlit injected `MAPBOX_API_KEY` with an empty value, and the app treated the sensitive
variable name alone as proof that a private capability was present. The correction rejects
sensitive environment entries only when their value is non-empty, so an empty framework
placeholder carries no authority while any non-empty secret-like value still fails closed. A
regression now covers that distinction. After the correction, both viewport sessions reported
zero forbidden hits, `window.__m28_xss` undefined, zero injected script elements, no overflow, and
clean final-tab consoles.

M28 is accepted locally on synthetic security evidence, making M29 eligible but not started. It
cannot be a production-security GO. M29 still owns operated remote secret management,
rotation/audit, TLS/NetworkPolicy, observability/SIEM, dependency/SBOM/provenance and recovery
controls; M30–M31 own production evaluation, independent security verification, pilot, and GA.

### M32 copy-first natural-SQL boundary — locally accepted bounded scope

- The preparation use case has no compiler, guard, copy renderer, cost-preflight, or executor
  capability. For a confirmable request it deterministically resolves the current approved
  datasets, mappings, joins, transformations, and fanout only to bind the resolved-plan
  fingerprint plus confidence/evidence/risks into the human preview; no SQL may exist before exact
  confirmation.
- Confirmation reloads the current semantic registry, verifies the signed registry/scope/head
  bindings, and deterministically revalidates and re-resolves the already confirmed typed request.
  It does not call retrieval or either language stage again. A changed mapping, contract, closure,
  approval, or semantic head makes the preview stale and produces no SQL.
- In staging/production, preparation first requires a registry-v2 physical binding set for exactly
  one tenant connection. It submits every active mapping/join dependency to the M26 semantic-
  current gate before either target lookup or language-provider access, then rechecks the exact
  selected-plan dependencies after interpretation, at confirmation and before generation. Only an
  eligible plan can resolve its current public `GovernedExecutionTarget`; that lookup is repeated
  at each later trust boundary. The `qsp3` HMAC claims and resolved-plan fingerprint bind connection
  ID, route revision, target fingerprint and PostgreSQL type-contract fingerprint; a stale
  dependency or missing/disabled/rotated/cross-tenant target fails with no preview token or SQL.
  Development/recorded mode may remain unbound only when visibly marked non-commercial.
- M32 retrieval traverses every current approved logical model and field in scope. Lexical scoring
  uses model/field names, definitions, and governed values, with role compatibility only as a
  bonus after a lexical hit. Canonical types, roles, and value constraints are exposed and
  validated in the bounded closure, not treated as free-text tokens. Provider/planner authority is
  capped at three models, twelve fields, and two approved joins. The separate M27
  physical-discovery lane may return `needs_mapping_review`; M32 never consumes or promotes that
  result. Neither search score nor a catalog-only name becomes an identifier allowlist.
- Mention extraction returns at most twelve exact source-grounded spans. Interpretation can emit
  only a strict typed request or closed ambiguity codes over the supplied logical vocabulary. It
  has no SQL, expression, physical identifier, join predicate, CTE, tool, approval, execution, or
  DataHub-mutation field.
- Live model use must pass the existing M27 tenant opt-in, approved public-metadata surface,
  restricted-data screening, admission/reservation/settlement, and sanitized audit boundaries.
  The closure contains no source rows, raw samples, SQL, parameters, credentials, private
  connector facts, or unbounded catalog export. Live failure never falls back silently.
- Route selection is a pure representability check after typed validation. A long flat request
  cannot force v2, and a short window request cannot remain v1. Text length, requested line count,
  keywords, language, and model confidence have no route authority.
- Version-2 request and plan contracts are independent from historical version 1. Alias references
  are validated at their exact evaluation stage; boolean depth/leaves, window count/arguments,
  offsets, tile counts, moving frames, predicate types, and output order are bounded. One request
  may expose at most four derived window outputs and eight window AST nodes.
- Unsupported advanced families return a closed unsupported outcome before compilation. Cross/self
  joins, arbitrary subqueries, set operations, recursion, and gaps/islands are not approximated.
  `ROLLUP` remains rejected until explicit reviewed `GROUPING()` flags preserve the distinction
  between subtotal `NULL` and genuine governed `NULL`.
- Confirmation produces parameterized PostgreSQL first. Only that statement plus typed bindings
  could enter the existing optional execution lane, after its independent guard and separate
  execution authorization.
- Standalone rendering starts only from the accepted parameterized statement. It associates
  placeholders by quote-aware textual index, never AST traversal order; rejects count mismatch,
  NUL, non-finite numbers, unsupported value types, and remaining parameters; emits typed literal
  AST nodes; and submits the normalized PostgreSQL to the complete guard again with zero bindings.
- The deterministic compiler, first AST guard, literal renderer metadata and second AST guard all
  receive the same resolved target. A target-bound query cannot be guarded without that target and
  `artifact.target_fingerprint` must equal `target.fingerprint`; `artifact.plan_fingerprint` is the
  fingerprint of the target-bound resolved plan. The standalone text is still never an executor
  input and target binding does not grant source credentials or execution authority.
- The copy artifact cannot reach a query executor and returns `executed=false`. Its SQL and
  embedded literal values are transient protected output: they are forbidden from logs, traces,
  metrics, provider requests/responses, workflow/job state, recipe documents, audit payloads, and
  generated evaluation reports. Only bounded fingerprints and non-secret capability metadata may
  persist.
- Streamlit does not trust an artifact merely because it exists in session state. On every later
  rerun it provider-free re-executes confirmation/generation revalidation from the retained typed
  confirmed request before rendering SQL or download. Semantic or target drift, revocation,
  substitution, changed output, or an unexpected resolver failure purges the artifact and exposes
  only the sanitized closed error.
- The advanced guard is CTE-scope aware and retains exactly one read-only outer statement, no
  comments/smuggling, three physical tables/two joins, no repeated asset/Cartesian join, explicit
  projections, one literal outer limit, and exact physical/CTE output allowlists. It additionally
  rejects user-shaped/recursive/forward CTEs, hidden subqueries, set operations, unsupported
  grouping sets, `OFFSET`, `FILTER`, `QUALIFY`, named windows, forged functions/frames, and
  excessive AST/SQL/window complexity.

### Natural-language boundary

- The legacy `IntentParserPort` and M27's separate local-expansion/typed-interpretation ports return
  only validated typed schemas with extra fields forbidden; no SQL, tool-call, approval,
  physical-asset, or execution output field exists;
- M27 provider input contains only bounded normalized business text and the complete public
  governed closure of at most three models, twelve fields, and two approved joins—never
  credentials, raw samples, source rows, SQL, parameters, or a catalog export;
- the live Responses adapter sets `store=false`, supplies the minimal option-selection output
  type, registers no tools, and requires an exact `ApprovedPublicMetadataSurface`; local
  expansion and the deterministic fake require no API key;
- application validation independently checks language, vocabulary membership, current approved
  context, allowed role values, and SQL-like filter payloads before confirmation;
- instruction-like user text remains untrusted business input and produces a typed unresolved/error
  preview; it cannot modify policy or reach planning;
- explicit confirmation is fingerprint-bound. A changed interpretation, invalid output, unavailable
  alternative, or unresolved ambiguity fails before the semantic planner;
- M27 confirmation can yield only the existing `ValidatedAnalyticalRequest`; the current M26
  semantic-change gate, deterministic planner/compiler, independent AST guard, and read-only
  execution boundary remain downstream.

The M27 slice broadens retrieval, not authority: a short Spanish or English description searches
the current governed registry through a bounded shortlist, while the physical catalog remains a
separate non-executable discovery lane. Names, definitions, tags, terms, deterministic scores, and
model output are evidence, not approval. A unique field match becomes executable only inside a
complete typed request that is exactly confirmed; an ambiguous match must first be resolved.
Local synthetic acceptance does not weaken the downstream M26 gate, deterministic compiler,
independent AST validation, read-only source role, or separate execution approval.

### SQL guard

Required rejection cases:

```text
INSERT / UPDATE / DELETE / MERGE
CREATE / ALTER / DROP / TRUNCATE
COPY / CALL / DO / SET / GRANT / REVOKE
multiple statements
SELECT INTO
unsafe functions or extensions if introduced
unknown schemas, tables, or columns
CROSS JOIN or join without a predicate
more than three tables
missing result limit for preview
comments or encodings used to conceal a second statement
```

The guard must parse the final compiled SQL again; trusting the compiler alone is insufficient.

### Verified M03 SQL boundary

M03 uses SQLGlot only inside the SQL adapters. The compiler constructs a typed AST, and an
independent guard reparses the rendered PostgreSQL before producing a `ValidatedQuery`. Stable
rejection codes cover parse failures, comments, multiple/non-read-only/forbidden statements,
destructive CTEs, `SELECT INTO`, recursive CTEs, wildcard projection, unknown/repeated assets,
duplicate aliases, unknown columns, unsafe functions, Cartesian or insufficient joins, fourth
tables, missing/excessive limits, and parameter-count mismatches.

Join validation is scope-aware: each explicit equality predicate must connect the newly joined
relation to a relation already available in that `SELECT`. Locking reads such as `FOR UPDATE` are
also rejected as forbidden statements.

Filter and approved mapping values use bound `%s` parameters. Identifiers can originate only from
validated domain references and are checked again against the final-SQL allowlist. The preview
adapter independently enforces a read-only transaction, transaction-local timeout, and fetched-row
cap even if another layer regresses.

### Semantic safety

- evidence and confidence do not equal approval;
- join contracts are versioned and approved;
- cardinality/fanout policy is mandatory;
- cardinality is evaluated in query traversal direction: reversing `one_to_many` yields
  `many_to_one`, while reversing an unmitigated `many_to_one` expansion fails closed;
- the query IR binds both physical keys and both transformation plans to the approved contract and
  rejects a reversed `LEFT JOIN`;
- automatic `COUNT DISTINCT` is limited to the exact approved one-side key; it is never substituted
  for `COUNT(attribute)`, and unsafe aggregates downstream of earlier fanout fail closed;
- unsafe floats are rejected rather than truncated;
- leading-zero semantics require an explicit policy;
- ambiguous requests require confirmation;
- rejected records are reported.

### DataHub mutations

- read-only by default;
- mutation capability toggled explicitly;
- runtime approval object passed to the write use case;
- MCP/client configured to prompt on writes;
- every target records family, operation, approval ID, actor, approval time, verified old/new
  fingerprint evidence, decision references, outcome, and stable failure reason where applicable;
  a null old fingerprint means “not verifiable for this target,” not “target absent”;
- the application validates every adapter result against the exact approval before persisting it;
  one approval ID cannot be reused for another family, actor, timestamp, or payload fingerprint;
- the selected publication-audit store appends one target set atomically and survives process
  restart: SQLite locally or workspace-scoped PostgreSQL in a managed profile;
- DataHub and the selected audit store do not offer a distributed transaction, so a ledger failure
  after an external mutation is surfaced for idempotent retry/reconciliation and remains a
  documented residual risk;
- immutable targets are checked before mutation and read back afterward, but DataHub has no
  compare-and-swap; the scoped publisher must remain single-writer/serialized;
- no destructive delete operation in the MVP.

Immutable version publication still crosses DataHub and the control ledger without a distributed
transaction, so its adapter keeps exact reservation, idempotent replay, and read-back rules. M23
removes that residual from *activation*: PostgreSQL commits the pointer/history/outbox/audit
atomically, and DataHub receives a separately reconciled projection. A pending projection must not
be represented as failed activation or as in-sync delivery.

### Secrets and privacy

- `.env` is ignored;
- logs redact tokens and connection passwords;
- raw samples are displayed only in bounded synthetic demo contexts;
- public examples use synthetic names and values;
- screenshots are reviewed before publication.

### Authentication, authorization, and tenant isolation

- `hosted-demo` uses one explicit pseudonymous local principal and only recorded/fake adapters;
- staging and production require Streamlit OIDC before the application composes data or publication
  adapters;
- OIDC issuer, audience, authorized party, issue/not-before/expiry times, one-hour maximum token
  lifetime, tenant, and group shape are checked on every rerun with no future/expiry leeway; the
  signed tenant value must also match the deployment's explicit tenant allowlist;
- managed startup preflights the native Streamlit auth sections, HTTPS callback/discovery transport,
  discovery/issuer origin, client/audience binding, non-placeholder independent secrets, and
  disabled token exposure before reading `st.user`;
- that preflight crosses the framework-free
  `application/ports/browser_auth.py` contract. The concrete
  `adapters/identity/streamlit_auth.py` validator is composed only by `bootstrap.py`; it imports no
  bootstrap or entrypoint, and the Streamlit entrypoint imports no concrete auth adapter. Private
  TOML validation therefore cannot create a bootstrap↔entrypoint dependency cycle or move secret
  parsing into the UI;
- the native OIDC transport authenticates the token; a separate deny-by-default application policy
  authorizes five closed roles, because OIDC alone is not authorization;
- actor/workspace IDs are deployment-scoped, versioned HMAC-SHA-256 pseudonyms; the key comes from
  the secret manager and must contain at least 32 UTF-8 bytes and eight distinct byte values, while
  email, names, tokens, and raw claims are excluded from SchemaBridge persistence and logs;
- Streamlit authentication material remains in Streamlit's browser identity cookie; the app
  revalidates ID-token times instead of treating the cookie lifetime as authorization;
- workflow access is scoped by workspace and immutable owner, with workspace-wide steward,
  publisher, auditor, and platform-admin permissions only for their explicit operations;
- draft creation and owner grant are one local SQLite transaction in demo mode or one PostgreSQL
  transaction in managed mode; legacy drafts and orphan/conflicting reservations cannot be
  adopted;
- workflow inspection/resume is inert; interrupted-state recovery is an explicit, operation-bound
  action authorized before it can persist a revision;
- unknown/no-role principals stop at a `permission_denied` UI boundary, and result
  rows/rejections are redacted for steward, publisher, and auditor views; only
  analyst/platform-admin can view/export results;
- missing and cross-scope resources return the same error and no protected workflow is loaded;
- live publication requires a different execution approver and a token issued within 15 minutes;
  publication retry rechecks freshness, permission, and approval principal;
- browser integration modes are immutable deployment settings in managed profiles;
- local development defaults to recorded execution; local live reads require an explicit
  development-only enablement flag;
- the unauthenticated legacy CLI is unavailable in staging/production;
- the local SQLite file is forced to mode `0600`.

M20 alone provides workflow control-plane isolation, not tenant-specific data-plane authorization.
M28 now adds locally accepted tenant-aware connector routing, while M20 itself still has no
per-principal rate, quota, or concurrency limit. M24/M25 add application request/job/catalog
admission and bounded pools. Heterogeneous production tenants must nevertheless remain separately
deployed or equivalently restricted until M29 operates remote secrets, workload identity,
TLS/NetworkPolicy, upstream denial-of-service protection, monitoring, and rotation/revocation.

## Security test matrix

At minimum test:

- semicolon + second statement;
- destructive CTE;
- quoted malicious identifier;
- injected filter value;
- Cartesian join;
- unknown table;
- fourth table;
- timeout path;
- database write attempt with reader role;
- unapproved DataHub mutation;
- non-integral float, NaN, Infinity, padded ID, empty string, and NULL;
- forward and reverse one-to-many overcount regressions, including unmitigated reverse expansion;
- arbitrary-attribute `COUNT`, chained fanout, forged contract predicates/transformations, and
  reversed `LEFT JOIN` regressions;
- parameterized normalized dimension grouping against PostgreSQL;
- manifest oversize, duplicate YAML key, absolute/parent-escaped path, symlink, missing active
  entry, checksum/fingerprint mismatch, catalog-scope mismatch, and unsupported format;
- exact DataHub registry document missing/removed, redirect, response/property oversize, duplicate
  JSON key, unexpected property, wrong workspace/version/fingerprint, incomplete decision closure,
  bad approval/audit, wrong related assets, target edit privilege, immutable conflict, post-write
  mismatch, and live outage with zero manifest access;
- orphan/stale/unapproved mappings, conflicting physical meanings, missing provenance decisions,
  stale join summaries, and join keys not backed by the exact approved transformation;
- a registry change or revocation after confirmation, with no compile, preview, or rejection I/O;
- a valid seven-model/five-contract registry alongside rejection of a fourth query table or third
  query join;
- homonymous support fields and a short field description must not create an approved mapping or
  forged cross-domain join;
- per-target publication audit binding, partial failure/retry, approval-ID collision, and
  fresh-process plus post-write target read-back;
- workflow replay with missing/tampered approval or embedded audit, plus a proposal that reuses an
  idempotency key for different content;
- hallucinated logical field and unsupported intent enum;
- model-output extra SQL field and SQL-like filter value;
- prompt-injection requests in Spanish and English;
- changed interpretation fingerprint and unconfirmed ambiguity;
- signed-campaign or ledger-attestation tampering, non-unique/partial ordinal binding, token/count/
  duration mismatch, incompatible policy chronology, open/orphan audit state, and attempts to
  relabel ordinal correlation as native case-to-request identity;
- wrong/missing OIDC issuer, audience, authorized party, subject, tenant, group shape, or time,
  including a validly signed tenant outside the configured allowlist;
- expired/future/over-age sessions and an unavailable authorization policy;
- actor spoof attempts, unknown roles, direct denied actions, same-owner live publication, and
  publication retry by a different principal;
- cross-owner and cross-workspace workflow access with identical safe errors and zero workflow I/O;
- production anonymous login boundary, immutable modes, logout, and absent token/PII leakage;
- weak/missing Streamlit auth secrets, a short/placeholder/low-diversity pseudonymization key,
  HTTP/mismatched metadata, malformed environment JSON, and inaccessible control-plane paths must
  produce constant fail-closed UI/CLI errors without traces.
- migration checksum drift, future/partial schema history, concurrent migrators, forbidden
  runtime/reconciler grants, and a control database that resolves to a source database;
- simultaneous activation with exactly one CAS winner, stale activation/rollback with zero
  mutation, audit-chain tampering, pending outbox persistence, and superseded projection refusal;
- every reconciliation classification, exact repair approval, DataHub post-write mismatch, and
  ahead/conflicting projection with no overwrite;
- active-pointer/version/source/scope/fingerprint mismatch with zero fixed-version or manifest
  fallback, plus M22 v1 legacy-shim activation rejection;
- offline legacy import with active journal, unknown/partial schema, changed source, orphan,
  ambiguous owner, preview-row removal, quarantine, exact replay, and stale approval;
- identity rotation collision, cycle, cross-workspace, mixed provenance, stale policy, incomplete
  binding, replay, and historical-byte preservation;
- stale recipe migration with incomplete workflow, changed pointer/intent/repository state,
  unguarded/unexecuted request, and unchanged historical recipe bytes;
- backup/manifest permission and integrity tampering, source/non-empty restore target, missing
  restore secret, state/audit mismatch, and exact fresh-target verification.
- bearer `alg=none`/algorithm confusion, forged signature, unknown `kid`, attacker key URL,
  redirect, duplicate/oversized/malformed JWKS, cache outage, wrong OIDC binding, oversized token,
  weak local secret, and local bearer outside development;
- duplicate headers, wrong host/content type/encoding, chunked body overrun, extra SQL/credential
  fields, sanitized problem responses, disabled managed docs, and cross-tenant non-disclosure;
- exact idempotent replay versus changed-payload collision, atomic job/event rollback, two-worker
  contention, expired-lease reclaim, stale fence/capability, cancellation before and after claim,
  authorization expiry, finite retry/dead-letter, terminal immutability, and restart;
- API/worker least privilege, exact schema-v3 startup, no auto-migration, source DDL/DML denial, and
  absence of tokens, claims, keys, DSNs, SQL, parameters, prompts, values, and rows in state,
  response bodies, and logs.
- identical catalog names/field paths across tenants and connections with no identity collision or
  protected cross-scope read;
- cursor tampering, unsupported version, overlength, expiry, changed filter/sort/generation,
  cross-tenant/connection/asset reuse, and pruned-generation denial before store access;
- page sizes 1, 17, and 50 over exact 10- and 5,434-table fixtures with no duplicate, omission,
  order drift, deep `OFFSET`, or full inventory materialization;
- DataHub missing/stalled/repeated cursor, malformed asset/field, excessive response, permission
  denial, outage, environment mismatch, and mutation-surface absence;
- refresh page/checkpoint atomicity, crash after every page including the terminal page,
  expired-lease reclaim, stale capability/fence, concurrent refresh conflict, base-generation
  compare-and-swap, fingerprint/count/quota mismatch, and invisible partial generations;
- immutable tombstones, disabled-connection exclusion, stale-generation exclusion, retention that
  preserves the active generation and cursor TTL, and genuine delta upsert/delete semantics;
- six-role catalog least privilege, private route denial to API/web/worker, no source-write or LLM
  capability in the indexer, exact schema-v4 startup, and zero auto-migration;
- cross-replica fixed-window rate admission, transactional five-job tenant capacity under a
  two-replica race, exactly-once terminal capacity release, workspace-rotating fair claim, and
  missing-policy fail closed;
- capacity-policy create/revision expected-version races, wrong confirmation/actor/role,
  immutable-history update/delete denial, retention below cursor lifetime, and newly lowered
  over-capacity state without deletion;
- per-process pool maximum/waiter/acquisition timeout/startup/shutdown behavior under concurrency,
  including sanitized saturation without credential or topology leakage;
- bounded field search proving that tags and glossary terms are indexed while never creating an
  approved mapping, join, or executable identifier;
- unchanged three-table/two-join compiler and guard rejection over the 5,434-table inventory,
  including cross-connection, fourth-table, third-join, Cartesian, DDL/DML, fanout, result-limit,
  and timeout bypass attempts.
- canonical target fingerprints changing for workspace, connection, dialect, route revision,
  reader, source identity, catalog identity, type contract, and every budget field;
- route create/rotate/disable exact confirmation, stale compare-and-swap, idempotent replay,
  changed-payload collision, append-only history, and no private binding in public models/repr/
  errors/state;
- source-identity canonical equivalence for bare IP and host CIDR, plus rejection of a network CIDR,
  local socket, port zero, scoped IP, wrong database, wrong reader, and post-connect retargeting
  before `EXPLAIN` or business-data reads;
- private directory/file ownership, mode, symlink, traversal, duplicate/unknown JSON key, size,
  concurrent replacement, DSN/server form, reader/dialect/platform, and missing-secret failures
  with no binding/path/credential disclosure;
- six-role denial of private tables/cross-capability loaders and exact positive lease/fence/expiry
  route access for preflight, catalog, execution, and profile;
- pristine schema v9 plus v8→v9 upgrade, immutable 0001–0008 checksums, drained non-terminal legacy
  jobs/refreshes, and retained targetless terminal history;
- new catalog generation identity propagation/fingerprint, mismatched delta base rejection,
  route/activation serialization, legacy-null generation denial to executable readers, identity
  rotation requiring refresh, and secret-only rotation preserving evidence but invalidating the
  old target;
- explicit PostgreSQL compiler/guard target+dialect equality and zero calls for unsupported
  dialects;
- `ANALYZE FALSE` cost SQL, read-only/rollback/timeout/reader/source identity, exact parameters,
  single bounded plan, response/node/depth and finite/non-negative parsing, every budget rejection,
  second preflight, and zero preview on failure;
- two tenants reusing the same connection/schema/table/field/request labels but producing distinct
  database roles/results with zero cross-scope reads;
- five governed query cases with exact results/rejections under two accepted cost preflights;
- retained 10/75 and 5,434/41,028 catalog traversal with bounded route lookup, pagination, memory,
  and unchanged one-connection/three-table/two-join query limits; and
- internal-browser accepted/blocked route-cost scenarios, desktop/390x844 layout, literal hostile
  metadata, clean console, no overflow, and zero secret, DSN, endpoint, SQL parameter, raw plan,
  source identity, database/topology, or source-value disclosure.
- M32 long-simple-v1 and short-advanced-v2 route cases proving that representability, not
  length/keywords, selects the contract;
- complete governed-registry retrieval with a maximum 3-model/12-field/2-join approved closure,
  plus ambiguity, staleness, and over-limit M32 failures before SQL; and a separate M27 regression
  proving that physical-only discovery stays `needs_mapping_review` and never enters M32;
- exact mention-span grounding, duplicate/overlapping/out-of-range/over-twelve mention rejection,
  vocabulary escape, operation/type mismatch, forged aliases/stages, and model-output SQL/physical/
  tool/approval/execution extras;
- zero compiler/guard/renderer/cost/source calls during M32 preparation, and zero executor calls
  during confirmed copy generation;
- positive typed/compiled coverage for rankings/top-N/`NTILE`, partition averages/percentages,
  duplicates/`HAVING`, running/moving windows, `LAG`/`LEAD`, delta/percent change, conditional
  metrics, and numeric buckets;
- typed no-SQL outcomes for cross/self joins, arbitrary subqueries, set operations, recursion,
  gaps/islands, and `ROLLUP` without `GROUPING()` flags;
- scope-aware CTE guard regressions for recursive/forward/extra stages, unknown CTE outputs, hidden
  subqueries/set operations, unsupported grouping/window clauses, forged frames/functions,
  excessive SQL/AST/window/predicate complexity, wrong outer limit, and remaining placeholders;
- standalone-rendering regressions for textual parameter order versus AST traversal order, quoted
  `%s`, dollar-quoted text, repeated values, dates/timestamps/time zones/decimals/booleans/`NULL`,
  non-finite numbers, NUL, unsupported types, injection strings, parameter-count mismatch, stable
  SHA-256, reparse, and zero-binding re-guard; and
- absence of copy SQL and embedded values from logs, traces, metrics, provider payloads, recipes,
  workflow/job/audit state, evaluation reports, and optional executor input.

## Incident rule

When a safety control fails, stop feature work, add a regression test first, then fix the narrowest responsible layer and document the decision.

## M29 security boundary

M29 retains every accepted query and semantic invariant and adds operated-runtime controls without
granting a new source write, SQL, DataHub mutation, or automatic semantic-approval path.

- Managed source composition permits only the HTTPS exact-version remote-secret adapter. Local
  files, global source/DataHub credentials, environment bearer tokens, redirects, unverified TLS,
  default service accounts, wrong audiences/roles, expired projected tokens, missing versions,
  oversized/malformed provider replies, and provider failures stop before source I/O.
- Remote errors, representations, logs, metrics, API/UI projections, and persisted state expose no
  token, JWT claim, binding, path, DSN, endpoint, username, password, provider response, SQL,
  parameter, result, or source value. TLS and pool failures suppress their upstream exception
  causes so credentials cannot survive in a chained traceback.
- Eight long-running runtime workloads—web, API, execution worker, publisher, catalog, profile,
  reconciler and observer—use distinct control identities. Only web/preflight, execution, catalog,
  profile and the separately scoped publisher receive the minimum capability-specific identity;
  publisher credentials never enter web/API. The observer can read only aggregate queue state
  through its security-barrier view. Scheduled backup uses a ninth dedicated
  `schemabridge_backup` identity with only the control-schema reads required by `pg_dump`; it
  cannot reuse the migrator credential. Migration v12 rejects rather than repairs the role unless
  it is exactly a login with `NOSUPERUSER`, `NOCREATEDB`, `NOCREATEROLE`, `NOREPLICATION`,
  `NOBYPASSRLS`, `NOINHERIT`, no membership that grants inherited authority or `SET ROLE`,
  `default_transaction_read_only=on`, no role-and-database-specific read-only/timeout override,
  and a positive `statement_timeout` no greater than 15 minutes.
- A real backup does not treat the DSN username as identity proof. Before exporting its snapshot
  or invoking `pg_dump`, it observes `SESSION_USER`, `CURRENT_USER`, `current_database()`, every
  role attribute, memberships, `default_transaction_read_only`, `transaction_read_only`, and the
  effective timeout inside a `REPEATABLE READ READ ONLY` transaction. Any mismatch fails closed
  with no dump artifact and no credential-bearing exception chain. The `pg_dump` child receives
  only `PATH` and the allowlisted `PG*` connection variables, never the process's OpenAI, signing,
  connector, or unrelated environment secrets.
- Managed web must explicitly disable synchronous execution and publication. Its preflight role
  cannot execute a query, and the pod receives neither a source execution credential nor DataHub
  writer material. The UI disables those actions before composing their adapters. Execution may
  cross only the authenticated API/job/worker lane once a Streamlit client is implemented;
  browser publication remains disabled. Publication may cross only the authenticated M34 API,
  durable approval queue and isolated publisher lane, and it stops at `activation_ready`; M23
  activation remains separate.
- `schemabridge-web` preflights managed configuration, projected OIDC material, workload identity,
  and exact control schema before process launch. Startup/readiness also require the fixed bounded
  loopback Streamlit health response. In staging/production, Operations has no synthetic fallback:
  without an operated source it says unavailable and cannot infer healthy status.
- The checked-in Kubernetes profile contains no Secret object, RBAC grant, token, or credential.
  It requires immutable version-named external Secret references, non-root restricted containers,
  digest-only images, TLS, namespace default-deny, exact ingress/scrape selectors, and separate
  capability egress planes. Its unresolved placeholders deliberately fail validation.
- Telemetry accepts only closed event/metric labels. Hostile input cannot become a field or label,
  and metrics endpoints are bounded, mutation-free, unauthenticated only on internal
  policy-selected ports, and perform no source I/O. Only families with a real composed producer
  are eligible for the active bundle: its dashboard uses all eight, while alerts and SLOs use
  closed producer-backed subsets. Active PromQL must match the canonical token sequence for the
  six allowlisted rules, and the three active SLO indicator contracts are closed by ID, category,
  type, fields, ordered metric tuple, and outcome vocabulary. Mutation tests prove that changed
  comparisons/functions/vector matching, appended syntax, missing or extra SLO fields, changed
  outcomes, and uncomposed metrics are rejected.
- SIEM and the remaining uncomposed backup/release/integrity/capacity signals are inactive
  contracts, not delivery or paging evidence. No OTLP exporter is composed, so no workload is
  authorized to reach an OpenTelemetry collector and TCP/4317 is deliberately absent. The
  rendered-manifest validator and a negative mutation test reject reintroducing that egress until
  exporter authentication, bounded buffering, loss accounting, and operated delivery exist.
- CI inputs are immutable and least-privileged. Vulnerability reports must be structurally complete
  and artifact-bound; an empty scanner result is not accepted as evidence. Python auditing reads
  the direct frozen runtime and build inputs without a resolver environment. The
  vulnerability-corrected sdist backend is isolated in its own hash-bound input instead of
  weakening DataHub's application dependency constraint. Runtime dependencies are built into an
  offline wheelhouse; the sdist-derived wheel has a fixed source epoch and exact local hash, and
  BuildKit mounts prevent retaining build inputs in the image. PostgreSQL backup tooling is not
  copied as untracked files from another runtime. A closed `TARGETARCH` matrix permits only
  `amd64`/Alpine `x86_64` and `arm64`/Alpine `aarch64`, with official Alpine 3.24 URLs, versions,
  and architecture-specific SHA-256 values for `postgresql16-client=16.14-r0`,
  `libpq=18.4-r0`, `lz4-libs=1.10.0-r1`, `zstd-libs=1.5.7-r2`, and
  `postgresql-common=1.3-r0`. Hash verification happens in the controlled download stage; the
  unchanged signed APKs reach the final stage through one read-only BuildKit mount and are
  installed by `apk add --no-cache --no-network`. Default Alpine signature verification remains
  mandatory, `--allow-untrusted` is forbidden, and neither APK archives nor download tooling is
  retained. The Docker context excludes private key formats and `node_modules`, while static
  policy rejects any changed or additional final-stage command. Runtime SBOM evidence accepts the
  Python base image's virtual `.python-rundeps` only through an exact platform mapping:
  `20260616.002547/noarch` on `aarch64` and `20260616.002554/noarch` on `x86_64`. Every real APK,
  package hash, graph edge, architecture, and reviewed PostgreSQL download remains exact; arbitrary
  virtual-package timestamps are rejected.
- Release authority is split across exactly seven jobs. Protected GET-only `audit` runs first with
  `contents`/`packages`/`attestations` read permissions and the environment audit token, and either
  rejects dirty external state before build or validates an exact immutable published no-op.
  Read-only `prepare` has `actions`, `contents`, and `packages` read authority for source, CI, and
  public/basic external-state inspection; protected inline-only `candidate` adds only
  `packages: write`; read-only `scan` supplies an ephemeral GHCR pull credential only to pinned
  Trivy and emits the canonical payload; protected `attest` adds
  `attestations: write`, `id-token: write`, and the package permission required for OCI
  attestation; protected `promote` adds only `packages: write`; and protected `release` alone has
  `contents: write`, with package/attestation reads. `audit`, `candidate`, `attest`, `promote`, and
  `release` enter `production-release`, so a new publication requires five sequential approvals.
- `GITHUB_TOKEN` is issued when a job starts. Security does not claim that a sentinel step can
  delay that issuance. Instead, the four mutation-capable privileged jobs (`candidate`, `attest`,
  `promote`, and `release`) are minimal, contain no checkout, dependency setup, or repository-code
  execution, and begin with an inline verifier whose first line is exactly `set -euo pipefail`.
  `set +e` and `|| true` error bypasses are forbidden. The verifier checks the actual upstream
  artifact ID/digest/hashes, source revision, peeled tag, exact remote default-branch HEAD, and
  current rules. The sentinel follows that verifier and proves environment approval before the
  first external mutation. Protected `audit` is a distinct GET-only preflight: it has no upstream
  artifact or mutation sentinel, and its exact historical no-op intentionally omits current
  HEAD/rules checks.
- Before extraction, all four privileged verifiers bind the downloaded ZIP to the artifact API's
  size and digest, then validate its exact flat allowlist/count, CRC, external attributes,
  per-entry and total size, and compression ratio. Absolute, nested, dot-segment, backslash, NUL,
  duplicate, encrypted, symlink, device, and other non-regular entries fail closed. Extraction
  occurs only into a fresh directory and every extracted path is rechecked as a regular,
  non-symlink file. Every checksum file is then bounded and parsed as an exact unique
  lowercase-SHA256/two-space/canonical-basename set before `sha256sum --check`.
- `prepare` performs only tag-ruleset metadata inspection and receives neither the protected
  environment nor its audit token. Fresh `audit` and each mutation-capable privileged boundary use
  `GET /repos/{owner}/{repo}/rulesets?includes_parents=true&targets=tag` and the exact
  `GET /repos/{owner}/{repo}/rulesets/{id}?includes_parents=true`. Exactly one active ruleset must
  target tags, include only `refs/tags/v*`, have no excludes, expose `bypass_actors: []`, and
  enforce update, deletion, and non-fast-forward protection. Because GitHub requires ruleset write
  visibility to expose bypass actors and contents write visibility to see drafts/assets,
  `SCHEMABRIDGE_RELEASE_AUDIT_TOKEN` has repository Administration write plus Contents write
  scopes. It must be an environment-only, repository-scoped fine-grained PAT whose expiry exceeds
  the maximum release window plus a recorded safety buffer, with explicit rotation/revocation.
  The workflow uses it only for explicit read-only `GET` requests and never logs it. A static
  GitHub App installation token is unsupported because per-job minting is not implemented.
- Every mutation-capable protected boundary re-reads the remote repository default branch and
  requires its HEAD to equal `SOURCE_REVISION`; ancestor status is insufficient. Fresh `audit`
  does the same, while the historical published no-op intentionally does not depend on current
  `main`. Mutation-capable boundaries also check that GitHub
  immutable Releases are enabled before any candidate, image-tag, draft, asset, or publication
  mutation.
- A new publication requires an externally enforced and recorded `main` freeze from before manual
  dispatch through the final post-publication verification. The record binds the exact source SHA,
  change-window/ticket, administrator, and independent reviewer, and every environment approval
  revalidates that it is still active. If `main` moves, the workflow fails closed; partial
  registry/attestation/Release state is an incident to preserve and inventory, not permission to
  rerun, redispatch, delete, clobber, overwrite, or rewrite `main`/the tag. The read-only
  historical published no-op is exempt from this mutation window.
- That mutation window is capped at seven calendar days from protected `audit` start through the
  successful post-publication read-back. Do not dispatch unless all five approvals can complete
  inside the cap. Expiry stops approvals and triggers the preserved-state incident path. The
  35-day artifact retention is only a 28-day investigation/recovery buffer and does not permit
  delayed publication, rerun, or redispatch.
- Release identity is stable and semantic: the annotated tag must be a non-prerelease canonical
  SemVer exactly equal to `v$project_version`; the registry candidate is
  `candidate-${{ github.sha }}`. Internal artifacts may be run-scoped, but downstream jobs use
  their declared upstream producer's outputs and public evidence contains no run ID or run
  attempt. A “re-run failed jobs” consumes the exact canonical upstream artifacts and may fill
  only missing exact state; duplicate attestation bundles are acceptable only for the same exact
  subjects, source repository, revision, and tag. A newly dispatched complete workflow requires
  clean candidate, SemVer image-tag, draft, and Release state and fails closed before rebuilding
  if any partial or conflicting state exists or a newer stable Release exists. A same-run
  `candidate` retry may adopt only the exact bounded manifest with the sealed config digest. It
  does not regenerate Trivy evidence, delete, clobber, or overwrite divergent state.
- Release builds select `ubuntu-24.04`, but that hosted-runner image and its toolchain remain
  mutable. `SOURCE_DATE_EPOCH=1730470033` normalizes timestamps only and is not evidence of a
  bit-for-bit rebuild. `DOCKER_BUILD_RECORD_UPLOAD=false` prevents the build action from uploading
  an additional unreviewed build-record artifact. Attested `release-metadata.json` records the
  temporal pip-audit service/version/source/observation and the pinned Trivy version/action plus DB
  timestamps/schema and exact metadata/database hashes; these identify the scan snapshot and are
  not a reproducibility claim.
- The public Release has the canonical body and exactly ten digest-checked assets, including
  `release-body.md`. The UI body must equal that file byte for byte, and the file's digest is bound
  by `release-assets.sha256`, `release-metadata.json`, and an attestation. Immediately after the
  draft-to-published transition, an exact-ID read-back plus the current latest-Release read require
  the exact source/tag/title/body/assets, `draft=false`, `immutable=true`, and no newer stable
  release. A later full dispatch for an exact already-published immutable tag terminates in
  GET-only `audit` after hosted file/OCI attestation and exact asset/body verification. This
  historical no-op does not require current/latest, no newer version, or current `main`, and
  performs no asset upload, edit, reconciliation, or `--latest` mutation.
- Repository/API inspection cannot prove global exclusive GHCR `PUT` or GitHub Release contents
  authority across manual users, PATs, Apps, repositories, and workflows. Production and release
  therefore remain NO-GO unless a current external administrator audit proves that writer
  boundary and a custom deployment-protection rule gates `production-release` on it. The exact tag
  ruleset, observable empty bypass set, and enabled immutable Releases are additional fail-closed
  preconditions.
- Retention verifies every signed archive/manifest pair before making any decision, requires exact
  reviewed policy/plan fingerprints and an explicit confirmation to execute, rechecks the complete
  set, rejects links/tampering/orphans, and moves pairs to recoverable quarantine. Restore cannot
  target the active control plane or a source, and verification failure cannot cut over. The
  scheduled backup pod receives only a versioned backup DSN/signing key and a PVC. Provider-side
  encryption, object lock, independent restore access, and retention remain external mandatory
  proofs.

Static manifests, local synthetic metrics, unsigned local evidence, and a local quarantine do not
satisfy the operated production boundary. Provider IAM/rotation/revocation, target-cluster
admission and network enforcement, production mTLS/SIEM/alert delivery, remote encrypted
object-lock retention, fresh-target recovery/rollback, vulnerability disposition, M30/M31, and
external security review remain mandatory production NO-GO items until independently evidenced.

## M34 publication and activation authority

- The API may reserve, inspect, authorize and cancel a publication job, but it has neither the
  DataHub writer credential nor the active-pointer update privilege. Public responses omit lease
  capabilities/digests, request fingerprints, submitter internals, DSNs and secret material.
- The publisher owns a separate no-inherit PostgreSQL login and operation-scoped DataHub writer
  secret. It has no source, connector execution, LLM, OIDC, web/API, migration, activation or
  registry-reader credential. Managed composition rejects local writer files and requires one
  exact remote secret role, opaque binding and immutable provider version.
- Registry-v2 publication and publisher-side base read-back require the observed mutating platform
  privilege set to equal exactly `{manageDocuments}` and require zero target-edit grants. The
  broader residual privileges tolerated by the historical local M22 writer are rejected for M34.
- Target versions are reserved uniquely. Claims use database time, a high-entropy capability that
  is persisted only as SHA-256, and a monotonically increasing fencing token. Expired claims are
  recovered deterministically; a stale worker cannot settle work.
- Candidate assembly happens before human authorization. The authorization is short-lived and
  binds the complete candidate, registry fingerprint, exact target and active decision set. An
  expired authorization may read an existing exact target but cannot create one.
- DataHub success is accepted only after strict read-back of the closed v2 document, observed
  approval, audit identity and the exact retained dataset URNs. Existing different content is a
  terminal conflict and is never overwritten. A transport failure after a possible write enters
  read-back recovery.
- Publication cannot mutate `registry_active_pointers`. Forward activation requires a second M23
  approval whose proposal binds the exact `activation_ready` receipt and current catalog-authority
  fingerprint. PostgreSQL repeats that validation inside the pointer CAS transaction. Rollback
  remains separate and cannot invent a fresh publication handoff.
- A dataset URN is authority only when copied from the exact retained catalog observation and
  revalidated against its current workspace, connection, generation/vector, locator, type and
  metadata fingerprints. Name similarity and reconstructed `schema.table` URNs are rejected.

These controls are local implementation evidence. They do not prove real DataHub IAM exclusivity,
secret-manager policy, target-cluster NetworkPolicy enforcement, external audit retention or an
operated incident/recovery path; those remain production NO-GO gates.

## M35 registry-change authority

- Join discovery and model-remediation profiling use only closed, aggregate read-only requests.
  Requested-job binding and its audit witness must exist before claim; a heartbeat rechecks exact
  catalog/base/route authority before each protected source-I/O boundary.
- A join name, score or apparent uniqueness never approves a contract. Self joins,
  cross-connection joins, many-to-many cardinality and one-to-many fanout without the explicit
  distinct-left-entity mitigation fail closed.
- A model replacement is bound to one immutable M33 source proposal, exact active v2 base,
  dependency watermark and, for remediation, the complete current blocking M26 report/impact set.
  Every incident join is preserved exactly, freshly approved, or explicitly removed.
- Phase B has an additional outer steward decision and a separate fresh publisher preparation.
  One M33 source can authorize only one outer draft; rejection or staleness requires a new source
  proposal instead of rebinding prior evidence.
- The generic publication INSERT trigger accepts only the three closed proposal kinds and verifies
  the exact source row, proposal payload, target scope/version and ready draft. The historical M34
  transition guard remains unchanged for update/delete state transitions.
- `replace_model_v1` must pass an exact PostgreSQL witness over source identity, model/fields,
  mappings, physical bindings, approvals and incident-join effects before M23 can observe an
  activation-ready handoff. Publication still cannot activate or write a source database.

M35 closes this bounded local PostgreSQL lifecycle only. Other dialects, real customer scale,
external IAM/secret/SIEM/recovery operation, independent penetration testing, M30 quality and M31
pilot evidence remain mandatory commercial/production gates.

## M30 campaign-manifest trust boundary

- Phase 0 repository models never accept an external gate as passed. Phase 1a uses separate types
  and authenticates only frozen campaign inputs; it cannot adjudicate a control receipt or release.
- Manifest and attestation bundle are bounded regular non-symlink files outside the candidate
  checkout. Canonical JSON rejects duplicate/extra/deep/oversized input; verification consumes
  owner-only private snapshots and all external bytes are reread afterward to detect replacement.
- Byte authentication requires exact GitHub repository, tag ref, signer workflow/source digests,
  GitHub OIDC issuer, hosted runner, SLSA predicate and at least one cryptographically verified
  log/TSA timestamp. Operational trust additionally requires separate evidence that the tag/ref is
  protected. The byte-pinned write-scoped job declares the no-secret
  `m30-manifest-attestation` environment, but the repository cannot prove its reviewers,
  self-review/bypass policy or exclusive authority. Those protections **must** be configured,
  independently inspected and retained as external evidence before dispatch; declaring the
  environment in YAML is not protection evidence. The verifier also pins the platform-specific
  SHA-256 of the official GitHub CLI 2.96.0 executable and ignores caller `PATH`; package-manager
  rebuilds and unreviewed platforms fail closed. The CLI exposes no local key, custom issuer,
  unattested, `status=passed`, skip or report-only override. Detached-bundle use avoids the
  attestation API lookup but is not an air-gapped claim because `gh` may bootstrap its trusted root.
- The manual workflow may receive only public opaque identifiers, versions and digests. Raw
  corpus cases, hidden answer key, prompts, SQL, rows, credentials and protected topology never
  enter workflow inputs, logs or the public manifest.
- A successfully authenticated manifest reports `campaign_executable=false` and
  `release_decision=no_go`; all 24 material controls remain missing until control-specific external
  receipts are authenticated and deterministically adjudicated in a later reviewed phase.
- Phase 1b freezes a canonical external control policy, the exact 24-node prerequisite DAG, five
  authorization stages, evidence subjects, producer workflows and five owner roles. Validation
  binds that policy to the authenticated manifest but explicitly reports
  `external_policy_trust_authenticated=false`, `receipt_authentication_enabled=false`, zero passed
  controls and no capabilities.
- The candidate cannot provision its own production trust root. Before any receipt adjudicator or
  campaign I/O is composed, an independently provisioned trust bundle must authenticate immutable
  GitHub identities/workflows and role keys with validity/revocation; a concrete verifier must
  derive criteria from canonical raw snapshots; and a durable trusted-clock/compare-and-swap
  attempt ledger must reject replay, gaps and stale predecessors. Signed booleans or caller-built
  `authenticated=true` models are not commercial evidence.
