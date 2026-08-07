# Architecture

## Architectural style

SchemaBridge uses ports and adapters with a functional core and imperative shell.

```text
┌──────────────────────────────────────────────────────────────────┐
│ Entrypoints                                                      │
│ CLI · Streamlit · authenticated HTTP API · worker · catalog indexer │
└─────────────────────────────┬────────────────────────────────────┘
                              │ typed commands / view models
┌─────────────────────────────▼────────────────────────────────────┐
│ Application                                                      │
│ use cases · ports · orchestration · approval workflow            │
└─────────────────────────────┬────────────────────────────────────┘
                              │ domain values and policies
┌─────────────────────────────▼────────────────────────────────────┐
│ Domain                                                           │
│ concepts · mappings · transformations · joins · requests · plans │
└──────────────────────────────────────────────────────────────────┘
               ▲                    ▲                    ▲
               │ ports              │ ports              │ ports
┌──────────────┴───────┐  ┌─────────┴──────────┐  ┌─────┴─────────┐
│ DataHub adapters     │  │ PostgreSQL / SQL   │  │ LLM / storage │
│ MCP · SDK · fakes    │  │ reader · compiler  │  │ typed parser  │
└──────────────────────┘  └────────────────────┘  └───────────────┘
```

## Dependency rule

All source dependencies point inward.

```text
entrypoints → application → domain
adapters ────────────────→ application ports / domain values
bootstrap → all concrete components
```

The domain never imports an adapter. The application never constructs a concrete adapter. The UI never contains business rules.
`src/schemabridge/bootstrap.py` is the only composition root: CLI, Streamlit, API, execution
worker, and catalog-indexer entrypoints request an already composed runtime from it and never
instantiate concrete adapters themselves.

Browser-authentication preflight follows the same dependency rule. The framework-free contract is
owned by `application/ports/browser_auth.py`; `adapters/identity/streamlit_auth.py` implements it
for Streamlit's private TOML structure. `bootstrap.py` composes that adapter into
`StreamlitRuntimeOptions`, and the Streamlit entrypoint supplies `st.secrets` only through the
composed port. The adapter imports neither bootstrap nor an entrypoint, bootstrap imports no
Streamlit entrypoint, and the entrypoint imports no concrete auth adapter. This prevents the former
bootstrap↔entrypoint cycle while keeping private auth parsing outside the UI.

## Modules

### Domain

Implemented focused submodules include:

- `fields.py`: physical field identity and profile signals.
- `concepts.py`: logical models and canonical fields.
- `transformations.py`: closed transformation algebra.
- `mappings.py`: candidate and approved column mappings.
- `joins.py`: join candidates, contracts, cardinality, fanout policy.
- `requests.py`: typed analytical request.
- `plans.py`: resolved semantic query plan plus independent contract-key, transformation,
  direction, and fanout invariants.
- `advanced_requests.py` and `advanced_plans.py`: parallel version-2 typed requests/plans for
  bounded boolean, aggregate, bucket, window, and output-stage operations; they do not mutate the
  historical version-1 schemas.
- `advanced_query_studio.py`: provider-neutral natural-language mentions, approved bounded
  semantic closure, v1/v2 route, preview, ambiguity, and confirmation contracts. The provider
  envelopes contain no SQL or physical identifiers; the later server-built user preview includes
  only resolved approved physical mappings with confidence, evidence, and risks.
- `validation.py`: validation findings and severity.
- `decisions.py`: approval and version records.
- `publication_audit.py`: immutable per-target publication facts, explicit unverifiable-old-state
  semantics, and exact approval binding.
- `identity.py`: opaque authenticated principals, closed roles and permissions, and immutable
  workflow workspace/owner grants.
- `semantic_registry.py`: one immutable logical-model, mapping, join, provenance, scope, and
  fingerprint boundary whose registry-wide capacity is separate from per-query limits.
- `registry_control.py`: authoritative active pointers, monotonic activation generations,
  approval-bound transitions, projection outbox state, reconciliation findings, and deterministic
  fingerprints.
- `control_plane_operations.py`, `legacy_import.py`, and `identity_rotation.py`: typed
  backup/restore evidence, offline legacy adoption/quarantine contracts, and immutable dual-key
  identity-rotation plans.
- `background_jobs.py`: the closed execution-job command, immutable authorization envelope,
  lifecycle, leases/fences, cancellation, retry/dead-letter classification, and minimized result
  summary.
- `catalog_inventory.py`: tenant/connection-qualified inventory identities, public connection
  metadata, capacity policies, versioned asset/field summaries, refresh generations, typed source
  changes, page keys, and cursor bindings. These identities do not replace the SQL-facing
  `PhysicalDatasetRef`.

### Application

Implemented use cases include:

- scan catalog;
- generate semantic candidates;
- review and publish a canonical model;
- discover and approve join contracts;
- resolve a guided request;
- parse a natural-language request;
- plan, compile, validate, and preview a query;
- prepare an M32 natural-SQL interpretation without compilation, then separately confirm it to
  resolve, compile, guard, render, and re-guard a standalone PostgreSQL artifact without
  execution;
- publish and retrieve reusable context;
- evaluate against ground truth.
- prepare/commit compare-and-swap registry activation and rollback;
- load the active pointer and exact immutable DataHub version;
- inspect and explicitly repair the non-authoritative DataHub projection;
- prepare/approve/apply legacy import, identity rotation, and stale-recipe migration;
- create and verify control-plane backup/restore artifacts.
- submit, inspect, and cancel one authenticated governed-preview job;
- claim and process at most one execution job while revalidating authorization, workflow,
  registry, query plan, SQL policy, and source-read controls.
- register/disable tenant catalog metadata, request and inspect refreshes, and list bounded
  connection/asset/field pages;
- claim and stream one catalog refresh page at a time, durably checkpointing before another source
  page is requested and completing an already persisted terminal page without rereading it.

Ports isolate:

- catalog reads;
- catalog writes;
- sample profiling;
- SQL execution;
- SQL parsing/guarding;
- natural-language parsing;
- source-grounded advanced mention extraction and typed advanced interpretation;
- deterministic standalone-SQL rendering from one already guarded parameterized query;
- draft/decision storage;
- clock and identifiers where needed.
- append-only publication-audit persistence.
- authentication-independent authorization and tenant-scoped workflow-access ports.
- one workspace/catalog-scoped governed semantic-registry loader.
- migrated PostgreSQL control-state, registry-version, projection, legacy-import, identity-rotation,
  and backup/restore ports.
- bearer authentication and PostgreSQL background-job storage ports. The application receives
  neither FastAPI/PyJWT values nor psycopg connections.
- separate public catalog, indexer-only route, refresh-generation, mutation-free source,
  tenant-capacity, and signed-cursor ports. Public API composition cannot resolve source
  credentials, while indexer composition cannot authorize browser requests.

### Adapters

- DataHub MCP adapter for discovery, lineage, query context, and documents.
- DataHub SDK/OpenAPI adapter for logical-model operations where MCP lacks a required primitive.
- PostgreSQL sample/preview adapter.
- SQLGlot compiler/guard adapter.
- OpenAI structured-output language adapter plus deterministic fake.
- PostgreSQL copy renderer that associates typed bindings by textual parameter index, emits no
  remaining placeholders, and cannot execute its output.
- SQLite/local YAML draft, publication-ledger, and workflow-access adapters for explicit
  local/recorded profiles; DataHub remains the approved semantic knowledge layer.
- OIDC/local identity mappers; browser claims and Streamlit stay outside domain and application
  modules.
- Manifest-backed, in-memory, and live DataHub semantic-registry adapters. The recorded adapter
  verifies a bounded manifest, exact file SHA-256, canonical registry fingerprint, scope, and path
  containment. The live reader uses a separate mutation-free HTTP client to load one exact,
  bounded immutable DataHub document; its approval-gated SDK writer is composed only for the
  explicit publication use case.
- PostgreSQL migrator and stores for authoritative pointer/history/outbox/HMAC audit, workflow
  drafts/access, publication audit, analytical requests, canonical reviews, join reviews, legacy
  import/quarantine, and identity bindings/rotation.
- A bounded DataHub active-pointer projector whose document is never used as planning authority.
- Transaction-consistent PostgreSQL backup and fresh-target restore adapters with signed manifests
  and post-restore integrity verification.
- Bounded signed-bearer adapters: OIDC JWT/JWKS verification before strict claim mapping, plus a
  constant-time fixed-token verifier available only in development.
- A PostgreSQL execution-job adapter with atomic submission/event creation, database-time
  `FOR UPDATE SKIP LOCKED` claims, monotonic fencing, heartbeats, cooperative cancellation,
  deterministic retry scheduling, exhausted-lease reaping, and append-only events.
- PostgreSQL catalog stores for tenant policies, public connection metadata, private route
  bindings, refresh runs, invisible generations, assets, fields, and tombstones; inventory reads
  use compound keyset order rather than deep offsets.
- A mutation-free DataHub GraphQL scroll source and a lazy deterministic synthetic source. Both
  return bounded typed pages; the DataHub adapter uses stable URN order, bounded responses and
  explicit cursor-progress validation.
- One bounded Psycopg control-plane pool injected per API, worker, and catalog-indexer process.

### Entrypoints

- Typer CLI for local diagnostics, evaluation, and scripted demos; it fails closed in managed
  staging/production until it has a verified principal transport.
- The managed operator subtree exposes explicit migration/check/status, immutable-version
  publication, activation/rollback, reconciliation, backup, restore, and controlled-state
  migration commands. Registry/version prepare and reconciliation inspect state that no write
  occurred; legacy inspect reserves only metadata and writes no imported target row. Mutating
  commands require the exact freshly prepared fingerprint and closed confirmation. Managed audit
  identity and roles come from trusted deployment configuration; an argv actor assertion is
  rejected.
- Streamlit for catalog overview, semantic review, relationships, query studio, validation, and
  decision history. Production authentication occurs before the UI service or external adapters
  are composed. It observes activation generation and projection status but exposes no control-plane
  mutation buttons.
- FastAPI for the three closed M24 job operations plus bounded liveness/readiness. It derives
  principal scope from a verified bearer token and has no source, LLM, registry, DataHub-writer,
  reconciler, or migrator adapter. Uvicorn access logging is disabled so request paths and headers
  do not become an accidental protected-data channel. The outer API boundary buffers the response
  and catches unexpected exceptions before Starlette/Uvicorn can emit a traceback, logs only
  request ID plus error type, and returns a bounded problem response without re-raising.
- A separate serial worker CLI that performs one durable claim at a time and shuts down
  cooperatively on `SIGINT`/`SIGTERM`. It cannot publish context and is never started inside the
  web or API process. `schemabridge-worker --probe-ready` composes a readiness-only runtime through
  `bootstrap.py`, verifies the exact control-plane schema with the worker credential, and exits
  without composing a polling worker, opening the source database, or loading DataHub. Kubernetes
  uses that command for startup/readiness and a process-only `kill -0 1` liveness probe.
- A separate serial catalog-indexer CLI with `--once`, continuous polling, schema-only readiness,
  and cooperative `SIGINT`/`SIGTERM` shutdown. It receives the catalog control role and
  mutation-free source adapter, but no API bearer, source-write, execution, publication, LLM,
  reconciliation, migration, restore, or identity-pseudonymization capability.

## Integration seams

Each external dependency gets a fake implementation before its real adapter. This permits modules to develop independently while preserving one final composition path.

```text
FakeCatalogPort  ─┐
FakeQueryExecutor ├─ application tests
FakeIntentParser ─┘

DataHubCatalogAdapter ─┐
PostgresQueryExecutor  ├─ bootstrap / integration tests
OpenAIIntentParser ────┘
```

## Data flow

```text
DataHub metadata + bounded source profile
    → candidate evidence
    → human-approved semantic artifacts
    → one atomic governed registry snapshot
    → AnalyticalRequest
    → registry-scoped semantic resolution
    → ResolvedQueryPlan
    → deterministic SQL compiler
    → SQL policy/AST validation
    → read-only preview
    → result/rejection report
    → approved context write-back
```

## State ownership

- Physical metadata source of truth: DataHub and source database.
- Draft decisions: local stores in the recorded/development profile; PostgreSQL in a managed
  profile.
- Approved semantic definitions and decision documents: DataHub.
- Active semantic planning snapshot: the explicitly selected recorded bundle in local mode, or the
  exact immutable DataHub version referenced by the authoritative PostgreSQL pointer in active
  mode. Managed active selection never consults a configured fixed version or manifest adapter.
- Managed registry authority: PostgreSQL active pointer, immutable transitions, projection outbox,
  and HMAC-chained control audit. The DataHub active-pointer document is a repairable projection.
- Managed execution-job authority: PostgreSQL schema v3. Migration `0002` introduces jobs/events
  and migration `0003` adds database-enforced authorization-expiry protection. Jobs retain only
  scoped authorization
  fingerprints, lifecycle/lease metadata, closed failure codes, and bounded result summaries.
  Raw idempotency keys, bearer tokens, claims, lease capabilities, SQL, parameters, prompts,
  source values, and preview rows are not durable job state.
- Managed catalog authority: PostgreSQL schema v4 stores tenant policy, public connection metadata,
  opaque private routes, refresh/checkpoint state, immutable generation contents, active-generation
  pointers, and append-only tombstones. DataHub remains the physical metadata source; it is not the
  interactive pagination backend. Only a completed, quota-checked, fingerprinted generation can
  replace the active pointer.
- Publication/review/request/workflow state: append-only or optimistic-concurrency PostgreSQL
  stores in managed profiles; SQLite remains an explicit local-demo implementation.
- Evaluation fixtures: versioned YAML in `demo/ground_truth`.
- UI session state: transient navigation, the current draft, and an exact-bound preview-result
  envelope. Preview rows are never authoritative or durable: the envelope is reusable only while
  actor, workspace, workflow, revision, registry fingerprint, activation generation,
  active-pointer fingerprint, and recomputed preview fingerprint all still agree. Drift or
  tampering purges it.
- UI workflow access: additive durable workspace/owner grants created atomically with the initial
  draft. OIDC claims and tokens do not enter the SchemaBridge control plane, SQLite, or application
  logs; Streamlit still maintains its browser identity cookie. Historical drafts without a
  grant are not trusted as authenticated resources.

## Failure strategy

External failures become typed application errors. The UI must distinguish:

- unavailable service;
- insufficient metadata;
- ambiguous semantic request;
- unapproved mapping or join;
- rejected SQL policy;
- source-quality rejection;
- query timeout;
- DataHub write rejection.
- unavailable, corrupt, out-of-scope, or stale semantic registry.
- unavailable/disabled catalog connection, missing tenant policy, stale or tampered cursor,
  source outage/malformed page, lost refresh lease, capacity denial, or unavailable generation.

No failure falls back to ungoverned SQL.

DataHub and PostgreSQL do not provide a distributed transaction. Registry activation therefore
commits pointer, transition, pending outbox, and audit atomically in PostgreSQL before any
projection write. A projection failure leaves the committed registry active and the outbox
pending. Reconciliation reads both systems, classifies the difference, and mutates DataHub only
after an exact approval for a safe, unchanged report. Ahead, conflicting, corrupt, superseded, and
audit-gap states are not overwritten. Immutable-version writers remain idempotent and require
exact post-write read-back.

## M20 identity flow

```text
Streamlit-managed OIDC identity cookie
    → managed auth-secret/HTTPS preflight
    → st.user claims (entrypoint only)
    → strict claim mapper
    → versioned HMAC-pseudonymous AuthenticatedPrincipal
    → deny-by-default authorization port
    → workspace-scoped access grant
    → JudgeUiService
    → typed workflow decision using principal.actor_id
```

The application checks role permission before looking up workflow access, then checks the
workspace/owner grant before composing the orchestrator. Missing, cross-tenant, and unauthorized
workflow IDs return the same safe boundary error. Result rows are removed before steward,
publisher, or auditor views are returned. Live publication additionally enforces a different
execution approver, 15-minute token freshness, and reauthorization of publication retries.
Workflow inspection is pure; interrupted-state repair is an explicit operation-bound transition.

The workspace boundary scopes workflow/control-plane state. Live catalog/query/publication adapters
still use deployment-wide credentials, so heterogeneous tenant data-plane routing is a later
architecture milestone and must not be inferred from M20 isolation.

## M21 semantic-registry flow

```text
workspace + catalog scope + configured registry ID
    → bounded manifest
    → exact active-entry selection
    → contained regular YAML file
    → SHA-256 verification
    → typed cross-artifact validation
    → canonical full-snapshot fingerprint
    → guided intent / planner / workflow / UI
```

`GovernedSemanticRegistryPort` supplies one `ScopedSemanticRegistrySnapshot`; consumers do not load
logical choices, physical mappings, and joins independently. The active synthetic bundle is
`synthetic_enterprise` version 1 in catalog scope `synthetic-demo`. It contains seven models,
31 approved mappings, and five approved contracts. Its checked-in file SHA-256 is
`4f086f8b9f0679d6405beab45d48c6601520b9b6e0c5f20d7dc0ad987485a723`, and its canonical registry
fingerprint is
`0710148874049078f751ac96f0a18131cf01daa41f27dc034ca52a8b212dd966`.

The registry may be much larger than one executable request. Planning still rejects a fourth table
or third join, and the independent SQL guard repeats the three-table limit. Rejected-source
allowlists are derived from exact approved registry join keys rather than a parallel hard-coded
list. Before execution or retry, the application reloads the current registry and resolves the
validated request again. A changed, revoked, corrupt, or differently scoped snapshot yields a typed
failure before compilation/preview I/O and requires a new confirmation.

The manifest adapter remains the explicit synthetic/offline implementation. M22 adds complete
governed DataHub read-back behind the same port without a recorded fallback. M23 adds PostgreSQL
activation, durable migrations, monotonic rollback, and projection reconciliation. M27 owns
registry-wide guided and natural-language field selection, including matching from a short
free-form description.

## M22 live DataHub registry flow

```text
recorded approved bundle + exact workspace scope
    → prepare immutable datahub: source/provenance
    → canonical live fingerprint
    → exact typed approval (37 semantic decisions)
    → deterministic workspace-hashed DataHub document URN
    → bounded SDK upsert + exact post-write read-back
    → common append-only publication ledger

configured live mode + version + read-only credential
    → exact URN HTTP read (no search, no manifest)
    → bounded document/status and privilege responses
    → typed snapshot + approval + audit + 7 related assets
    → scope/version/fingerprint/decision verification
    → GovernedSemanticRegistryPort consumers
```

For the current local-demo principal, `synthetic_enterprise` version 1 is stored at
`urn:li:document:schemabridge-semantic-registry-synthetic_enterprise-v1-w3f92b9304d7d1f8f0cbf410c`.
Its source is the same ID prefixed by `datahub:` and its canonical live fingerprint is
`ef480eb7370924ff4c94131a2c6c4063d85652cc83aaec9059538cdc2cf9c1b9`. The document reconstructs
7 models, 31 mappings, 5 contracts, the complete 37-decision closure, and links exactly 7 physical
assets.

The workspace suffix is the first 24 hexadecimal characters of SHA-256 over the opaque workspace
ID; raw tenant/workspace labels are not placed in the target. Publication verifies an immutable
target before mutation, performs post-write read-back, and validates its per-target audit before
the application appends the attempt to the selected audit store: SQLite locally or PostgreSQL in a
managed profile. The reader is a different protocol and concrete client with no mutation method,
fetches only that exact URN with bounded responses, verifies that it cannot edit the target, and
fails closed without search or a recorded fallback.

The immutable version document is not itself an activation control plane. M23 resolves that gap
with a PostgreSQL pointer and outbox; the M22 writer remains the separate approval-gated operation
that creates immutable versions. The M21 recorded query-recipe fixture is intentionally stale
under the live source/fingerprint until it is republished through the governed migration use case.
Registry-wide matching from a brief field description remains M27.

## M23 durable control-plane flow

```text
explicit migrator command
    → checksum-pinned transactional migration + advisory lock
    → exact schema check through runtime/reconciler/migrator roles

strict immutable DataHub version + current PostgreSQL pointer
    → read-only activation proposal
    → exact actor/time/fingerprint/confirmation approval
    → compare-and-swap transaction
       ├── new authoritative pointer generation
       ├── immutable transition
       ├── pending projection outbox
       └── HMAC-chained audit event
    → active loader reads pointer then exact immutable version
    → reconciler projects/read-backs DataHub under explicit repair approval
```

The web process receives only the runtime control credential. It checks migration version and
uses read-only server-observed source/control identities to prove separation before loading
workspace-scoped managed state, but cannot run migrations, restore a database, or repair the
DataHub projection. The reconciler can inspect pointer/history/outbox, update only the outbox's
delivery columns, and append its audit event. The migrator owns DDL and backup access. Restore uses
a separate secret target DSN and refuses the active control database, every source database, and
any non-empty target.

The initial migration creates all managed stores in `schemabridge_control`, revokes `PUBLIC`, and
grants the three roles only their required statements. Migration history, transitions, control
audit, grants, decisions, and publication ledgers are protected by append-only/immutable database
triggers; legacy import item rows are immutable as well. Startup requires the exact current schema;
it never upgrades it.

An M22 v1 legacy-shim document may still be read as historical evidence, but it is not eligible for
first managed activation. Rollback selects a previously active strict version and writes a higher
generation. Active loading verifies pointer generation, registry version/fingerprint/URN, scope,
source, approval closure, and the derived active-pointer fingerprint before returning the existing
`GovernedSemanticRegistryPort` value.

Legacy import, identity rotation, and stale-recipe migration follow the same prepare/approve/apply
shape in application code. Their PostgreSQL adapters preserve quarantine and opaque binding
history. After a completed rotation, workflow draft/access decorators resolve a current principal
to a bounded set of verified same-lineage workspace/actor coordinates and load or update the one
matching historical resource in its original scope. They never rewrite grants, drafts, decisions,
or publication history; unknown, uninitialized, ambiguous, or multiply matching identity fails
closed. Legacy import exposes metadata-only inspect plus exact approved atomic apply; recipe
migration exposes read-only prepare plus exact approved publication. Identity control verifies a
transient owner-only signed evidence envelope, initializes the first opaque lineage explicitly,
then separates read-only rotation preparation, durable approval reservation, and atomic
completion. No CLI response emits the envelope's derivations.

The Streamlit view model carries activation generation, full registry fingerprint, active-pointer
fingerprint, and the closed projection state (`pending`, `delivered`, `superseded`, `blocked`, or
`unknown`). Rendering is read-only. Durable workflow state retains only row count, preview
fingerprint, and bounded summary; exact rows exist only in the validated session envelope above.

The 2026-07-23 operated M23 acceptance verified schema version 1 with checksum
`65d9447619165a92e9b8d8c6541a76569564931b6dc9c7ccbd73deb0f3fd09cc`, separate
runtime/reconciler/migrator roles, PostgreSQL-authoritative generations 1 through 3, explicit
projection reconciliation, and a generation-3 rollback to immutable registry version 6. A signed
backup restored into a distinct fresh database with identical state SHA-256
`22b8fdc5568b81e2bdd8626b3315e525c16651f19d3966c538b87cc7948ec2e5`.
The fresh internal-browser run showed generation 3/version 6 with a delivered projection,
7/31/5 registry counts, a clean console, and no horizontal overflow at 390x844. This is local
operated acceptance, not evidence of a production deployment; remote retention, automated
disaster recovery, HA, workers/API hardening, and quotas remain later milestones.

## M24 authenticated API and durable-worker flow

```text
signed bearer
    → bounded JWT/JWKS verification
    → existing strict OIDC claim mapper
    → deny-by-default workflow authorization
    → exact execution-approval checkpoint/revision/plan fingerprint
    → atomic PostgreSQL job + submitted event

separate worker
    → database-time SKIP LOCKED claim + expiring lease + monotonic fence
    → reload exact owner grant/workflow/active registry
    → check cancellation and authorization again
    → typed plan → deterministic compiler → independent AST guard
    → bounded read-only source preview
    → sanitized count/fingerprint/rejection summary
```

The public HTTP surface is deliberately narrow:

- `GET /health/live`;
- `GET /health/ready`;
- `POST /v1/workflows/{workflow_id}/execution-jobs`;
- `GET /v1/execution-jobs/{job_id}`;
- `POST /v1/execution-jobs/{job_id}/cancel`.

Submission accepts only the expected workflow revision, expected plan fingerprint, the exact
`EXECUTE GOVERNED PREVIEW` confirmation, and a bounded `Idempotency-Key`. Exact replay returns the
same job; a changed payload under the same identity conflicts. Queued cancellation is terminal,
leased cancellation is cooperative, and terminal jobs are immutable. The worker rechecks
cancellation before preview, before rejection inspection, and before each governed source
statement.

Delivery is **at least once**, not exactly once. A crashed lease can be reclaimed only after
database-time expiry with a higher fence. A completed workflow is summarized without re-execution;
an ambiguous `STARTED` source operation is recovered to an explicit human-retry workflow state and
the job is dead-lettered rather than blindly replayed. Only registry/source outage and source
timeout are automatically retried, with finite deterministic backoff. PostgreSQL source adapters
classify only SQLSTATE class `08` and the reviewed codes `40001`, `40P01`, `53300`, `55P03`,
`57P01`, `57P02`, and `57P03` as unavailable/retryable; `QueryCanceled` is the separate timeout
path, and a SQLSTATE-less `OperationalError` is the bounded connection-failure fallback.
Safety-check failures, invalid evidence, permission/schema errors, and every other PostgreSQL error
are terminal policy rejections rather than availability incidents. A dedicated heartbeat
supervisor renews the current fenced lease periodically while synchronous governed work is in
flight and performs one final heartbeat before any terminal transition. Losing the capability,
fence, or lease fails closed. Schema v3 adds the database predicate that independently rejects
success after authorization expiry.

The API, worker, runtime, reconciler, and migrator use distinct control roles. The API receives
only its control credential and bearer-verification configuration. The worker receives its
control credential, the read-only source credential, and read-only active-registry material; it
receives no OpenAI key, DataHub writer, reconciler, migrator, or restore capability. M24 makes no
LLM call and does not need `OPENAI_API_KEY`. The API records the current submitter plus immutable
historical workflow workspace/owner scope. API authorization resolves verified OIDC identity
lineage, while staging/production workers use `verified-oidc` to follow only persisted opaque
same-lineage bindings without receiving claims, tokens, JWKS material, or pseudonymization keys.
The worker binds the job's persisted submitter and validates the exact historical
workspace+submitter pair across the complete verified linear lineage before evaluating either
owner or workspace-wide `platform_admin` grants. API/grant creation remain active-identity-only.
Arbitrary, mixed-key, cross-lineage, quarantined, unknown, or ambiguous pairs fail closed before
workflow/source I/O.

Unexpected workflow-access store failure is an external-state ambiguity, not proof of an
authorization mismatch. The worker preserves `STORE_FAILURE` and dead-letters it rather than
reporting a normal failed authorization outcome.

Success keeps the exact rejected-row total while bounding materialized rejection codes. If the
sample is incomplete, the API reports a non-negative unclassified residual plus explicit
completeness/truncation flags; the internal persistence sentinel is never an HTTP value.

Migration `0002_authenticated_api_jobs.sql` introduces the control job schema and has SHA-256
`4e828aacfc9cc0db35a5b05838556e35360d7e0322c16e1ed008c94faf4f4efc`.
Final M24 schema is version 3 with
`0003_reject_expired_job_success.sql`, SHA-256
`fba8bf2fec35c56be95487c21452ac46a45b3598c5b56cf382414aa57c7240f0`.
The local migration check reports current/expected 3 and pending none for all five roles, with
source/control separation intact. Runtime packaging now includes schema v3: the wheel smoke
installs from an empty environment/working directory and the non-root UID-10001 image exposes the
same packaged migration set. The Kubernetes worker obtains a unique lease owner from its pod name,
stages the group-readable projected `reader.env` through an unprivileged UID/GID-10001 init
container into a memory-backed volume, verifies a regular non-symlink UID-10001 mode-0600 file,
and gives the main container only the staged read-only mount. The manifests remain unoperated
reference configuration, not a cluster-deployment claim.

Process lifecycle evidence now uses a real `subprocess.Popen` lease owner, kills it with
`SIGKILL` while the database retains the lease, proves a replacement is idle before expiry,
reclaims after database-time expiry with attempt `+1` and fence `+1`, rejects the stale
capability/fence, and accepts the current heartbeat. Separate real API/worker subprocesses also
prove schema-mismatch refusal, readiness, and graceful `SIGTERM`, with sanitized output. M24
is locally accepted after the exact internal-browser desktop/390x844 record and final milestone
gates passed. This remains local evidence, not a production deployment claim.

M24 changes execution delivery, not catalog sizing. M25 now makes each tenant's connection and
table inventory data-driven and paginated, with operated local 10-table and 5,434-table fixtures
and no hardcoded inventory total or full-memory page. M25 is accepted locally after its final
quality, coverage, service, package, and internal-browser gates passed. That inventory capacity
stays separate from the governed per-query maximum of three physical tables.

## M25 dynamic catalog-inventory flow

The lasting decisions are recorded in
`docs/adr/0011-dynamic-catalog-inventory.md`. M25 separates metadata ingestion from interactive
catalog reads:

```text
platform_admin + authenticated API
    → public connection metadata + opaque binding reference
    → durable refresh request

separate catalog indexer
    → database-time claim + transient capability digest + monotonic fence
    → indexer-only route lookup
    → DataHub stable-URN scroll or lazy synthetic page
    → atomic page changes + source checkpoint in an invisible generation
    → repeat only after the prior page commits
    → count/quota/fingerprint/base-generation verification
    → atomic active_generation promotion + append-only tombstones

authenticated catalog reader
    → PostgreSQL active generation
    → compound keyset query with LIMIT page_size + 1
    → HMAC-bound opaque continuation cursor
```

Inventory identity is `(workspace, connection, catalog asset)` plus field path and generation.
This permits the same `schema.table` and column names in different connections without collision.
`PhysicalDatasetRef(schema.table)` remains the SQL-facing identity inside an already governed,
single-connection query plan; an inventory item does not become executable merely because it was
indexed.

Connections, assets, and fields have no deployment-sized tuple or hardcoded tenant total. The
reference fixtures deliberately represent one tenant with 10 tables and another with 5,434 tables
distributed across connections. Source pages and interactive pages are independently bounded.
Interactive pages contain at most 50 items and the store reads at most 51 rows. Connections use
their public sort key; assets and fields bind the keyset to one exact active or retained
generation. The API cursor is URL-safe, at most 1,024 bytes, expires after 15 minutes, and
authenticates format version, workspace digest, resource scope, connection/asset scope, generation,
normalized filter digest, and final key. Invalid, expired, cross-scope, or unavailable-generation
cursors share one non-disclosing boundary.

Refresh state is closed:

```text
requested → leased → staging → completed
requested | leased | staging → failed
leased | staging → requested after safe lease expiry
```

Each page and its next source checkpoint commit together. A crash keeps the old active generation
fully readable. After lease expiry, a higher fence resumes from the durable checkpoint; if the
terminal page was already committed, completion occurs without another source read. Full refresh
builds a replacement generation. A genuine delta-capable source may emit typed upserts/deletes;
DataHub's M25 path remains an explicitly labeled full reconciliation and is never relabeled as
incremental. Delta cloning/application happens inside PostgreSQL rather than materializing the
active catalog in Python.

Tenant policy is durable control-plane data: connection, asset, field, per-minute API request, and
nonterminal-job limits can differ by workspace and change without a code release or schema
migration. The operator applies a version-checked, exactly confirmed change through a
migrator-only database capability, and PostgreSQL appends its actor and complete values to an
immutable revision ledger. A missing policy fails closed. Rate admission is per workspace,
principal digest, operation, and fixed window across API replicas. Job capacity is
reserved/released transactionally, and worker selection rotates workspaces before selecting the
oldest eligible job. These controls bound admission; they do not reduce semantic approval
requirements.

The sixth control role, `schemabridge_catalog`, can claim, heartbeat, stage, and finalize catalog
refreshes and read only the private route it needs. API can manage public connection and refresh
requests but cannot read route bindings or source metadata. Worker, web runtime, reconciler, and
migrator retain their separate contracts. API, worker, and indexer each own one bounded
lifecycle-managed PostgreSQL pool; pool limits are per process, so deployment-wide maximum
connections equal each process limit multiplied by its bounded replica count.

The reference Kubernetes manifest now includes an independent two-replica catalog-indexer
Deployment. Each pod derives a unique lease owner from its pod name, receives only its catalog
control credential and read-only DataHub token, uses schema-only startup/readiness probes and a
process-only liveness probe, and runs non-root with dropped capabilities and a read-only root
filesystem. This is an unoperated deployment reference, not cluster evidence.

Catalog cardinality never changes query cardinality. The 5,434-table tenant may search and browse
its governed inventory, but one executable request still targets one governed connection, uses at
most three physical tables and two joins, and passes the same deterministic compiler, independent
AST guard, fanout policy, row limit, and statement timeout. Cross-connection execution remains
rejected until an explicit federation contract exists.

M25 makes no OpenAI request. Names and definitions are indexed context, not semantic equivalence.
M27 may retrieve a bounded candidate set and match a short description, but ambiguity and approval
remain human-governed.

Schema-v4 storage reserves `platform_instance`, `database_name`, and `normalized_type` for later
governed connector and type-normalization contracts. M25 persists `NULL` for those columns and
does not represent or advertise them through domain or HTTP models. Field full-text search
includes the field name, definition, native type, tags, and glossary terms in a stored `tsvector`
with a GIN index. Those values are retrieval evidence for M27, never an approved equivalence.

### M25 evidence status

The architecture above is implemented. Migration v4 has SHA-256
`45c7d95e56a336f267cbe29ad390f6fd54a59d826ffd33253d290be0257060bb`.
Focused domain, refresh, PostgreSQL, API, pool, DataHub, storage-alignment, browser-panel, and
query-safety cuts passed during implementation.

The operated local PostgreSQL evidence profile passed exact 10/5,434 traversal at page sizes 1,
17, and 50. The small inventory has 10 assets/75 fields. The large active inventory has 5,434
assets and 41,028 fields across two connections; sparse-wide assets include 64 fields, nested
paths, Unicode names, heterogeneous native types, definitions, null/key variation, and tags. Its
110-page full refresh completed in 29.685331 seconds. A bounded page read at most 51 rows and
returned at most 50. Heap/RSS deltas were 126,601/0 bytes. The 5,000-read concurrency-16 run had
zero unexpected errors with p95/p99 41.762/54.951 ms. Expected asset and field keyset indexes were
observed under the default PostgreSQL planner with no planner override; the field probe naturally
selected a 64-field asset.

These are local regression facts, not production SLO evidence and not local milestone acceptance.
The final full command matrix, broader exact index-plan inventory, runtime package/process
consolidation, protected-data scan, and desktop/390x844 browser record remain explicit blockers in
`tasks/M25_HANDOFF.md`.

## M26 governed semantic-change flow

M26 keeps the M23 registry authority and M25 catalog authority separate, then records the exact
evidence relationship between them:

```text
catalog generation promotion
    → append immutable generation-change fact
    → fan out one scoped scan per matching active registry pointer

active registry transition
    → enqueue one exact workspace/catalog/registry scan

semantic reconciler
    → claim with database time, capability digest, lease, and fence
    → page all managed workflow dependencies (PostgreSQL)
    → page all scope-qualified current recipe documents (DataHub)
    → heartbeat before, between, and after dependency pages
    → inspect only explicitly bound registry fields
    → enqueue every required connection-qualified join profile
    → persist immutable report, findings, impacts, and completeness

semantic profile worker for workspace W / connection X
    → claim/reclaim only jobs whose workspace_id is W and connection_id is X
    → execute the exact approved normalization plans through the read-only source role
    → persist aggregate counts and fingerprints only
```

A catalog promotion is not tied to one configured registry. The database trigger creates one
idempotent catalog-generation request for every active pointer in the same workspace and catalog
scope; if no active pointer exists yet, the later registry transition provides the initial scoped
scan. Registry-transition requests already carry their exact scope. Supersession, retries, lease
expiry, fencing, and terminal retention are durable and do not convert at-least-once delivery into
an exactly-once claim.

Dependency traversal uses pages of at most 50. The reconciler renews its scan lease through the
supplied continuation callback around every page; losing ownership aborts before the dependency
index watermark advances. A manifest is complete only when both workflow and recipe sources reach
validated EOF without duplicate, malformed, out-of-scope, unavailable, or unresolvable artifacts.
Partial coverage is persisted as incomplete and is a blocking safety state.

The reconciler materializes one explicitly bounded snapshot: at most 10,000 managed artifacts and
100,000 mapping/join edges. Crossing either bound makes coverage incomplete. Persistence keeps the
replacement atomic but inserts edges in JSON batches of 500. A same-scope workflow or recipe from
an older registry is projected conservatively to the current exact `LogicalFieldRef` and contract
ID; a cross-scope artifact or one with no exact current correspondence makes coverage incomplete.
Legacy intent-only recipes stay outside the active managed namespace until governed republishing.

Query recipes published against an active registry carry its full semantic scope fingerprint.
Their DataHub current/version identities include the first 32 hexadecimal characters of that
fingerprint before the intent prefix. Inventory scrolls use the same scope-qualified prefix, so
two tenants with the same intent cannot share or overwrite a current marker. Active publication
and reuse reject a missing or different scope binding; legacy unscoped documents remain historical
compatibility evidence only.

The schema-v5 gate projection combines the approved head, exact current M25 evidence, the latest
report impact for each dependency, and the latest aggregate join safety fingerprint. Runtime and
worker readers must find exactly one row for every requested mapping/join and exactly one common
`connection_id`. Missing, ambiguous, changed, incomplete, or cross-connection evidence returns one
sanitized stale-context result. An unaffected plan remains eligible. The application calls this
gate before deterministic compilation and repeats it immediately before preview and rejected-row
inspection.

The live dependency state must match the head/report registry generation, version, fingerprint,
pointer transition, completeness, watermark, and index fingerprint exactly. An accepted baseline
stores the resulting head revision—not the proposal's previous expected revision—so bindings,
profiles, reports, and the gate converge on one revision after commit.

The PostgreSQL evidence reader calls the fixed-`search_path`, `SECURITY DEFINER`
`load_semantic_bound_catalog_evidence` function with a bounded locator array. The function joins
workspace, catalog scope, connection, asset ID, active generation, and field path directly against
the indexed catalog tables; generation-vector reads use a separate connection-level projection.
It does not materialize all fields in a governed connection.

Initial explicit binding review uses a separate fixed-`search_path`, `SECURITY DEFINER`
`load_semantic_initial_catalog_candidates` capability. Its JSON batch accepts at most 2,000
requests and 2 MB, requires six exact keys, contiguous unique ordinals, and an all-null or
all-present selected locator. It resolves connection → active-generation asset → exact field
through length-prefixed locator helpers, `catalog_assets_semantic_lookup_idx`, and unique
`catalog_fields_semantic_lookup_idx`. Each lateral probe is equality-only with `OFFSET 0`, and the
outer query rechecks every raw workspace/connection/generation/qualified-name/asset/field value.
It returns at most two witnesses per mapping and validates that any selected field path equals the
requested path. Only the reconciler may execute the main lookup; `PUBLIC` cannot, and helper
execution is catalog-only under migrator ownership. This keeps ambiguity visible as
`candidate_count` 0/1/2 without scanning every asset or field in a 5,434-table connection.

M26 does not route credentials dynamically. A semantic profile worker is configured for one
explicit workspace and source connection and can claim/reclaim only that exact pair's queue rows.
It validates the pair again before heartbeat and source I/O. Deploy one isolated worker
configuration per governed workspace/connection pair. M28 owns general connector resolution and
any federation contract.

### M26 evidence status

The domain, additive schema v5, scoped scan/profile processes, read-only API projections,
operator CLI, dependency-aware gate, and acceptance instrumentation are implemented in the current
working tree. Focused regression now covers exact bound locator lookup and the initial-candidate
capability's validation/six-role ACL. After exposing a statistics-dependent index choice, the final
cold run keeps autovacuum off and relation estimates unknown, then passes over 5,434 assets/5,458
fields/31 mappings with both expression indexes and the original buffer/time bounds. Other focused
coverage includes conservative stale-artifact projection/fail-closed coverage, 5,434 real
PostgreSQL dependency edges, and workspace-plus-connection profile routing. The final full
integration/acceptance, current-byte migration/six-role output, package, release audit, quality,
81.64% coverage, checksum, and diff gates pass. The browser presentation record passes at
desktop/390x844; its session preceded the locator/CLI backend fixes, whose final bytes are covered
by cold acceptance and the full automated matrix. M26 is complete and accepted locally; exact
results and all diagnostic failures/corrections are in `tasks/M26_HANDOFF.md`.

## M27 dynamic Query Studio flow — accepted locally on synthetic data

M27 treats catalog cardinality as runtime data. The Streamlit composition reads the active
connection, physical-asset, physical-field, and governed-mapping counts from injected ports; a
10-table tenant and a 5,434-table tenant use the same path. Interactive work stays bounded:
governed keyset pages return at most 50 rows plus a continuation, the executable shortlist is at
most 20 candidates, and the complete model-visible closure is at most three logical models,
twelve fields, and two approved joins. The unchanged governed request boundary still permits one
connection, at most three physical tables, and two joins.

Executable governed matching and physical discovery are separate lanes:

```text
short description or guided choice
    → bounded search probes
    → current governed keyset search
    → bounded shortlist and complete approved-join closure
    → typed proposal using opaque candidate IDs
    → signed interpretation preview
    → optional natural edit, deterministic recomputation, and re-sign
    → exact human confirmation
    → existing ValidatedAnalyticalRequest
    → M26 semantic-change gate

physical catalog search
    → bounded physical keyset page
    → needs_mapping_review only
    → no planner, compiler, source, or DataHub-write path
```

The application depends on distinct governed-search, physical-discovery/cardinality, local
description-expansion, typed-interpretation, candidate-ID, and preview-token ports. PostgreSQL and
recorded adapters implement the catalog ports; deterministic fake and optional OpenAI adapters
implement only the interpretation boundary. Analytical expansion is local and provider-free. A
changed interpretation invalidates its signature. The application reloads current registry,
pointer, evidence-head, and catalog-generation state, recomputes deterministic retrieval,
validates the edited proposal against that exact candidate set, and issues a new signed preview
without replaying model output.

The final provider contract minimizes model authority. SchemaBridge derives the complete numbered
slot set, unique source spans, semantic owners, roles, canonical types, compatible operations, and
filter evidence locally. The interpretation response is only complete
`slot_id`/`option_index=1` selections plus closed ambiguity kinds; option `1` exists only for a
strict compatible deterministic leader. The server maps it to the scoped opaque candidate and
derives filter values, joins, primary selections, ordering, and limit. A tied top score is
ambiguity before provider I/O. The provider cannot emit candidate/logical-field IDs, physical
identifiers, SQL, joins, plans, tools, approvals, or execution actions. The reviewed boundary pins
prompt `m27-openai-prompts-v16`, schema `m27-query-studio-v10`, expansion contract
`m27-expansion-contract-v10`, selection contract `m27-slot-selection-v4`, proposal normalizer
`m27-proposal-defaults-v3`, matcher `m27-deterministic-v9`, orchestration policy
`m27-local-analytical-preflight-v5`, and OpenAI SDK `2.46.0`.

Exact slot coverage, unique source grounding, semantic owner/type constraints, strict-leader
membership, and deterministic reconstruction are validated independently after parsing. Partial
or duplicate slots, mixed ambiguity, an option other than `1`, non-unique grounding, changed
sign/decimal/date meaning, conflicting governed labels, and incompatible operation/type
combinations fail closed. Durable failure evidence retains only the closed `output_limit`,
`schema_validation`, `grounding`, or `semantic_contract` category, never provider output.

Catalog metadata may reach interpretation only with an `ApprovedPublicMetadataSurface`. That
immutable evidence binds policy version and fingerprint, the pseudonymous semantic-scope
fingerprint, active registry fingerprint, and exact prompt-vocabulary fingerprint; its own
fingerprint covers that complete tuple. The OpenAI adapter compares it with the active
`ProviderConfigurationFacts` before serialization, then separately applies restricted/secret-like
metadata screening. A scope, registry, provider-contract, or vocabulary change therefore blocks
egress rather than inheriting an earlier approval.

The selected Nano configuration fingerprint is
`f5fb0b20d85edca1a9780d76161342760f7d25ee4dc829fb686ec737f05cb0e7`.
It is a non-secret evidence digest, not a tenant label, catalog value, or credential.

Migration v6 adds scope-bound governed search, separate physical discovery counts, versioned
tenant external-AI policy, and durable reservation/settlement/audit capabilities. Policy changes
use an inspect/prepare/apply operator CLI with exact proposal fingerprint and confirmation.

Additive migration v7 hardens that existing settlement boundary without rewriting evidence. It
locks and preflights retained reservation/audit history, aborts on incompatible rows, and requires
successful observed input/output counts to be positive and no greater than their exact reservation.
Non-success outcomes retain conservative reserved-token charging, and the original fenced replay
identity and least-privilege runtime grant remain unchanged.

Additive migration v8 keeps the deployed v7 bytes immutable and places reserve, settle, and expiry
behind exact-ACL wrappers. Provider transitions acquire reservation rows before admission state
and admission state before daily usage. Settlement refreshes its lease clock after the reservation
lock returns; admission invokes its timestamping core only after accounting locks are held.
Retained audit evidence is preflighted against its deterministic scope digest, audit ID, and
retention timestamp before the migration changes function boundaries.

The evaluation order is Nano-first—`gpt-5-nano-2025-08-07`, then 5.4 Nano, then Luna—and is never
a runtime cascade. Final plan `m27-cheapest-first-campaign-v11` stopped at the first complete PASS:
`gpt-5-nano-2025-08-07`. Its signed 136-case corpus result passed 62 positive cases, 31 negative
outcomes, 18 ambiguity trials, 15 typed core trials, and 10 adversarial cases. Top-1 was
`56/62`, top-3 and recall@20 were `62/62`, and MRR was `0.946237`. Qualification plus the retained
full run used 16 attempts, 15,715 input tokens, 1,204 output/reasoning tokens, 30,016 ms, and
EUR 0.001394085. The immutable signed file SHA-256 is
`beef3391aef2ea2cedfdfbc6e065ce259f7143861ccc0470197c6edcb88ba30a`.

The accepted schema-v2 campaign/ledger attestation proves one unique compatible authenticated
ordinal correlation between that signed report and exactly 16 settled interpretation
reservations/audits, with identical token totals and zero orphan, missing, mismatched, or open
attempts. This is intentionally narrower than native case-to-request identity: the historical
campaign did not persist a common content-derived request nonce/fingerprint, so the attestation
does not claim one.

The retained internal-browser fake-mode record exercises the actual final UI bytes with
10 assets/75 fields and 5,434 assets/41,028 fields/31 governed mappings. It covers short Spanish
description matching, the north-star typed proposal, deterministic SQL/AST/read-only preview,
ambiguity, no-match, stale, rate/quota/provider-down, physical `needs_mapping_review`, desktop and
390x844 layout, a clean fresh console, no horizontal overflow, and protected-data scans. The
separately authorized live-UI smoke was blocked by the browser host's URL policy before submission,
so it made no provider call and is not a live-browser result. External AI was returned immediately
to disabled policy v86.

M27 and M28 are accepted locally within their synthetic scopes. The final M28 internal-browser
record passed all nine closed scenarios at both 1280x720 and 390x844, with state fingerprint
`2b854c0596ada31ab5a80f5e6d25c23282f0096ac90a712e953476dff899be33`. The final integration
selection passed 164 tests with one known skip in 662.06 seconds, and acceptance passed 47 tests in
133.50 seconds. The final `make check` and coverage reruns after these documentation changes remain
explicitly pending and must not be inferred from earlier runs. M29 is eligible but has not started.
M29–M31, an operated production environment, production data/traffic evaluation, external
security/operations review, and a clean release candidate still block any global production or
release GO.

## M28 governed connector-routing and cost-preflight flow

M28 keeps the restricted `QueryPlan` vendor-neutral and credential-free. The exact executable
connection is added only after the M26 gate proves that every selected mapping and join belongs to
one current `CatalogConnectionId`:

```text
confirmed ValidatedAnalyticalRequest
    → exact active registry and pure semantic resolution
    → M26 dependency gate proves one current connection
    → load one current public connector target
    → bind target into ResolvedSemanticPlan and its fingerprint
    → PostgreSQL compiler emits explicit postgresql dialect + target fingerprint
    → independent SQLGlot guard reparses the same dialect and target
    → resolve private preflight capability inside the adapter
    → EXPLAIN (FORMAT JSON, ANALYZE FALSE) in a read-only transaction
    → retain only a sanitized cost assessment
    → exact human execution approval
    → reload semantic evidence, public target, private route, and cost assessment
    → bounded read-only SELECT
    → reload semantic evidence and the same target for rejected-source inspection
```

`GovernedExecutionTarget` is the complete public execution identity. It binds workspace,
connection, connector kind, SQL dialect, immutable route revision/fingerprint, approved reader,
source-identity fingerprint, catalog-identity fingerprint, type-contract fingerprint, and the
complete cost budget/fingerprint. It has no binding reference, DSN, endpoint, database name,
password, token, or secret path. Because `ResolvedSemanticPlan` serializes that value before
fingerprinting, workflow checkpoints, query fingerprints, background-job authorization, retries,
and compatibility checks cannot redirect an existing approval to a later route.

The PostgreSQL source-identity fingerprint is derived from values observed inside the same
read-only transaction: normalized server IP, real server port, current database, and current user.
The raw coordinates do not enter the public target. A hostname alias, local socket, port zero,
network CIDR rather than one host, database change, or reader change cannot satisfy the expected
identity.

M28 supports exactly the reviewed PostgreSQL native-type contract v1 and its canonical
fingerprint. A positive integer plus an arbitrary SHA is not a valid contract identity: target
construction, route prepare/apply, persistence, loaders, job admission, and catalog activation all
fail closed. The module recomputes and verifies that exact SHA once at import, then field
normalization reuses the verified immutable constant in O(1). Adding v2 requires a coordinated code
and schema migration.

### Durable routes and immutable catalog-generation identity

Additive schema v9 introduces:

- immutable public connector-contract revisions and public route revisions;
- four separate private `preflight`, `catalog`, `execution`, and `profile` binding revisions;
- one compare-and-swap route head plus append-only operation audit;
- target-bearing execution and semantic-profile jobs; and
- lease/fence/expiry-bound `SECURITY DEFINER` loaders for each private capability.

An execution job deliberately retains two workspace coordinates. `execution_jobs.workspace_id`
is the current verified submitter scope used for visibility, idempotency, lease ownership, and
job events. Its immutable `connector_workspace_id` is the historical workflow/target scope used
for the connector foreign key, route head, catalog generation, and private execution binding.
Identity rotation may change the first without redirecting the second. Domain fingerprints,
schema checks, triggers, the worker context, and the private loader require the persisted target
workspace to equal the historical workflow workspace; a cross-workspace substitution fails.

Create, rotate, and disable use the explicit `inspect → prepare → approve → apply` operator flow.
The proposal and approval are separate owner-only artifacts. `apply` rechecks the expected head,
state fingerprint, actor, operation-specific confirmation, idempotency identity, public contract,
private-binding fingerprint, and database read-back. Exact replay is inert; a changed payload
under the same idempotency identity or a concurrent head change fails closed.

Schema v9 also prevents a catalog generation from outliving the semantic identity of the route
that produced it. Each newly claimed refresh creates one immutable
`catalog_refresh_semantic_bindings` row containing the exact source, catalog, and type-contract
identity fingerprints. The staging generation inherits that complete trio; a delta base must have
the same trio. The generation fingerprint v2 covers the identity payload before streaming its
asset/field records, including populated database, platform, and normalized-type evidence.
Activation serializes the connection, route head, refresh, generation, and policy checks and
rejects a changed target.

A FULL page is one atomic store operation under the same lease. Asset rows are inserted once and
field rows are split into batches of at most 500 inside that transaction. This bounds each JSON
parameter without widening the statement timeout or making a partially persisted page visible.

Pre-v9 generations retain all three identity columns as `NULL` so migration never invents
history. A runtime, reconciler, preflight, execution, or profile read is executable only when the
active completed generation's identity trio exactly matches the current connector contract.
Catalog and migrator capabilities may still obtain the current raw route to perform the first
governed refresh. A source, catalog-origin, or type-contract identity change therefore requires a
new completed generation before execution. A secret-only rotation that preserves the identity
trio may retain catalog evidence, but its new route revision still invalidates every earlier
target-bearing plan and approval.

The public `CatalogConnectionRoute` contains only public metadata and target facts. Its opaque
binding is carried separately as the application-port-only, `repr=False`
`ManagedCatalogConnectorRoute`, then consumed immediately by the routed DataHub adapter. Public
domain, API, UI, cursor, inventory, refresh-summary, generation, and log models have no binding
field.

### Closed connector capability and dynamic scale

PostgreSQL 16 with the PostgreSQL dialect is the sole executable query connector in M28.
Unsupported dialects may remain catalog metadata but fail before compiler, route, `EXPLAIN`, or
source I/O. Cross-connection execution and federation remain forbidden. A future dialect requires
another ADR plus real compiler, independent guard, identity, cost, timeout, result, type, and
adversarial evidence.

The execution worker, catalog indexer, and aggregate profile worker are no longer configured for
one source. They claim a target-bearing job/refresh, resolve one indexed workspace/connection
route, and open only that capability's transient secret. One process can therefore serve
different configured tenants without materializing every route. This does not couple inventory
and query cardinality: the retained scale profiles remain 10 assets/75 fields and 5,434
assets/41,028 fields, while one query still uses one connection, at most three tables, and at most
two approved joins.

PostgreSQL native types are normalized through one fingerprinted deterministic contract. Scalar
aliases and explicit domain bases map to the existing physical types; arrays, binary/struct, and
unknown/custom values remain visible non-executable evidence. No catalog type creates a mapping,
cast, or approval.

### Cost assessment boundary

Application cost admission runs only:

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

The adapter applies an independent planning timeout, verifies the expected reader and read-only
transaction, verifies source identity, binds the already validated parameters, and installs a
connection-local raw JSON loader. It checks the response-byte limit before `json.loads`, rejects
duplicate keys/non-finite constants, parses decimals directly as `Decimal`, accepts exactly one
plan envelope, bounds nodes/depth/numeric precision, and rolls back. The budget caps total cost,
root rows, width, node count, depth, response bytes, and planning timeout. Only the decision,
stable reasons, bounded scalar metrics, safety facts, and fingerprints may persist or reach the
UI. Raw plan JSON, relation/index names, SQL, parameters, values, credentials, and topology remain
transient.

M28's local owner-only secret resolvers, reference Kubernetes manifests, synthetic sources, and
browser state prove the application contract only. M29 must still provide operated remote secret
management, workload identity, network/TLS policy, observability/alerting, supply-chain evidence,
and production rollback before any production GO.

## M29 operated-runtime architecture

M29 adds a provider-neutral connector-secret port in `application/ports`. The existing owner-only
file adapter and the HTTPS remote adapter implement that port independently. Managed composition
accepts only remote mode: each read requires a short-lived projected workload token with an exact
audience, one closed capability role, an exact immutable secret version, verified TLS, bounded
request/response sizes, no redirects, and no persistent copy. The connector still opens only one
transient capability-specific connection. API, reconciler, observer, and migrator composition
cannot receive connector-secret authority.

The immutable provider version is private route state and is independent from the public
`route_revision`. The route proposal binds a one-way digest of each opaque reference together
with that exact provider version, so a version change invalidates the proposal and approval even
when the opaque reference is reused. Schema v11 stores those versions in an immutable companion
relation and exposes only version-bearing capability loaders. It deliberately does not backfill
v9/v10 rows from `route_revision`; an unversioned historical route is non-executable until an
operator performs a newly approved rotation.

Control-plane schema v10 introduces the exact `schemabridge_observer` principal, schema v11 adds
immutable connector-provider version pins, and schema v12 introduces the distinct
`schemabridge_backup` principal. The observer has default and transaction read-only enforcement
plus `SELECT` only on the security-barrier
`schemabridge_control.operational_queue_snapshot` aggregate and migration identity. It cannot read
queue records, identifiers, SQL, parameters, routes, bindings, audit payloads, or source data, and
it owns no mutation, routine-execution, role-switching, or schema-creation capability.
The backup role may select the complete control schema needed by `pg_dump`, but receives no
runtime, source, migration, role-management, or schema-write authority. Migration v12 fails
closed unless that existing principal is exactly a login with `NOSUPERUSER`, `NOCREATEDB`,
`NOCREATEROLE`, `NOREPLICATION`, `NOBYPASSRLS`, and `NOINHERIT`; it also rejects any membership
that can inherit authority or use `SET ROLE`, role-and-database-specific read-only/timeout
overrides, `default_transaction_read_only` other than `on`, and a missing, zero, or
greater-than-15-minute `statement_timeout`. It does not repair an unsafe role.

Every real backup repeats the check from observed PostgreSQL state rather than trusting the DSN
username. Inside one `REPEATABLE READ READ ONLY` transaction it requires
`SESSION_USER = CURRENT_USER = schemabridge_backup`, the exact DSN database,
`transaction_read_only=on`, the same role posture and safe memberships, and a positive timeout no
greater than both 15 minutes and the configured backup command timeout. The snapshot and
`pg_dump` are not reached on failure. The subprocess receives only `PATH` plus the required
allowlisted `PG*` connection variables; unrelated process secrets are not inherited, and
connection errors are collapsed to sanitized operator codes.

The observer refreshes the four closed queue aggregates into one process-local OpenMetrics
registry. API, execution worker, catalog indexer, profile worker, and reconciler each own a
separate bounded internal metrics listener; the API business listener never exposes `/metrics`.
All six scrape targets emit readiness without source I/O. Structured JSON events use closed
schemas and low-cardinality enums; unknown fields and sensitive payload classes are rejected
before serialization. Only the eight metric families with a composed producer enter the active
dashboard. The active alert contract contains exactly six producer-backed rules and its PromQL is
validated against the canonical token sequence, not merely by referenced metric name. The active
SLO file is the canonical reviewed declaration for exactly `api_availability`, `api_latency`, and
`queue_freshness`. Its validator fixes each ID/category/indicator type, exact field set, ordered
metric tuple and outcome vocabulary, requires positive thresholds, and verifies that each
objective and error budget are complementary. Mutation tests reject changed PromQL operators,
functions, thresholds or vector matching, trailing syntax, SLO fields/outcomes, or an uncomposed
metric even when the same metric families remain. The exact active alert group is delivered as a
`PrometheusRule`.

Uncomposed backup/release/capacity/integrity and SIEM contracts remain explicitly inactive design
artifacts; they cannot page or imply delivery until their producers/export lifecycle are composed.
There is currently no OTLP exporter or collector delivery path. Consequently no workload has an
OpenTelemetry collector egress rule and TCP/4317 is absent from the M29 NetworkPolicies; adding
that path is a validation failure until a real bounded exporter, destination authentication, loss
handling, and operated evidence are composed.

The current Kubernetes base composes eight long-running workloads with distinct service accounts,
immutable external Secret references, projected capability identities only where needed,
restricted pod security, resource limits, topology spread, PDBs, TLS ingress, default-deny
networking, six internal metrics Services/ServiceMonitors, and capability-specific egress-plane
selectors. Migration remains a reviewed one-shot operator workflow rather than a fake Deployment.
Backup is an hourly bounded CronJob using only `schemabridge_backup`, an exact versioned external
Secret, and an externally provisioned PVC; the pod needs no secret-manager or backup-store egress.
The CSI/provider boundary must independently prove encryption and immutable retention. Restore
remains a reviewed explicit command to a distinct empty target, with external cutover. The
production overlay intentionally contains blocking placeholders and is not deployable until the
target operator supplies reviewed image digests, trust roots, hosts, external Secret names,
provider roles, backup storage, and cluster selectors.

The managed web composition is intentionally planning-only at its mutation boundary. It must set
`SCHEMABRIDGE_JUDGE_EXECUTION=disabled` and
`SCHEMABRIDGE_PUBLICATION_MODE=disabled`; consequently it can load the active registry, retrieve a
bounded governed closure, compile deterministic SQL, apply the independent AST policy, and run
remote cost preflight, but it cannot open a source execution connection or load DataHub writer
material. The existing authenticated API and execution worker retain the execution job lane, but
Streamlit does not yet submit to that API. Publication has no equivalent durable approval queue or
publisher worker yet. Both browser actions therefore remain explicit NO-GO capabilities rather
than silently using recorded data or a synchronous writer.

The managed web process is wrapped by `schemabridge-web`. Before starting it validates the typed
configuration, exact projected OIDC document and workload token, and current control schema.
Startup/readiness repeat those checks and require one bounded exact response from the fixed
loopback Streamlit health listener. Managed Operations never substitutes showcase samples for
runtime truth: without an operated data source it renders an explicit unavailable state.

Dependency and build inputs are locked and hash exported. Strict workflow validation rejects
mutable action/image references, YAML aliases/merge keys/duplicates, broad permissions, unsafe
triggers, incomplete vulnerability reports, or attestations that do not cover the reviewed wheel,
SBOM/provenance evidence, and runtime image. Runtime construction uses the exact reviewed Python
3.13.14/Alpine 3.24 subject. The sole Linux sdist is converted into a byte-identical
amd64/arm64 wheel using pinned build tooling and the upstream source epoch, then installed by
exact SHA-256 with the complete frozen wheelhouse through read-only BuildKit mounts; the runtime
has no resolver network, build backend, or retained wheelhouse. The vulnerability-fixed build
backend is isolated in an exact hashed input because DataHub constrains its unrelated application
dependency range; audit evidence covers both application and build inputs.

The backup client is an independently closed Alpine 3.24 package set, not a raw binary copy from a
PostgreSQL image. `TARGETARCH` selects only `amd64` → `x86_64` or `arm64` → `aarch64`; each branch
contains the complete official Alpine URL and a distinct expected SHA-256 for
`postgresql16-client=16.14-r0`, `libpq=18.4-r0`, `lz4-libs=1.10.0-r1`,
`zstd-libs=1.5.7-r2`, and `postgresql-common=1.3-r0`. A controlled download stage verifies every
archive hash without unpacking or repackaging it. The final Python/Alpine stage mounts those signed
APKs read-only and runs `apk add --no-cache --no-network`; `apk` verifies the Alpine package
signatures, package metadata remains visible to SBOM/vulnerability scanners, and no APK archive or
download stage is retained in the runtime image.
Runtime SBOM verification also binds the Python base image's virtual `.python-rundeps` component
to the selected platform exactly: `20260616.002547/noarch` for Alpine `aarch64` and
`20260616.002554/noarch` for Alpine `x86_64`. This closed mapping reflects the two leaves of the
same pinned multi-architecture base-image digest; it does not relax the exact inventory, hash,
architecture, graph, or five downloaded PostgreSQL APK bindings.

Recovery remains a separate capability: signed
archive/manifest pairs are fully reverified, retention is bound to exact reviewed policy and plan
fingerprints, and expired pairs move into a recoverable owner-only quarantine. Restore targets must
be distinct and empty; cutover remains external and automatic down-migration remains forbidden.

Release promotion is a seven-job manual state machine over an existing annotated stable SemVer tag
whose value must equal `v$project_version`; prereleases are rejected. Protected GET-only `audit`
runs before every build, sees drafts and authoritative rules through its environment token, and
either proves a clean new dispatch or validates an exact already-published immutable no-op.
Read-only `prepare` then validates the peeled remote tag, exact hosted CI, public/basic tag-ruleset
metadata, and the exact protected default-branch HEAD, builds and locally gates once, and uploads a
run-scoped prepared artifact without an environment or audit token. Protected inline-only
`candidate` has package-write authority; read-only `scan` supplies one ephemeral GHCR pull
credential to pinned Trivy and emits the canonical payload; protected `attest`, `promote`, and
`release` retain their separated attestation, package-promotion, and contents authorities.
`audit`, `candidate`, `attest`, `promote`, and `release` use `production-release`, so a complete new
publication requires five sequential approvals.

That exact-HEAD contract has an external operating prerequisite: for every new publication, an
administrator opens a recorded release change window and freezes all pushes and merges to `main`
before dispatch until `release` completes its immediate post-publication read-back. The evidence
binds the source SHA, change-window/ticket identity, responsible administrator, and independent
reviewer; each protected approval confirms that the freeze remains active. A historical
already-published `audit` no-op is read-only and does not need this mutation window. If `main`
moves during a new publication, the next boundary fails closed and any registry, attestation, or
draft state already written becomes incident evidence: it is inventoried and preserved, never
deleted, clobbered, overwritten, or bypassed by rewriting `main` or the tag.

The recorded window has a hard maximum of seven calendar days from `audit` start through the
successful post-publication read-back. Operators must not dispatch unless all five approvals can
finish inside that bound. Reaching the bound stops further approvals and enters the same preserved
partial-state incident procedure. The two run-scoped artifacts are retained for 35 days only to
leave a bounded 28-day investigation/recovery buffer; retention never authorizes a rerun,
redispatch, or late publication.

GitHub creates each job's `GITHUB_TOKEN` when that job starts; an environment step cannot delay
token issuance within the job. The boundary therefore uses minimal separate jobs, gives privileged
jobs no checkout, dependency setup, or repository-code execution, and makes each job's first
inline shell step begin exactly with `set -euo pipefail` and verify the exact upstream artifact
ID/digest/hash set, source identity, peeled tag, remote default-branch HEAD, and current rules
before any external mutation. `set +e` and `|| true` error bypasses are forbidden. The sentinel is
checked only after that boundary verification: it confirms that the protected environment was
approved, but is not represented as preceding artifact or policy verification.

Each mutation-capable privileged boundary downloads its predecessor archive through the GitHub artifact API and
validates the reported byte size and digest before extraction. A fixed standard-library ZIP
validator then requires the exact flat file allowlist and count; rejects absolute, nested,
dot-segment, backslash, NUL, duplicate, encrypted, symlink, device, and hostile external-attribute
entries; verifies CRCs; and enforces per-entry, total-size, and compression-ratio ceilings. Only
after that validation may the job extract into a fresh directory, where every result must be a
regular non-symlink file. Before `sha256sum --check`, each checksum file is size-bounded and parsed
as an exact unique lowercase-SHA256/two-space/canonical-basename allowlist; paths and omissions are
rejected.

The authoritative tag policy is read through
`GET /repos/{owner}/{repo}/rulesets?includes_parents=true&targets=tag` followed by an exact
`GET /repos/{owner}/{repo}/rulesets/{id}?includes_parents=true`. Exactly one active tag ruleset
must target `refs/tags/v*`, exclude nothing, expose `bypass_actors: []`, and enforce update,
deletion, and non-fast-forward protection. GitHub exposes `bypass_actors` only with ruleset-write
visibility, while authoritative draft/asset inspection requires contents-write visibility.
Accordingly, the environment secret `SCHEMABRIDGE_RELEASE_AUDIT_TOKEN` has repository
Administration write and Contents write scopes. It is a repository-scoped fine-grained PAT whose
expiry exceeds the maximum planned approval window plus an operator-recorded safety buffer; it is
rotated/revoked under the release credential procedure. A static GitHub App installation token is
unsupported because this workflow does not mint a fresh token per job. The workflow nevertheless
uses the PAT exclusively for explicit read-only `GET` requests, never logs it, and never supplies
it to `prepare`. Privileged
boundaries require GitHub immutable Releases to be
enabled before any publication mutation and require the remote default-branch HEAD to remain
exactly `SOURCE_REVISION`.

Internal artifact names remain run-scoped, while every downstream failed-job rerun consumes the
actual artifact ID, name, and digest emitted by its declared upstream producer. The registry
candidate is the stable semantic reference `candidate-${{ github.sha }}`, and public evidence
excludes run IDs and run attempts. File and OCI attestations may be duplicated by a rerun, but
every bundle must bind the same exact subject, source repository, revision, and tag. A newly
dispatched workflow requires an externally clean candidate, SemVer image tag, draft, and Release
state, established authoritatively by `audit` before build; it also rejects a newer stable Release
before any downstream job. If the same `candidate` job is retried after its push, it may adopt only
the bounded remote manifest whose config digest equals the sealed prepared image. Divergence is an
incident; state is never regenerated, deleted, or clobbered.

Release builds run on the explicitly selected but still mutable `ubuntu-24.04` hosted-runner
image. `SOURCE_DATE_EPOCH=1730470033` normalizes timestamps only; it does not freeze the runner,
kernel, BuildKit, Docker daemon, or toolchain and is not a bit-for-bit rebuild guarantee. The
workflow therefore never relies on a complete redispatch to reproduce canonical artifacts.
`DOCKER_BUILD_RECORD_UPLOAD=false` also prevents the build action from adding an unreviewed build
record artifact. `release-metadata.json` identifies the temporal scanner snapshot rather than
claiming reproducibility: pip-audit 2.10.1 with explicit PyPI service/source and observation time,
plus Trivy 0.69.3, its pinned action revision, DB schema/update/download times, and exact
metadata/`trivy.db` hashes.

The Release has one canonical body and exactly ten digest-checked public assets:
`direct-licenses.json`, `pip-audit.json`, `provenance.intoto.json`,
`release-assets.sha256`, `release-body.md`, `release-metadata.json`,
`runtime-image.cdx.json`, `schemabridge-0.1.0-py3-none-any.whl`, `trivy-image.json`, and
`wheel.cdx.json`. The GitHub UI body must be byte-for-byte identical to `release-body.md`; that
file's digest is present in `release-assets.sha256`, release metadata, and its own attestation.
Immediately after publishing, `release` re-fetches the exact Release ID and latest Release and
requires the exact tag, target/source, title, body, ten assets, `draft=false`, `immutable=true`,
and no newer stable release. A later full dispatch for an already-published tag ends in `audit` as
a strictly read-only no-op after re-downloading, size/digest checking, and attestation-verifying
the exact immutable body/assets plus OCI digest. Historical verification intentionally does not
require that tag still to be latest, no newer stable version, or current `main`, and performs no
edit, reconciliation, upload, or `--latest` mutation.
One repository-global FIFO publication queue
(`queue: max`, `cancel-in-progress: false`) serializes stable releases.

Repository workflow policy can constrain in-repository permissions, but no GitHub API proves the
global absence of competing GHCR `PUT` or GitHub Release contents writers across humans, PATs,
Apps, repositories, and workflows. Before dispatch, an externally administered writer audit must
be current and a custom deployment-protection rule must gate the environment on that evidence.
Missing or stale evidence is a release NO-GO. Immutable Releases, the exact tag ruleset, and the
empty-bypass result are likewise re-read by fresh `audit` and at every mutation-capable boundary
rather than inferred from repository YAML. The exact historical published no-op relies on its
immutable hosted/OCI attestations and does not require current rules.

These components are production-shaped local contracts, not proof of an operated provider or
cluster. Production remains NO-GO until exact external secret rotation/revocation, server-side
cluster admission, telemetry delivery/pages, immutable remote backup retention, fresh-target
restore, rollback, production traffic/SLOs, M30/M31, and independent security/operator approval
are actually exercised.

## M32 copy-first natural-SQL architecture — locally accepted bounded scope

M32 is an output capability layered on the governed semantic and compiler boundaries. It does not
reopen M29's production acceptance, advance M30/M31, or add a source-write path.

```text
untrusted simple or advanced business request
    → bounded source-grounded mention extraction (≤ 12)
    → search across the complete current approved semantic registry
    → deterministic relevant closure (≤ 3 models / 12 fields / 2 joins)
    → strict typed interpretation or closed ambiguity
    → independent context/type/stage/shape validation
    → deterministic representability route (v1 or v2)
    → deterministic semantic resolution and fanout checks (no SQL)
    → signed preview with resolved-plan fingerprint, datasets, mapping/join reviews
    → exact human confirmation
    → reload exact current registry/head, revalidate and re-resolve signed request
    → deterministic parameterized PostgreSQL compiler
    → independent scope-aware AST guard
    → typed-literal standalone renderer
    → independent zero-binding AST guard
    → copy/download artifact (`executed=false`)

optional and separate:
    guarded parameterized query
    → existing cost/authorization/read-only preview lane
```

The first use-case operation prepares the natural-SQL preview. It owns retrieval, interpretation,
validation, ambiguity, deterministic semantic resolution/fanout checks, and fingerprinting so the
human sees the exact approved datasets plus mapping/join evidence and risks being confirmed. It
has no compiler, guard, renderer, cost-preflight, or executor capability and cannot expose SQL.
The second operation confirms that exact preview and generates the copy artifact. It reloads the
current registry and reconstructs the bounded closure from the signed logical-field set without
rerunning retrieval or language interpretation, validates and re-resolves the request and
fingerprints again, then compiles, guards, renders, and re-guards. It has no executor call and
always returns `executed=false`. Optional execution remains a third, pre-existing capability with
its own authorization and read-only controls.

“Complete table context” is implemented as complete governed retrieval coverage, not unbounded
model context. Retrieval traverses every current approved logical model and field in scope.
Lexical scoring uses names, definitions, and governed values, with role compatibility only as a
bonus after a lexical hit. Canonical types, roles, and value constraints are exposed and validated
in the bounded closure rather than used as free-text search tokens. Deterministic closure
construction and resolution subsequently validate current approved mappings, transformation
plans, join contracts, cardinalities, fanout policies, and freshness bindings. Only the
request-relevant 3/12/2 closure crosses into interpretation or planning. A physical search result
without an approved current mapping stays in the separate `needs_mapping_review` lane.

The provider boundary is split into mention extraction and typed interpretation. Mention output is
grounded to exact source spans and capped at twelve. Interpretation can reference only logical
identifiers and closed values supplied in the approved closure and can return only a versioned
typed request or closed ambiguity codes. SQL, physical identifiers, join predicates, tools,
approvals, execution actions, arbitrary expressions, credentials, source rows, and raw samples
have no provider field. Live mode reuses the M27 tenant policy, public-metadata approval, screening,
admission, reservation/settlement, and sanitized audit path; fake/recorded mode is key-free.

Routing happens after structured interpretation:

```text
complete request fits historical flat AnalyticalRequest exactly → v1
anything valid requiring row mode, COUNT_ROWS, OR/NOT, a conditional metric, bucket,
HAVING, window/output-stage operation, advanced alias, or grouping mode → v2
```

Text length, line count, keywords, language, and model confidence never select a route. The
historical version-1 request, validation, resolution, plan, compiler output, and fingerprints
remain unchanged. Version 2 has separate request/validation/resolution/plan envelopes so a union
or base-class serializer cannot discard advanced state.

The version-2 IR contains no general expression or subquery node. Its closed algebra supports
row/aggregate modes, bounded boolean trees, standard and conditional aggregates, numeric buckets,
`HAVING`, ranking/`NTILE`, partition averages and percentages, running/moving aggregates,
`LAG`/`LEAD`, delta/percentage change, output-stage filtering, and deterministic final ordering.
It permits at most four derived window outputs/eight window AST nodes and predicate trees of
depth four/sixteen leaves. Table and join limits stay three and two.

The compiler emits a direct `SELECT` for a simple shape or, only when required, compiler-owned
`aggregated` and `windowed` CTEs plus the final `SELECT`. This topology permits PostgreSQL to filter
window outputs without accepting a user-shaped subquery. Every projection is explicit; CTE names,
window frames, aliases, and expression shapes are derived from typed values.

Copy rendering is deliberately downstream of the first guard. The renderer numbers `%s`
placeholders in quote-aware textual order, reparses the numbered statement, inserts typed literal
AST nodes by parameter index, emits normalized PostgreSQL, and returns it only after a second
zero-binding guard. AST traversal order never determines binding association. The copy artifact
contains dialect, plan version, request/plan/target fingerprints, SQL SHA-256, and
`executed=false`; its SQL and embedded values are transient and excluded from persistence,
telemetry, provider payloads, recipes, and audit events.

The scope-aware guard allows only the compiler's closed CTE topology and approved functions/frames.
It validates physical relation scope and CTE output scope independently, rejects forward/unknown
references, and retains the single read-only statement, allowlist, no-Cartesian, no-repeated-asset,
fanout, limit, and parameter-count boundaries.

The benchmark capability is explicit. Bounded ranking/top-N/`NTILE`, partition averages,
duplicates/`HAVING`, running/moving calculations, `LAG`/`LEAD`, delta/percent change, conditional
metrics, and numeric buckets are representable. Cross/self joins, arbitrary subqueries, set
operations, recursion, and gaps/islands are typed unsupported outcomes. `ROLLUP` remains rejected
until a future design emits reviewed `GROUPING()` flags; otherwise a subtotal `NULL` would be
indistinguishable from a genuine governed `NULL`.

## M34 governed registry publication

M34 turns one immutable M33 proposal into one complete semantic-registry format-v2 document. The
domain assembler is pure and additive: it accepts an explicit empty base or an exact active v2
base, preserves every existing model, mapping, join and provenance decision, and adds only the new
approved model and mappings. Every active mapping carries one fingerprinted physical binding with
workspace, connection, catalog generation/vector, locator, observed DataHub dataset URN, physical
type and metadata fingerprints. Display names and `schema.table` strings cannot construct an
authority URN. Format v1 stays readable as historical input but cannot become an M34 base or a new
forward activation.

The authenticated API can reserve, inspect, authorize and cancel a tenant-scoped durable job. It
uses only the API control-plane credential and never receives a DataHub token. Preparation runs in
a dedicated publisher process, re-reads the exact proposal, retained catalog authority, active
pointer and strict base, and persists the complete candidate before authorization is possible.
Authorization binds the candidate, registry, target, active decisions, actor and fresh session.
Publishing then revalidates all authority, observes the immutable target, writes only when absent
and the authorization is current, and requires exact read-back of the document, approval, audit
and observed `relatedAssets`. Ambiguous external failure becomes bounded read-back recovery rather
than inferred success.

The schema-v14 publication tables retained inside current control-plane schema v15 own the target
reservation, immutable payload, append-only events, database-time leases, hashed transient
capability, monotonically increasing fencing token, bounded retry/dead-letter state and
cooperative cancellation. PostgreSQL privileges and lifecycle triggers split API transitions from
publisher transitions. The publisher can read only the exact catalog/base state needed for
revalidation and cannot update `registry_active_pointers`.

Successful publication finishes at `activation_ready`; it does not activate. A bounded
`SECURITY DEFINER` function returns only the exact handoff identity, receipt and a fingerprint of
the currently revalidated physical bindings. M23 forward prepare and commit bind and re-read that
handoff; the commit repeats the check inside its pointer CAS transaction while holding the job and
active catalog-connection rows. Rollback remains the historical M23 transition and cannot claim a
new publication handoff.

The production-shaped Kubernetes contract therefore has eight long-running workloads. The
publisher has its own service account, control-publisher DSN, exact-version registry-writer secret
capability, PDB, metrics service and default-deny NetworkPolicy. Its only non-DNS egress planes are
the secret manager, control publisher database and DataHub registry writer. It receives no OIDC,
LLM, source, execution, API or registry-reader credential. These manifests remain static local
contracts, not operated-cluster evidence.

## M35 governed registry-v2 changes

M35 adds two immutable delta families over one exact active registry-v2 base. Phase A appends one
approved same-connection join between exact active mappings after aggregate-only profiling. Phase
B replaces/remediates one logical model from an immutable M33 source proposal and accounts for
every incident join as exact preservation, newly profiled upsert, or explicit removal. Neither
family edits an existing registry document or active pointer.

The API composes tenant-bound profile and change commands with strict bodies and exact
idempotency. It can persist/finalize aggregate profile requests, create/list/inspect changes,
record a steward decision and let a separate publisher prepare a proposal. List responses omit
mapping/evidence payloads; unknown and cross-tenant identifiers share the same bounded response.
Source rows, SQL, parameters, DSNs and credentials never enter these records.

Control-plane schema v15 adds append-only M35 requests, jobs, profile witnesses, join/model drafts,
decisions, prepared proposals and audit events. Database functions enforce current catalog/base/
dependency authority, immutable requested-job binding, fenced leases, exact replay and role
separation. A worker rechecks authority by heartbeat before source I/O. Each M33 replacement
proposal is a one-shot provenance source: rejection or staleness requires a new immutable M33
proposal rather than rebinding old evidence.

M34 publication is generalized with closed proposal kind `replace_model_v1`. The publisher reads
the outer replacement proposal plus its exact M33 source, decisions, catalog/base/dependency/M26
authority and profile witnesses. A PostgreSQL witness function rejects any altered closure before
the candidate can become `activation_ready`; the existing isolated write/read-back path and the
separate M23 activation CAS remain unchanged.
