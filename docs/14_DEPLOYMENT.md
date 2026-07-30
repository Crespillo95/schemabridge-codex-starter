# Deployment strategy

## Judge requirements

The final project needs an easy, free path for judges to test. A judge may also rely only on the written submission and video, so deployment is one layer of evidence, not the only one.

## Selected M17 topology

```text
Public Hugging Face Docker Space
        │
        ├── packaged deterministic demo context / typed fake LLM
        ├── fingerprint-bound recorded PostgreSQL result/rejections
        └── recorded DataHub catalog and fake local publication
```

The full local deployment keeps real DataHub in the live path. The selected free hosted topology provides:

1. a stable deterministic live demo that preserves the same ports and clearly labels recorded DataHub context;
2. a reproducible local full-DataHub path;
3. video evidence of the real full integration;
4. examples of actual write-back artifacts.

Do not misrepresent a fake as a live DataHub integration.

## Deployment decision

The current comparison and rationale are recorded in
`docs/adr/0005-free-recorded-judge-deployment.md`. The complete build, smoke, cold-start,
troubleshooting, rollback, and unverified-public-test status are in `docs/17_JUDGE_OPERATIONS.md`.

## Health and demo mode

Final application should expose:

- source connection health;
- DataHub connection health;
- LLM adapter mode: live or deterministic;
- write-back mode: disabled, proposal, or direct-approved;
- build/version identifier;
- a one-click demo reset that changes only synthetic/draft state.

## Secrets

Use the hosting platform secret store. Never commit tokens. Service accounts should be scoped to demo assets. Rotate credentials after recording or public testing when appropriate.

## M20 deployment profiles

| Profile | Authentication | Catalog | Registry | Execution | Publication |
|---|---|---|---|---|---|
| `hosted-demo` | fixed pseudonymous local demo | recorded | recorded | recorded | fake |
| `development` | local demo by default; OIDC may be tested | recorded by default | recorded by default | recorded by default | fake unless OIDC |
| `staging` | OIDC required | live | live immutable version | web disabled; worker lane | web disabled |
| `production` | OIDC required | live | live immutable version | web disabled; worker lane | web disabled |

Managed modes are not rendered as selectors. `SCHEMABRIDGE_ENVIRONMENT`,
`SCHEMABRIDGE_AUTH_MODE`, `SCHEMABRIDGE_CATALOG_MODE`,
`SCHEMABRIDGE_REGISTRY_MODE`, `SCHEMABRIDGE_JUDGE_EXECUTION`, and
`SCHEMABRIDGE_PUBLICATION_MODE` are deployment configuration. A conflicting hosted-demo override,
recorded registry/catalog in a managed profile, local live publication, or incomplete managed OIDC
configuration stops startup. Local-demo live catalog/registry/query reads are development-only and
require `SCHEMABRIDGE_ALLOW_LOCAL_LIVE_READS=true`; recorded execution remains the safe default.
Staging and production web settings must explicitly declare
`SCHEMABRIDGE_JUDGE_EXECUTION=disabled` and
`SCHEMABRIDGE_PUBLICATION_MODE=disabled`. The existing API/worker execution lane is separate from
the browser and is not yet wired to Streamlit; no durable publication-submission lane exists.

OIDC non-secret metadata, exact tenant allowlists, and group-to-role allowlists use the variables documented in
`.env.example`. Copy `.streamlit/secrets.toml.example` to an untracked/mounted
`.streamlit/secrets.toml` and replace every placeholder. Keep `expose_tokens` unset. The absolute
redirect URI must end in `/oauth2callback`; all replicas must share a randomly generated cookie
secret. Register the same URI with the provider. Managed startup rejects HTTP redirect/discovery
URLs, a discovery origin different from the configured issuer, a client ID different from the
configured audience, missing/placeholder/weak secrets, and token exposure.

Set `SCHEMABRIDGE_OIDC_ALLOWED_TENANTS` to a non-empty JSON array of exact, case-sensitive tenant
claim values (at most 256 values of at most 120 characters). Inject a unique, random,
at-least-32-UTF-8-byte `SCHEMABRIDGE_PSEUDONYMIZATION_KEY` with at least eight distinct byte values
through the platform secret manager, plus the non-secret
`SCHEMABRIDGE_PSEUDONYMIZATION_KEY_VERSION` (initially `v1`). Do not reuse this key between
deployments. All replicas sharing a control plane must use the same key/version. Generate both the
cookie and pseudonymization secrets with a cryptographically secure secret generator; never copy
the invalid values from the example file.

The production image installs `streamlit[auth]`, which supplies the required Authlib dependency.
Do not expose the app through an iframe: Streamlit authentication does not support embedded apps.
Do not route production traffic to the legacy CLI.

The accepted ID-token lifetime is at most one hour. Live publication additionally requires a token
issued within 15 minutes. Provider group removal is therefore effective on token renewal or expiry,
not merely on a Streamlit rerun. Configure the IdP accordingly and use its token/session revocation
controls for incidents.

The tenant allowlist scopes SchemaBridge workflow state; it does not select different live
DataHub/PostgreSQL credentials. Until tenant-aware data-plane routing exists, allowlist together
only tenants authorized for the same external asset scope or deploy separate instances. Apply
upstream request/concurrency limits as well: M20 bounds each query but does not provide
per-principal quotas.

## M21 semantic-registry deployment boundary

Each deployment selects one exact governed registry with:

```text
SCHEMABRIDGE_SEMANTIC_REGISTRY_ID=synthetic_enterprise
SCHEMABRIDGE_SEMANTIC_REGISTRY_CATALOG_SCOPE=synthetic-demo
SCHEMABRIDGE_SEMANTIC_REGISTRY_MANIFEST_PATH=demo/ground_truth/registries/manifest.yml
```

The ID and catalog scope are checked against both the requested workspace scope and the signed
manifest payload. The adapter rejects unsupported versions, duplicate YAML keys, symlinks, paths
outside the manifest directory, unlisted files, excessive sizes, SHA-256 mismatches, and registry
fingerprint mismatches without falling back to legacy fixtures. Mount the manifest and its listed
bundle read-only. A registry change or revocation after a plan is confirmed invalidates that plan
before SQL preview or rejection inspection.

The bundled values above are for the public synthetic recorded deployment only. M22 reconstructs
the same port from one immutable DataHub document; M23 adds durable activation/migration controls.
Do not present either the copied manifest or one unactivated version document as a complete
production catalog control plane.

## M22 live DataHub registry deployment boundary

Live registry deployments additionally set:

```text
SCHEMABRIDGE_REGISTRY_MODE=live
SCHEMABRIDGE_SEMANTIC_REGISTRY_VERSION=1
SCHEMABRIDGE_SEMANTIC_REGISTRY_READER_ENV_PATH=.local/datahub/mcp.env
```

Staging and production force both catalog and registry to `live`. In development, local-demo live
reads also require `SCHEMABRIDGE_ALLOW_LOCAL_LIVE_READS=true`. `hosted-demo` remains fixed to the
recorded registry and cannot override that mode.

The registry version is published separately through `registry-prepare` and `registry-publish`.
The target is deterministic for the registry ID, version, and a 24-character SHA-256 prefix of the
opaque authenticated workspace ID. The local-demo example is:

```text
urn:li:document:schemabridge-semantic-registry-synthetic_enterprise-v1-w3f92b9304d7d1f8f0cbf410c
```

Its source is the document ID prefixed by `datahub:` and its exact synthetic fingerprint is
`ef480eb7370924ff4c94131a2c6c4063d85652cc83aaec9059538cdc2cf9c1b9`. Fresh read-back must expose
7 models, 31 mappings, 5 joins, 37 decisions, and 7 exact related assets.

Mount the reader credential as an owner-only file and keep it separate from the ignored writer
credential. The HTTP reader has no mutation method, reads only the configured exact URN with
bounded responses, and refuses target edit grants; a live failure never opens the recorded
manifest. The local stock `generatePersonalAccessTokens` privilege is a documented DataHub
residual, not a SchemaBridge operation, and is tolerated only while exact-target mutation remains
absent.

An immutable M22 version does not itself provide a current pointer or compare-and-swap. M23 adds
those controls in PostgreSQL while retaining M22 publication as the separate version-creation
step. A checked-in recipe that cites the M21 recorded source is stale in live evaluation until
that provenance is migrated. Registry-wide matching from a short description remains M27.

The live DataHub/CLI checks do not satisfy deployment acceptance by themselves. The recorded M22
browser run covers fixed-version selection. The separate M23 local rehearsal below covers the
PostgreSQL-authoritative generation, pending/delivered projection, rollback, restore, and
minimized-result boundaries. Neither record is a production rollout claim.

## M23 managed PostgreSQL control-plane deployment

Staging and production use this authority topology:

```text
source PostgreSQL (read-only reader)       DataHub
                │                            │
                └──── web runtime ───────────┘
                         │ runtime role only
                         ▼
              separate control PostgreSQL
                 ▲                 ▲
          reconciler job      migrator/backup job
          reconciler role      migrator role

fresh restore PostgreSQL ← dedicated restore invocation/secret
```

The web deployment sets:

```text
SCHEMABRIDGE_CONTROL_PLANE_MODE=postgres
SCHEMABRIDGE_SEMANTIC_REGISTRY_SELECTION=active
SCHEMABRIDGE_CONTROL_PLANE_SCHEMA=schemabridge_control
SCHEMABRIDGE_CONTROL_PLANE_SCHEMA_VERSION=1
SCHEMABRIDGE_CONTROL_DATABASE_URL=<runtime-role secret reference>
SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY=<audit-key secret reference>
SCHEMABRIDGE_CONTROL_AUDIT_KEY_VERSION=v1
SCHEMABRIDGE_IDENTITY_MIGRATION_KEY=<independent identity-key secret reference>
SCHEMABRIDGE_IDENTITY_MIGRATION_KEY_VERSION=v1
SCHEMABRIDGE_IDENTITY_POLICY_VERSION=v1
```

These are used together with the existing managed OIDC, live catalog/registry, source-reader, and
DataHub-reader configuration. The angle-bracket values above are secret-manager references, not
sample secret values to copy.

The control endpoint must be a different database from `DATABASE_URL`. Staging/production
PostgreSQL URLs require `sslmode=verify-ca` or `verify-full`. Configuration first rejects equal
normalized URL identities; startup and `control-plane check` additionally compare server-observed
address, port, database, and expected user in read-only transactions, so two DNS aliases for the
same database fail closed. The fixed schema name and release schema version are also validated at
startup. An active managed deployment refuses local/SQLite control mode, fixed-version selection,
recorded fallback, missing pointer, unverifiable separation, or a different schema history.

### Credential and process separation

| Process | Control credential | Permitted responsibility |
|---|---|---|
| Streamlit/web runtime | `SCHEMABRIDGE_CONTROL_DATABASE_URL` | exact schema check; active pointer and workspace-scoped managed state |
| Reconciliation job/CLI | `SCHEMABRIDGE_CONTROL_RECONCILER_DATABASE_URL` | inspect pointer/history/audit/outbox; approval-gated projection delivery state |
| Migration/backup job/CLI | `SCHEMABRIDGE_CONTROL_MIGRATOR_DATABASE_URL` | explicit schema migration and transaction-consistent backup |
| Restore invocation | `SCHEMABRIDGE_CONTROL_RESTORE_DATABASE_URL` | restore only into one distinct fresh target |

Do not inject reconciler, migrator, or restore URLs into the managed web environment. Bootstrap
rejects those elevated values for staging/production web composition. Operator jobs also receive
only the DataHub reader/writer material required for their exact command; the PostgreSQL control
roles have no source-database grant.

Each managed mutating operator job also receives the trusted pseudonymous
`SCHEMABRIDGE_CONTROL_OPERATOR_ACTOR_ID` and a JSON role list in
`SCHEMABRIDGE_CONTROL_OPERATOR_ROLES`. Configure them together and grant only the operation's
required role. Managed commands reject `--actor`; local commands derive their principal and treat
that option only as an exact identity check. These values are authorization configuration, not a
replacement for the platform's authenticated job-launch boundary.

Migration is a pre-deployment operation:

```bash
export SCHEMABRIDGE_COMPONENT=operator
export SCHEMABRIDGE_CONTROL_PLANE_MODE=postgres
.venv/bin/schemabridge control-plane migrate --json
.venv/bin/schemabridge control-plane check --json
```

The `operator` component is the only managed configuration boundary that accepts migration,
backup, restore, and multi-role schema-check credentials. It does not load `.env` and rejects
source-execution, DataHub, OIDC, LLM, API-auth, and connector-secret capabilities. The web
component rejects those operator credentials before UI composition.

Every web replica then performs the same read-only exact-version check. No replica auto-migrates.
Blue/green rollout must keep traffic on the old release if the migration/check or active pointer
fails. Since M23 migrations are forward-only, application rollback means deploying a release that
understands the already-applied schema; it does not mean reversing migration history.

Registry cutover publishes a strict immutable version, prepares/approves its exact activation, and
commits a new pointer generation. Query traffic may use the generation while projection is
`pending`; projection delivery is a reconciler operation and never the planning authority.
Registry rollback chooses a previously active strict transition and creates another, higher
generation. It never edits DataHub versions or control history.

OIDC workflow composition in PostgreSQL mode also requires the current opaque workspace/actor
pair to be initialized in the identity-binding store. A completed key rotation preserves verified
same-lineage aliases so the new principal can resolve one historical grant/draft in its original
scope. Unknown or ambiguous lineage stops composition; deployment automation must run the
approval-gated rotation workflow instead of copying or rewriting rows.

### Backup, restore, and cutover

`control-plane backup` writes an owner-only archive and signed manifest to a new owner-only local
directory. Move both together into encrypted, access-controlled platform storage without renaming
or modifying either artifact. M23 does not supply remote retention, schedules, immutable object
lock, or multi-region failover.

Restore is a separate maintenance invocation. The target DSN is deliberately absent from command
arguments and comes only from the secret `SCHEMABRIDGE_CONTROL_RESTORE_DATABASE_URL`. The target
must differ from the active control and source databases and contain no user relations. Do not
route web traffic until post-restore verification reports exact schema checksum, state digest,
table counts, audit chains, pointer/history, pending outbox, and quarantine facts. Cutover then
changes the runtime secret through the platform's normal secret-release process and repeats
`control-plane check` and `control-plane status`.

Legacy import, identity initialization/rotation, and stale-recipe migration expose exact
fingerprint-bound operator commands under `control-plane`. Legacy import requires an offline
unchanged SQLite source; recipe migration requires a completed current-pointer workflow. Identity
operations accept only a short-lived owner-only envelope produced by a separate boundary that
already verified the same OIDC identities under old/new keys. The operator pod/job receives the
identity-migration signing key and inert key version; the evidence file is not mounted into the web
runtime and is destroyed through the platform's secure ephemeral-file procedure after the
operation. Do not perform ad-hoc SQL, direct SQLite copying, identity row rewrites, or recipe edits
as a deployment substitute.

### Verified local M23 deployment rehearsal

The 2026-07-23 local operated rehearsal verified:

- separate source/control services on ports 55433/55434 and distinct
  runtime/reconciler/migrator roles;
- schema version 1 checksum
  `65d9447619165a92e9b8d8c6541a76569564931b6dc9c7ccbd73deb0f3fd09cc`;
- PostgreSQL-authoritative version 6/generation 1, version 7/generation 2 with persistent pending
  projection followed by exact reconciliation, and rollback to version 6 as generation 3 followed
  by reconciliation to `delivered`;
- offline legacy import of exactly two accepted resources with four quarantined and three preview
  rows stripped; replay was exact;
- replay-safe identity initialization/rotation v1→v2 with two bindings and no historical rewrite;
- live DataHub recipe migration from version 43 to 44, with no SQL or preview payload exposed;
- owner-only backup artifacts and a distinct fresh-target restore with identical state SHA-256
  `22b8fdc5568b81e2bdd8626b3315e525c16651f19d3966c538b87cc7948ec2e5`;
- a fresh internal-browser view of generation 3/version 6, projection `delivered`, pointer
  fingerprint `85f131dd53bb5cf2da8538cc171277c4d988b77101e0e5b37da46fed2ef24757`,
  7/31/5 counts, console `[]`, and no horizontal overflow at 390x844;
- final post-fix `make check`: 767 no-service tests passed, plus Ruff, formatting, and strict mypy
  over 160 source files;
- final service gates: integration 58 passed/782 deselected with 6 expected DataHub-overwrite
  warnings; acceptance 15 passed/825 deselected with 1 expected warning; evaluation passed 11
  tables/465 rows with global SHA-256
  `487388495115265d5fac5a17675e05c236fd4eb4c430613ef02050a5f6010654`; coverage passed
  840 tests with 7 warnings at 81.03%, above the 80% gate.

This closes the local M23 deployment rehearsal. The production gate remains open until a real
environment supplies secrets, TLS, encrypted remote retention and restore scheduling, monitoring
and SIEM integration, HA/failover, a clean reviewed release artifact, and the later M24+ API,
worker, concurrency, and quota controls. Local service evidence must not be relabeled as a
production deployment.

## M24 API and worker deployment boundary

M24 adds two independently started processes to the managed topology:

```text
client ── signed bearer ──> schemabridge-api
                               │ schemabridge_api control role
                               ▼
                       PostgreSQL schema v3
                               ▲
                               │ schemabridge_worker control role
                    schemabridge-worker
                       │              │
              active registry     source PostgreSQL
                read only          reader only
```

The API has no source DSN, OpenAI key, DataHub credential, runtime, reconciler, migrator, restore,
or worker control credential. The worker has no API/browser bearer secret, OIDC client material,
OpenAI key, DataHub writer, runtime, reconciler, migrator, or restore credential. It receives only
its control-worker DSN, read-only source DSN, and the read-only material needed to load the active
registry. Neither process applies migrations or starts the other.

Install the direct runtime `api` extra:

```text
FastAPI              typed ASGI routing and strict response validation
Uvicorn[standard]    bounded one-process serving and graceful shutdown
PyJWT[crypto]        asymmetric JWT/JWK signature verification
```

These runtime dependencies are direct because the API must not rely on Streamlit/DataHub
transitive packages. HTTPX is intentionally a direct `dev` dependency only: unit and real-socket
acceptance clients use it, but neither the `api` extra nor the runtime image installs or imports it.
The final runtime image installs the frozen hashed API, PostgreSQL, SQL, worker, and UI dependency
set from `requirements/runtime.txt`, then installs the project wheel with `--no-deps`. The exact
Python 3.13.14/Alpine 3.24 builder converts the verified `watchdog` sdist into the fixed hash in
`requirements/runtime-built.txt`; the runtime consumes both wheels only through read-only
BuildKit mounts with `--no-index`. The vulnerability-fixed backend is isolated and hash-pinned in
`requirements/watchdog-build.txt`, while CI audits it together with both frozen exports. Its
version lock, SBOM, vulnerability scan, provenance, and digest binding are enforced by M29.

`make runtime-wheel-smoke` builds the current wheel, installs its runtime extras into an empty
virtual environment and working directory, verifies that migrations are packaged, and composes
the control-plane migrator without relying on the source checkout. `Dockerfile.runtime` installs
the same package source with only `api`, `postgres`, and `sql` runtime extras and runs as non-root
UID 10001. The final M24 wheel smoke resolved migrations 1/2/3 from `site-packages`; the final
runtime image built successfully, ran as UID 10001, exposed migrations 1/2/3, and completed
`schemabridge-worker --probe-ready` against control schema v3 with exit code 0.
`deploy/kubernetes/m24-runtime.yaml` keeps API and worker in separate hardened workloads and secret
references; it is a reference manifest and has not been operated in a cluster.

### Configuration and secret separation

Both components require:

```text
SCHEMABRIDGE_CONTROL_PLANE_MODE=postgres
SCHEMABRIDGE_CONTROL_PLANE_SCHEMA=schemabridge_control
SCHEMABRIDGE_CONTROL_PLANE_SCHEMA_VERSION=4
```

The API additionally sets `SCHEMABRIDGE_COMPONENT=api`,
`SCHEMABRIDGE_CONTROL_API_DATABASE_URL`, its bind/port/allowed-host/request/concurrency/shutdown
bounds, and one authentication mode:

- development only: a strong secret-managed `SCHEMABRIDGE_API_LOCAL_BEARER_TOKEN`;
- staging/production: the existing OIDC issuer/audience/tenant/group/pseudonymization policy plus
  `SCHEMABRIDGE_API_OIDC_JWKS_URL` and a fixed `RS256`/`ES256` allowlist.

The worker sets `SCHEMABRIDGE_COMPONENT=worker`,
`SCHEMABRIDGE_CONTROL_WORKER_DATABASE_URL`, `DATABASE_URL`,
`SCHEMABRIDGE_REGISTRY_MODE=live`,
`SCHEMABRIDGE_SEMANTIC_REGISTRY_SELECTION=active`,
`SCHEMABRIDGE_WORKER_IDENTITY_LINEAGE_MODE=verified-oidc`, a unique worker ID,
lease/heartbeat/attempt/poll bounds, and read-only active-registry configuration. The lineage mode
is a non-secret routing policy: it lets the worker follow persisted, verified opaque bindings using
only its control-reader credential. It does not provide OIDC issuer/audience metadata,
pseudonymization keys, JWKS material, or bearer tokens. Managed workers reject `exact-local`;
development may select either mode for explicit acceptance drills. The lease must exceed the source
statement timeout and heartbeat must remain below half the lease. The Kubernetes deployment
derives `SCHEMABRIDGE_WORKER_ID` from `metadata.name`; every replica therefore has its own lease
owner rather than sharing a ConfigMap value.

Do not inject a shared `.env` into dedicated managed components. Supply each exact variable through
the platform's workload identity/secret references. `OPENAI_API_KEY` is not a dependency of M24
and must not be present in API or worker workload configuration. The existing key is reserved for
later explicit M27 description matching.

The worker's DataHub reader file is staged without weakening its owner-only runtime contract. The
projected Secret is mode `0440` and readable by pod `fsGroup: 10001`; an init container running as
UID/GID 10001 copies it with `umask 077` into a 128 KiB memory-backed `emptyDir`, changes it to
mode `0600`, and verifies owner UID 10001 plus regular/non-symlink type. The main worker does not
mount the projected Secret at all; it receives only that staged volume read-only. The final
container smoke reproduced a synthetic regular UID-10001 mode-0600 non-symlink reader file.

### Migration and rollout

Migration remains a pre-deploy operator job:

```bash
make control-plane-reset       # local synthetic environment only
make control-plane-migrate
make control-plane-check
```

Migration `0002_authenticated_api_jobs.sql` introduces the job schema with SHA-256
`4e828aacfc9cc0db35a5b05838556e35360d7e0322c16e1ed008c94faf4f4efc`.
Final M24 schema version 3 adds `0003_reject_expired_job_success.sql`, SHA-256
`fba8bf2fec35c56be95487c21452ac46a45b3598c5b56cf382414aa57c7240f0`, with a database predicate
that rejects success after authorization expiry. The local check reports current/expected 3 and
pending none for runtime, reconciler, migrator, API, and worker and verifies source/control
database separation. API/worker/source negative-grant checks and the schema-only worker readiness
contract have current regression evidence; exact final command totals and browser results are in
`tasks/M24_HANDOFF.md`.

Deploy in this order:

1. back up and apply the reviewed migration with the migrator workload;
2. run the exact control-plane check for runtime/reconciler/migrator/API/worker roles;
3. run `schemabridge-worker --probe-ready`, then start the worker with no traffic;
4. start one API process and verify `/health/live` plus `/health/ready`;
5. execute authenticated real-socket acceptance, then route bounded traffic;
6. scale only after M25's local capacity evidence is accepted and production workload/replica
   budgets are separately measured; the local regression report is not production sizing.

`/health/live` performs no database I/O. `/health/ready` checks exact schema state and returns only
`{"status":"ready"}` or a sanitized problem; it does not disclose topology. Uvicorn runs one
process with proxy headers and access logging disabled plus bounded concurrency/graceful shutdown.
The outer API boundary contains unexpected exceptions before Starlette/Uvicorn, logs only request
ID plus error type, and returns sanitized `500` problems without re-raising. A production-like
HTTP/socket focused selection passed 10 tests without sentinel, traceback, or
`Exception in ASGI application` leakage. Horizontal API
replicas are permissible only when the external platform supplies the normal load balancer and
all replicas use the same policy. M25 adds explicit per-process pool/replica arithmetic and a local
single-replica load report; production worker count and pool sizing remain unclaimed.

The worker has no HTTP server. Its Kubernetes startup and readiness probes execute
`schemabridge-worker --probe-ready`, which checks only the exact control-schema history through the
worker role and exits without composing the polling worker or probing the source database,
DataHub, a workflow, or queue availability. Its liveness probe is process-only (`kill -0 1`).
Source/DataHub outages remain typed job failures and must not cause a fleet-wide readiness flap.

Local process targets are:

```bash
make api
make worker
make worker-once
schemabridge-worker --probe-ready
```

`worker-once` is useful for deterministic acceptance and maintenance probes; it processes at most
one poll and exits. `--probe-ready` performs only the schema preflight described above and never
claims a job. `SIGINT`/`SIGTERM` stop the continuous worker between iterations. Cancellation
of a source statement already in progress still relies on the existing statement timeout. During
bounded synchronous work, a separate supervisor renews the current fenced lease periodically and
performs a final heartbeat before transition; lost ownership fails closed. The worker checks
cancellation before preview, before rejection inspection, and before each governed source
statement. Schema v3 independently rejects a terminal success after authorization expiry.

### Role and runtime matrix

| Process | Control role | May do | Must not receive or do |
|---|---|---|---|
| API | `schemabridge_api` | schema check, scoped job submit/read/cancel, scoped workflow/grant read | claim/complete jobs; source/registry/LLM/DataHub/migration/reconciliation |
| Worker | `schemabridge_worker` | schema check, claim/heartbeat/fence/complete, exact workflow execution fields, active-registry/source reads | arbitrary submit; publication; source write; activation/reconciliation/migration |
| Web | `schemabridge_runtime` | unchanged M23 runtime reads/workspace state | API/worker queue transitions |
| Reconciler | `schemabridge_reconciler` | unchanged projection repair boundary | API/worker queue transitions |
| Migrator | `schemabridge_migrator` | explicit reviewed DDL/backup | runtime traffic |

The implementation uses at-least-once delivery. Monitor logical terminal outcomes separately from
delivery attempts and alert on lease reclaims, retry backlog, dead letters, authorization expiry,
readiness failure, and cancellation latency. M24 logs only bounded codes/outcomes; metrics export,
tracing, alert integration, production capacity targets, HA, and SLO proof remain later
operations/pilot gates.

M24 does not make catalog inventory static. M25 now introduces tenant-scoped dynamic connection and
table inventories, cursor pagination/indexing, full reconciliation plus genuine-source deltas, and
operated local fixtures at 10 and 5,434 tables. Final M25 acceptance remains pending. A large
inventory does not authorize a query above three physical tables.

The schema-v3 deployment-shaped evidence includes independent process startup/shutdown,
schema-mismatch refusal without auto-migration, real `Popen`/`SIGKILL` lease reclaim and stale
fencing, real-socket signed-OIDC execution, response/log/state secret scans, the final wheel/image
checks above, and structural tests for the Kubernetes probes, pod-name worker ID, and reader-secret
staging. The internal-browser desktop/390x844 record passed and is documented in
`docs/14_BROWSER_ACCEPTANCE.md`. No production traffic, immutable registry image push, operated
cluster rollout, autoscaling, monitoring, HA, or SLO claim is made.

## M25 dynamic catalog-indexer deployment boundary

The architecture decision is recorded in
`docs/adr/0011-dynamic-catalog-inventory.md`. M25 adds a third independently deployed managed
service:

```text
authenticated client
      │
      ▼
schemabridge-api ── public catalog metadata/refresh requests ─┐
                                                             ▼
DataHub ── read-only scroll ──> schemabridge-catalog ──> PostgreSQL schema v4
                                                             ▲
schemabridge-worker ── fair execution-job claims ─────────────┘

M25's same-origin acceptance panel presents the authenticated API's bounded inventory responses;
the product Streamlit integration remains M27. The web runtime receives no direct catalog-table
privilege and does not scroll DataHub.
```

The API, execution worker, and catalog indexer have separate control credentials, processes,
readiness probes, pools, and replica budgets. They never start one another and never migrate.

### M25 workload configuration

All three managed processes require:

```text
SCHEMABRIDGE_CONTROL_PLANE_MODE=postgres
SCHEMABRIDGE_CONTROL_PLANE_SCHEMA=schemabridge_control
SCHEMABRIDGE_CONTROL_PLANE_SCHEMA_VERSION=4
SCHEMABRIDGE_CONTROL_POOL_MIN_SIZE=1
SCHEMABRIDGE_CONTROL_POOL_MAX_SIZE=8
SCHEMABRIDGE_CONTROL_POOL_MAX_WAITING=32
SCHEMABRIDGE_CONTROL_POOL_ACQUISITION_TIMEOUT_SECONDS=2
SCHEMABRIDGE_CONTROL_POOL_STARTUP_TIMEOUT_SECONDS=10
SCHEMABRIDGE_CONTROL_POOL_CLOSE_TIMEOUT_SECONDS=5
SCHEMABRIDGE_CONTROL_POOL_MAX_IDLE_SECONDS=300
SCHEMABRIDGE_CONTROL_POOL_MAX_LIFETIME_SECONDS=1800
```

These values are bounded defaults, not universal production sizing. Each pool maximum is per
process. Database connection budgeting must sum `max_size × bounded replicas` for API, worker,
catalog indexer, web, reconciliation, and operator jobs, plus administrative headroom. Increase a
pool only after the M25 load report shows its wait/saturation behavior and PostgreSQL has matching
capacity.

The API receives:

```text
SCHEMABRIDGE_COMPONENT=api
SCHEMABRIDGE_CONTROL_API_DATABASE_URL=<schemabridge_api secret>
SCHEMABRIDGE_INVENTORY_CURSOR_SIGNING_KEY=<independent API-only secret>
```

along with its existing bearer/OIDC policy. The cursor key must be strong and distinct from every
bearer, pseudonymization, control-audit, and identity-migration key. The API must not receive
`DATAHUB_GMS_TOKEN`, a source DSN, the catalog control DSN, or `OPENAI_API_KEY`.

The execution worker retains its M24 control-plane identity but no longer receives one global
source `DATABASE_URL`. It resolves the exact approved PostgreSQL route of each leased job through
the worker role and an absolute, owner-only `SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY`. Execution
and profiling workloads must mount separate capability directories. Neither receives catalog
control credentials, DataHub tokens, cursor-signing keys, or OpenAI keys.

The catalog indexer receives only:

```text
SCHEMABRIDGE_COMPONENT=catalog
SCHEMABRIDGE_CONTROL_CATALOG_DATABASE_URL=<schemabridge_catalog secret>
SCHEMABRIDGE_CATALOG_INDEXER_ID=<unique pod/process identity>
SCHEMABRIDGE_CATALOG_POLL_INTERVAL_MS=1000
SCHEMABRIDGE_CATALOG_LEASE_SECONDS=120
SCHEMABRIDGE_CATALOG_SOURCE_TIMEOUT_SECONDS=15
SCHEMABRIDGE_CATALOG_PAGE_SIZE=50
SCHEMABRIDGE_CATALOG_MAX_RESPONSE_BYTES=4194304
SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY=<absolute owner-only catalog capability directory>
SCHEMABRIDGE_CATALOG_STALE_AFTER_SECONDS=900
```

The lease-bound v9 catalog route selects one opaque capability reference. The catalog adapter
resolves that reference immediately before each page from a strict owner-only JSON document
containing the HTTPS DataHub server and read token; neither value is an environment variable or a
public API field. `SCHEMABRIDGE_CATALOG_DATAHUB_CREDENTIAL_BINDING_REF` is retired and rejected.
The indexer must not receive `DATABASE_URL`, `DATAHUB_GMS_URL`, `DATAHUB_GMS_TOKEN`,
API/worker/web control credentials, OIDC/JWKS/bearer material, <!-- gitleaks:allow — names only -->
pseudonymization/audit/identity keys, DataHub writer material, or `OPENAI_API_KEY`. A synthetic
source is for explicit local/scale fixtures only and must not become fallback behavior after a
live DataHub failure.
`SCHEMABRIDGE_CATALOG_SYNTHETIC_ASSET_COUNTS` is therefore allowed only in an explicit development
scale drill and stays `{}` in managed DataHub workloads. The binding reference is a bounded
non-secret value stored only in the private route revision; the actual token remains a separate
secret and neither value is returned by the public API.

Tenant connection/asset/field limits, fixed-window request limits, nonterminal-job capacity,
cursor TTL, and generation retention are durable PostgreSQL policy, not environment variables.
The source page size remains a bounded indexer setting. Provision each workspace policy through
the reviewed operator/deployment path before admitting traffic. A missing policy intentionally
denies registration, refresh, rate, and job-capacity operations. The implemented interface is:

```bash
schemabridge control-plane capacity apply \
  --workspace-id "$SCHEMABRIDGE_TARGET_WORKSPACE_ID" \
  --expected-version 0 \
  --connection-limit 10 \
  --asset-limit 10000 \
  --field-limit 100000 \
  --api-requests-per-minute 1000 \
  --nonterminal-job-limit 100 \
  --generation-retention-seconds 1800 \
  --confirm "APPLY TENANT CAPACITY POLICY" \
  --json
```

Managed actor/role and the migrator DSN come only from trusted environment/secret configuration.
Creation uses expected version `0`; revisions supply the current version. The database function is
migrator-only, fixes `search_path`, and appends every accepted change to an immutable policy
revision table. The interface exists and PostgreSQL operator tests pass; an operated production
policy rollout remains pending.

### Schema-v4 rollout and rollback

Migration `0004_dynamic_catalog_inventory.sql` is additive to the immutable versions 1–3, but
runtime version checks are exact. A v3 binary must refuse schema v4, so the upgrade is coordinated
and must not be described as zero downtime.
Its exact SHA-256 is
`45c7d95e56a336f267cbe29ad390f6fd54a59d826ffd33253d290be0257060bb`.

Use this order:

1. produce and verify a current signed control-plane backup;
2. drain API submissions and execution workers, then stop v3 API/worker replicas;
3. apply the reviewed v4 migration only with `schemabridge_migrator`;
4. run the exact schema/source-separation and six-role privilege checks;
5. start one v4 catalog indexer in readiness-only mode, then `--once` against synthetic/approved
   metadata;
6. start v4 worker and API canaries, verify readiness and pool lifecycle, then route bounded
   traffic;
7. request one catalog refresh and verify atomic promotion before adding indexer/API/worker
   replicas;
8. run scale/load/socket/browser gates and record exact evidence before wider rollout.

Do not down-migrate. Application rollback must use a release that understands the already applied
v4 schema, or restore a separately verified pre-migration backup through the existing explicit
fresh-target/cutover procedure. Blue/green with a distinct control plane is required if the
operator needs continuous service through this exact-version transition.

Fresh schema-v4 reset/migration/check and focused pristine/upgrade/role regressions passed during
implementation. The exact final six-role output, v3 refusal, v3→v4 command transcript, backup
reference, and canary record remain **evidence pending**.

### Catalog process lifecycle and health

The indexer exposes:

```bash
schemabridge-catalog --probe-ready
schemabridge-catalog --once
schemabridge-catalog
```

`--probe-ready` checks exact schema readiness through the catalog role and exits without polling,
resolving a route, opening a source page, or applying a migration. `--once` performs at most one
claim iteration. Continuous mode polls serially and stops between bounded operations on
`SIGINT`/`SIGTERM`. Pool open is a startup gate and pool close is part of shutdown. A liveness
probe checks only the process; DataHub outage is a typed refresh failure and must not restart the
whole fleet through readiness flapping.

`deploy/kubernetes/m24-runtime.yaml` now includes a bounded two-replica
`schemabridge-catalog` Deployment. Each pod derives `SCHEMABRIDGE_CATALOG_INDEXER_ID` from its pod
name, receives only its dedicated catalog DSN and read-only DataHub token, executes
`schemabridge-catalog --probe-ready` for startup/readiness, and uses `kill -0 1` only for process
liveness. It runs as UID/GID 10001 with no service-account token, no privilege escalation, all
capabilities dropped, and a read-only root filesystem. This is a reviewed unoperated reference
manifest; it is not Kubernetes rollout evidence.

Deployments monitor bounded metadata only: queue depth by workspace, requested/leased/staging/
failed refresh counts, refresh age/page/counts, stale inventory age, rate/capacity denials, fair
claim delay, pool in-use/wait/saturation, and sanitized failure codes. Do not export route
bindings, cursor contents, capability digests, tenant identifiers from denied lookups, DataHub
tokens, source values, SQL, or prompts.

Autoscaling, production alert thresholds, HA/failover, and SLOs remain later gates. The operated
local PostgreSQL report passes exact 10/5,434 traversal, 75/41,028 fields and two active large
connections, 126,601/0-byte heap/RSS deltas, a 29.685331-second full refresh, and 5,000
concurrency-16 reads with zero errors at p95/p99 41.762/54.951 ms. Asset and 64-field probes use
their expected indexes under the default PostgreSQL planner without an override. These values size
no production deployment. Final pool/process/package consolidation, DataHub-offline browser
proof, protected-data scan, and desktop/390x844 results remain **evidence pending** before M25
local acceptance.

### M25 role matrix

| Process | Control role | May do | Must not receive or do |
|---|---|---|---|
| API | `schemabridge_api` | schema check; public connection/refresh operations; tenant-scoped keyset inventory reads; rate/job admission | private route/source reads; refresh claim/stage/promote; DataHub/source/LLM/migration |
| Catalog indexer | `schemabridge_catalog` | schema check; private route read; claim/heartbeat/stage/fail/complete/prune catalog generations | public authorization; source write; workflow execution; publication; LLM; migration |
| Worker | `schemabridge_worker` | schema check; fair claim; fenced governed execution; capacity release | catalog route/refresh mutation; API submit; source write; publication; LLM; migration |
| Web | `schemabridge_runtime` | unchanged active-registry and workspace workflow state; present catalog responses obtained through the API boundary | direct catalog-table/route access; catalog refresh ownership; API/worker queue mutation |
| Reconciler | `schemabridge_reconciler` | unchanged DataHub projection repair boundary | catalog refresh/source/queue mutation |
| Migrator | `schemabridge_migrator` | explicit reviewed DDL/backup and version-checked capacity-policy application | runtime traffic or source writes |

The table is implemented and focused positive/negative role tests passed. Exact final
positive/negative grant output remains pending the final PostgreSQL matrix run.

## M26 semantic-change deployment boundary

Schema v5 adds two independently operated processes without adding a seventh control role:

```text
catalog generation / registry pointer
              │
              ▼
PostgreSQL scoped scan queue
              │
              ▼
semantic reconciler ── dependency pages ──> PostgreSQL workflows
        │              dependency pages ──> DataHub scoped recipes
        │
        └── workspace/connection-qualified profile jobs ──> semantic profile worker(s)
                                                    │
                                                    ▼
                                             read-only source

runtime / execution worker ── minimized dependency gate ──> PostgreSQL
API ── read-only reports/findings/impacts ──> PostgreSQL
```

M28 supersedes the original static M26 profile binding. The checked-in Kubernetes reference
contains one semantic reconciler Deployment and one aggregate profile-worker Deployment. That
worker claims bounded jobs across workspaces and connections, while every job persists an exact
connector contract, route revision, route fingerprint, and target fingerprint. Before source I/O
the worker must prove the live lease, capability digest, fence, expiry, workspace, connection, and
unchanged target, then resolve only the profile capability secret. This is dynamic single-source
routing, not federation: each profile still addresses one connection and at most the approved
tables for its join evidence.

### M26 workload configuration

The semantic reconciler receives:

```text
SCHEMABRIDGE_COMPONENT=reconciler
SCHEMABRIDGE_CONTROL_PLANE_SCHEMA_VERSION=5
SCHEMABRIDGE_CONTROL_RECONCILER_DATABASE_URL=<reconciler-role secret>
SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY=<independent audit secret>
SCHEMABRIDGE_CONTROL_AUDIT_KEY_VERSION=<non-secret version label>
SCHEMABRIDGE_CONTROL_OPERATOR_ACTOR_ID=<opaque configured actor>
SCHEMABRIDGE_CONTROL_OPERATOR_ROLES=<closed configured roles>
SCHEMABRIDGE_SEMANTIC_REGISTRY_SELECTION=active
SCHEMABRIDGE_SEMANTIC_REGISTRY_READER_ENV_PATH=<owner-only mutation-free reader file>
SCHEMABRIDGE_SEMANTIC_RECONCILER_ID=<unique pod/process identity>
SCHEMABRIDGE_SEMANTIC_RECONCILER_LEASE_SECONDS=60
SCHEMABRIDGE_SEMANTIC_RECONCILER_POLL_INTERVAL_MS=1000
SCHEMABRIDGE_SEMANTIC_RECONCILER_RETENTION_DAYS=30
SCHEMABRIDGE_SEMANTIC_RECONCILER_MAINTENANCE_BATCH_SIZE=100
```

It receives no `DATABASE_URL`, catalog route/source-write credential, direct DataHub writer token,
OpenAI key, OIDC/bearer secret, pseudonymization key, migrator DSN, restore DSN, or identity
migration key.

Each aggregate profile worker receives:

```text
SCHEMABRIDGE_COMPONENT=worker
SCHEMABRIDGE_CONTROL_PLANE_SCHEMA_VERSION=9
SCHEMABRIDGE_CONTROL_WORKER_DATABASE_URL=<worker-role secret>
SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY=<absolute owner-only profile capability directory>
SCHEMABRIDGE_SEMANTIC_PROFILE_MAX_ATTEMPTS=5
SCHEMABRIDGE_SEMANTIC_PROFILE_RETENTION_DAYS=30
SCHEMABRIDGE_SEMANTIC_PROFILE_MAINTENANCE_BATCH_SIZE=100
```

The profile directory is mandatory for the managed worker. It contains only SHA-256-named,
owner-only PostgreSQL profile documents selected by private v9 routes; it contains no execution,
preflight, or catalog capability. Composition fails before queue/source I/O when the directory is
absent, relative, unsafe, or inaccessible.

It receives no global `DATABASE_URL`, fixed workspace/connection selector, reconciler/audit,
API/OIDC, catalog-route, DataHub, OpenAI, publication, migration, backup, or restore capability.
Every resolved source credential remains `SELECT`-only with read-only transactions and bounded
timeout.

API replicas receive the schema-v5 API DSN and reuse the existing API-only inventory cursor HMAC
key through a separate semantic-change signing context; they receive no
reconciler/profile/source/DataHub capability. Runtime and execution worker read only
`semantic_context_gate_projection`. Catalog receives no semantic table read or decision
capability; its reviewed generation trigger is the only scan-enqueue effect.

### Schema-v5 rollout

Migration `0005_semantic_change_management.sql` is additive to immutable versions 1–4, but all
managed binaries require the exact schema version. Roll out in this order:

1. verify a signed backup and drain v4 runtime/API/worker/catalog traffic;
2. apply v5 with only `schemabridge_migrator`;
3. run exact migration history, source/control separation, and six-role positive/negative checks;
4. start semantic profile workers and reconciler in `--probe-ready` mode;
5. start one profile worker per configured workspace/connection pair, then the semantic
   reconciler;
6. process initial scoped scans and explicitly approve every baseline before enabling affected
   planning;
7. start v5 runtime/API/execution/catalog canaries and verify the pre-I/O gate;
8. run PostgreSQL/DataHub, process/package, protected-data, load, and browser gates before wider
   traffic.

Do not down-migrate. A v4 binary must refuse v5. Application rollback requires a v5-compatible
build or a separately verified fresh-target restore and explicit cutover. Initial rollout is
intentionally fail-closed until dependency coverage and evidence baselines are complete.

### M26 role matrix

| Process | Control role | May do | Must not receive or do |
|---|---|---|---|
| API | `schemabridge_api` | schema check; tenant-scoped read-only report/finding/impact pages | semantic decision/report mutation; profile/scan claim; source/DataHub/LLM/migration |
| Catalog indexer | `schemabridge_catalog` | unchanged catalog refresh; invoke only the reviewed generation trigger as part of promotion | read semantic evidence/reports; commit decisions; profile/source query beyond catalog metadata |
| Semantic reconciler | `schemabridge_reconciler` | scope-qualified dependency reads; scan claim/heartbeat/fence; binding/profile/report/impact/head commits | source DSN; execution; DataHub mutation; API bearer; OpenAI; migration |
| Aggregate profile worker | `schemabridge_worker` | claim/reclaim/heartbeat/complete matching-workspace/connection profile jobs; aggregate-only read-only source evidence | another workspace or connection's jobs; scan/report/decision writes; publication; LLM; migration |
| Runtime / execution worker | `schemabridge_runtime` / `schemabridge_worker` | read only the exact minimized semantic gate before compile/source work | semantic baseline/report mutation; broad catalog/dependency reads |
| Migrator | `schemabridge_migrator` | explicit reviewed DDL, backup/restore controls, immutable migration history | runtime traffic or source writes |

The Kubernetes material is an unoperated structural reference. Local schema checksum, exact grant
matrix, process/package, locator/dependency, quality, coverage, and browser evidence pass, so M26
is accepted locally. NetworkPolicy/TLS/external-secret operation, autoscaling, alerts, HA, and
production SLOs remain unverified; none of the local evidence is a production deployment claim.

## M27 dynamic Query Studio and external-AI deployment boundary

M27 adds Query Studio only to the authenticated Streamlit/runtime composition. It does not add an
LLM or Query Studio endpoint to the M24 API and does not give provider capability to the execution
worker, catalog indexer, semantic reconciler, semantic profile worker, migrator, or DataHub
publisher.

```text
authenticated Streamlit user
        │
        ├── local deterministic slot expansion
        ├── bounded governed PostgreSQL retrieval
        ├── optional admitted typed interpretation ──> OpenAI Responses
        └── signed preview + exact human confirmation
                            │
                            ▼
             existing M26 gate / deterministic compiler /
             independent AST guard / read-only preview
```

The governed and physical lanes remain distinct. The physical catalog may contain 10, 5,434, or
more assets, but only fields with current approved mappings and M26 evidence enter the governed
shortlist. One request remains bound to one connection, three physical tables, and two approved
joins.

### Runtime configuration and capability separation

The runtime uses:

```text
SCHEMABRIDGE_CONTROL_PLANE_SCHEMA_VERSION=8
SCHEMABRIDGE_QUERY_STUDIO_AI_MODE=disabled|fake|live
SCHEMABRIDGE_QUERY_STUDIO_AI_MODEL=gpt-5-nano-2025-08-07
SCHEMABRIDGE_QUERY_STUDIO_AI_REGION=global
SCHEMABRIDGE_QUERY_STUDIO_SIGNING_KEY=<independent runtime-only secret>
OPENAI_API_KEY=<runtime-only provider secret; live mode only>
```

`disabled` is the managed default. `fake` is an explicit key-free local/CI/acceptance mode and
must reject even an empty provider key in its dedicated scenario runtime. `live` requires the
existing key in process environment, the pinned selected snapshot, an exact enabled tenant policy,
and the governed public-metadata surface. The key is never a command argument, browser value,
tracked file, fixture, state field, report, or log fact.

The signing key is independent from OIDC, inventory cursor, control-audit, identity-rotation,
bearer, and provider credentials. It authenticates opaque candidate identities and short-lived
preview tokens; it is not a provider key or semantic approval. Managed deployment rejects a
caller-selected model, base URL, proxy, redirect, prompt, tool, or provider option. The Responses
boundary uses the pinned structured-output contract, `store=false`, no tools, bounded output and
timeout, one governed transient retry, and a pseudonymous safety identifier. `store=false` must
not be described as Zero Data Retention.

External AI policy is durable tenant data and defaults off. Only the migrator operator path may
inspect, prepare, and exactly apply it:

```bash
schemabridge-ai-policy inspect
schemabridge-ai-policy prepare \
  --expected-version <current-version> \
  --proposal-output <new-owner-only-mode-0600-file>
schemabridge-ai-policy apply \
  --proposal-file <same-file> \
  --expected-proposal-fingerprint <exact-fingerprint> \
  --confirm "APPLY TENANT AI POLICY"
```

`prepare` is read-only and exclusively creates a regular owner-only proposal. `apply` revalidates
that exact file and compare-and-swap version. Ad-hoc SQL, an environment key, or a prior campaign
does not enable a tenant. Every temporary enablement must have an exact disable proposal prepared
first and must apply it after success or any failure.

### Selected model and retained accounting evidence

The fresh signed `m27-cheapest-first-campaign-v11` selected
`gpt-5-nano-2025-08-07`. Nano passed the complete 136-case synthetic corpus, so the evaluated order
stopped without calling 5.4 Nano or Luna and there is no runtime cascade. The campaign used 16
interpretation attempts, 15,715 input tokens, 1,204 output/reasoning tokens, and EUR
`0.001394085`.

The accepted content-addressed schema-v2 campaign/ledger attestation is:

```text
reports/m27-query-studio-live-history/
campaign-ledger-attestation-f4fceb557b8dd0a2ddf3c05a5f670ba2ea759f32273ef515e0daf0231a117053.json
```

It HMAC-authenticates the campaign and ledger digests and proves one unique chronological matching
across all 16 signed attempts and all 16 durable policy-v83 reservation/audit pairs. It retains
only non-secret digests and aggregates. Its correlation is an authenticated ordinal witness, not
a native content-derived case/request identity; future evidence should persist one shared
sanitized run/case/repetition/ordinal digest in both surfaces.

### Schema-v8 rollout

Migrations 0006–0008 add governed field search, tenant provider policy/admission/audit, successful
settlement bounds, serialized reservation/admission/daily-usage locks, refreshed post-lock lease
time, deterministic audit-derivation preflight, and exact wrapper/core ACLs. They are additive to
the immutable v1–v5 history, but every managed component requires the exact release schema
version.

Use this order:

1. verify a signed control-plane backup and drain schema-v7 traffic;
2. apply v8 only with the migrator and run pristine plus populated v6/v7 history preflights;
3. run exact schema/source separation and six-role positive/negative checks;
4. keep every tenant's external-AI policy disabled while starting runtime/API/worker/catalog/
   reconciler/profile canaries;
5. verify dynamic governed retrieval, signed preview/confirmation, the M26 pre-I/O gate, and the
   fake browser matrix before considering a tenant opt-in;
6. for an approved synthetic/live drill, prepare the disable revision before enabling, run only
   the exact bounded operation, and disable immediately after any result;
7. run the complete integration/acceptance/evaluation/package/release/quality/coverage/diff matrix
   on the final bytes before wider rollout.

Do not down-migrate. A schema-v7 binary must refuse v8. Rollback requires a v8-compatible build or
the separately verified fresh-target restore/cutover procedure. A provider outage or invalid
provider output must leave guided mode available and must never fall back to fake while labelled
live.

### Local M27 evidence and production boundary

The local fake/runtime internal-browser matrix passed at desktop and 390x844 over exact
10-asset/75-field and 5,434-asset/41,028-field profiles with 31 governed mappings. It covered
guided pagination, short Spanish matching, all five governed cases, ambiguity/no-match/stale/rate/
quota/provider-down states, non-executable `needs_mapping_review`, deterministic SQL/AST/read-only
preview, hidden parameter values, hostile text, clean console, protected-data scans, and no
horizontal overflow.

A separately authorized one-submission live-UI drill enabled policy v85, but the internal browser
rejected `http://127.0.0.1:8510` under its URL security policy before loading the page, form,
click, submission, or provider boundary. The operator did not bypass that control. Policy v86 was
applied disabled immediately afterward and remains current. One PostgreSQL
`REPEATABLE READ, READ ONLY` snapshot found zero policy-v85 reservations, interpretations,
settlements, audits, request-window updates, or daily-usage updates; post-v86 there are zero
active/open/stale reservations and zero reserved tokens. The historical schema-v2 attestation
still verifies after v86. The block is recorded as a pre-provider browser limitation, not a
provider request, product success, or model-quality failure.

The final local deployment gate passes. Migrations 1–8 apply and all six service roles report
current/expected v8, no pending migration, and source/control separation. The focused plan passes
552 tests; final full integration/acceptance pass 148/35; API/worker pass 18/24; deterministic
evaluation, runtime wheel, release audit, Ruff/format/mypy, 2,281-test `make check`, 2,459-test
coverage at 81.47%, the reviewed 41,028-field index/cold-budget plans, and diff hygiene pass.

One first full-integration run exposed a test-only fixture that used `clock_timestamp()` inside an
exact fenced transaction. Changing only that fixture to transaction-stable
`statement_timestamp()` preserved the production lease guard; the focused regression and complete
148-test rerun passed. Release audit exited 0 after 697 candidate files, 23 direct licenses, and
zero external links, while retaining the dirty-tree warning. M27 is therefore complete and
accepted locally, but this is not clean-room release evidence.

Despite those local passes, neither M27 nor locally accepted M28 is a production deployment GO.
M29 is now in progress, but its checked-in contracts are not operated evidence. Production still
requires tenant provider
DPA/retention/region/legal approval, data classification, operated remote secret rotation,
TLS/NetworkPolicy, monitoring/SIEM, capacity/SLO evidence, HA/DR, clean release artifacts, and
M29–M31 acceptance.

## M28 governed connector-routing deployment boundary — accepted locally

M28 removes the managed deployment's global source/DataHub credential assumption. A route is
selected from durable tenant data by exact workspace and connection, but its four private binding
references remain outside every public domain/API/UI/job/generation model. The same worker or
catalog process may serve many bounded tenant routes; deployment replica count is no longer tied
to the number of configured tables or connections.

```text
public target: workspace + connection + PostgreSQL dialect + route revision/fingerprint
               + expected reader + source/catalog/type identities + cost budget
                                  │
                                  ▼
lease/fence/capability-scoped private route lookup
                                  │
        ┌─────────────┬───────────┼────────────┬─────────────┐
        ▼             ▼           ▼            ▼             │
   preflight      execution    catalog       profile          │
   secret dir     secret dir   secret dir    secret dir       │
        │             │           │            │               │
        └──── exact target/identity revalidation before I/O ───┘
```

`QueryPlan` remains vendor-neutral. `ResolvedSemanticPlan`, compiled/guarded SQL, job
authorization, retries, and the sanitized cost assessment bind the exact public target
fingerprint. Only PostgreSQL has a complete compiler/guard/preflight/preview toolchain. M28 does
not implement federation or an executable Snowflake/BigQuery/MySQL dialect, and a catalog-only
source cannot be silently routed through PostgreSQL.

### Schema-v9 rollout and immutable catalog evidence

Migration `0009_tenant_connector_routing.sql` is additive to versions 1–8. It introduces
immutable connector contracts, route revisions, private capability bindings, heads, audits,
target-bearing execution/profile work, and source/catalog/type identity on catalog refreshes and
generations. Managed binaries require exact schema version 9.

Roll out only in this order:

1. take and verify a signed backup; stop v8 writers and drain every non-terminal legacy query,
   refresh, and profile job;
2. run pristine and v8→v9 preflights with the migrator; retain immutable 0001–0008 checksums and
   exact role grants;
3. provision capability-specific owner-only connector secrets without exposing their values to
   the route operator, image, manifest, ConfigMap, log, or browser;
4. inspect, prepare, separately approve, and CAS-apply each initial route through
   `schemabridge-connector-route`;
5. run a full catalog refresh so its promoted generation receives the exact current source,
   catalog, and type-contract identity;
6. start catalog/profile/execution/runtime canaries and prove wrong-scope, stale-route, wrong-
   reader, identity, lease/fence, unsupported-dialect, and cost failures before source I/O;
7. run two-tenant isolation, 10/75 and 5,434/41,028 scale, browser, package, release, quality,
   coverage, and diff gates on the same bytes before considering local acceptance.

Do not down-migrate. A v8 process must refuse schema v9. Legacy null-identity generations remain
historical and non-executable. A source, DataHub, or type-contract identity change requires the
next reviewed contract and a fresh promoted catalog generation; runtime remains fail closed until
then. A secret-only rotation may preserve the generation identities, but its new route revision
still invalidates every old confirmed plan.

### Secret and process capability matrix

Each managed source capability uses an absolute directory configured by
`SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY`. The checked-in Kubernetes reference stages three
distinct `emptyDir.medium: Memory` volumes for execution, catalog, and profile, with init
containers enforcing directories mode `0700` and files mode `0600`. The web/runtime preflight
capability must be mounted separately by the operated deployment; it must not reuse execution,
catalog, or profile files. M29 still owns the remote secret manager/CSI integration, rotation
procedure, NetworkPolicy/TLS, observability, and supply-chain evidence.

| Process | Public route authority | Private capability | Must not receive |
|---|---|---|---|
| API | connection/refresh request and public status only | none | route bindings, source/DataHub secrets, compiler, preflight |
| Web/runtime | exact public target for confirmed workflow | `preflight` only | execution/catalog/profile binding, source preview credential |
| Execution worker | target-bearing leased job | `execution` only | preflight/catalog/profile binding, global source DSN |
| Catalog indexer | lease-owned workspace/connection route | `catalog` only | execution/profile/preflight binding, global DataHub endpoint/token |
| Profile worker | target-bearing leased profile job | `profile` only | execution/catalog/preflight binding, fixed tenant selector |
| Reconciler | current immutable generation identity | none | source/DataHub route secret |
| Migrator/operator | reviewed route artifacts and private references only during apply | control-plane mutation | runtime traffic or source writes |

PostgreSQL source documents bind the DSN to the exact expected reader and dialect. DataHub
documents bind normalized server origin and platform. Local resolvers require owner-controlled
regular files, content-addressed names, bounded strict JSON, no symlink/traversal, and stable
metadata across open/read. Public errors expose only stable codes and approved fingerprints.

Managed catalog startup rejects `DATAHUB_GMS_URL`, a global DataHub token, and synthetic catalog
counts. Managed source processes reject a global `DATABASE_URL` fallback. Secrets are injected at
runtime only and are never baked into images or committed.

The execution worker receives two non-interchangeable scopes for a rotated identity: the current
job workspace for lease ownership and the immutable historical connector workspace for
workflow/route/generation/private-binding lookup. Both are persisted and validated; deployment
configuration cannot redirect the historical target to the current workspace.

### Cost preflight and capacity boundary

The application issues a single bounded, parameterized PostgreSQL
`EXPLAIN (FORMAT JSON, COSTS TRUE, ANALYZE FALSE, BUFFERS FALSE, VERBOSE FALSE, SETTINGS FALSE)`
inside a read-only transaction with an independent timeout and rollback. It persists only
sanitized scalar evidence. A connection-local loader rejects raw JSON above the hard/budget
response bound before decoding and preserves exact decimal semantics. It never uses
`EXPLAIN ANALYZE`; an engineering-only
`EXPLAIN (ANALYZE, BUFFERS)` against synthetic test data is separately labelled diagnostic
evidence and is not a deployed request path.

Preflight runs before approval and again immediately before preview. Route and semantic identity
are revalidated each time. Rejection, timeout, invalid response, unavailable target, changed
statistics over budget, or stale route produces zero preview. Catalog scale is dynamic—10,
5,434, or more tables are data—while each executable request remains one connection, at most
three tables/two joins, a result cap, and a statement timeout.

The reviewed PostgreSQL type-contract SHA is self-validated once at process import and reused in
O(1) for each normalized field. Extreme FULL pages retain one transaction and lease while field
rows are inserted in batches of 500; the deployment must not increase the statement timeout to
compensate for one oversized JSON parameter. The authoritative local full-refresh regression
budget remains 60 seconds in `test_catalog_scale_postgres`. Query Studio's second fixture setup is
diagnostic timing only. Cold first browse and each of 34 steady-state pages are independently
bounded below five seconds in synthetic regression evidence; these are not production SLOs.

### M28 acceptance and production boundary

The reference manifests and local helper remain structural/synthetic evidence only. M28 is
accepted locally with 164 integration tests passed, one known skip, and 662.06 seconds; acceptance
passed 47 tests in 133.50 seconds. The final Codex internal-browser record passed all nine scenarios
at desktop 1280x720 and mobile 390x844, with clean final consoles, no overflow, no XSS, zero
forbidden hits, exact cleanup, and sanitized state fingerprint
`2b854c0596ada31ab5a80f5e6d25c23282f0096ac90a712e953476dff899be33`.

The post-fix final-byte quality gates pass: `make check` completed Ruff, strict mypy over 276 source
files, and 2,714 tests with 207 deselected in 989.17 seconds; full coverage passed 2,920 tests with
one retained M27 fixture skip and seven expected DataHub attribution warnings in 2,626.35 seconds,
reaching 81.76%:

```text
M28_POST_FIX_MAKE_CHECK=PASS_2714
M28_POST_FIX_COVERAGE=PASS_81.76_PERCENT
```

Local M28 acceptance did not itself start M29 and was not a production deployment GO. M29 is now
in progress, while operated external
secrets, rotation/revocation, TLS/NetworkPolicy, metrics/logs/traces/SIEM, alerts and runbooks,
production replica/traffic/tenant scale, availability/SLOs, backup/restore and HA/DR drills,
provider/legal governance, real-tenant metadata evaluation, penetration/security review, a clean
signed release identity, and M29–M31 remain required.

## Release gate

- cold start tested;
- mobile is optional, desktop browser required;
- no login preferred; otherwise clear test credentials;
- no paid user action;
- deployment remains available through judging;
- demo data reset is reliable;
- application degrades honestly when an integration is unavailable.

## M29 production-shaped deployment profile

`deploy/kubernetes/m29` is the canonical M29 deployment contract. Its base contains seven
long-running workloads—web, API, execution worker, catalog, profile, reconciler, and observer—with
distinct service accounts and control identities. Connector workload identity exists only for web
preflight, execution, catalog, and profile. Six internal metrics targets use port `9464`; API
business traffic remains on `8520` and does not serve `/metrics`.

The production overlay is intentionally non-deployable as committed. Before use, an operator must
replace every `.invalid`, `replace-with-*`, all-zero digest, placeholder trust bundle, public host,
external Secret name, provider role, registry scope, and cluster selector with an independently
reviewed exact value. External Secret objects and values, private endpoints, credentials, and
certificate material must remain outside the repository.

Render and validate:

```bash
kubectl kustomize deploy/kubernetes/m29/overlays/production > rendered-m29.yaml
python deploy/kubernetes/m29/validate_rendered.py rendered-m29.yaml
kubectl apply --server-side --dry-run=server -f rendered-m29.yaml
```

The local validator requires the exact closed resource graph, digest-only images, distinct
identities, immutable versioned external references, no application RBAC or embedded Secret,
restricted pod security, probes, graceful termination, limits, topology spread, PDBs, TLS ingress,
default-deny networking, capability egress selectors, and metrics scrape isolation. Server-side
dry-run is mandatory because static YAML cannot prove target Kubernetes/CRD/admission/CNI/ingress
behavior.

Managed runtime settings require exact-version HTTPS remote secrets with verified trust and
projected audience-bound identity; no local/global fallback is permitted. Every control DSN uses
verified TLS and the component-specific principal. The observer principal is exactly
`schemabridge_observer` and receives only aggregate operational reads.

The web ConfigMap explicitly sets execution and publication to `disabled`. Query Studio remains
useful for bounded field matching, active-registry resolution, deterministic compilation,
independent SQL validation, and cost preflight, while the web pod receives no execution or DataHub
writer credential. The authenticated API and execution worker already provide a queued execution
boundary, but this release has no Streamlit-to-API submission client. It also has no typed durable
publication approval queue or dedicated publisher worker. Treat both web actions as NO-GO until
those separately reviewed lanes are implemented; never work around the boundary with `live` or
recorded/fake production modes.

Control schema v11 separates each private provider version from the public connector
`route_revision`. Roll out migration `0011_connector_secret_versions.sql` before the binaries that
call the `*_v2` route functions. Existing v9/v10 route rows are intentionally not assigned an
inferred version and therefore return no executable private route. Rotate each required route
through the reviewed operator using binding artifact format v2, verify the exact external version,
then drain/revoke the former credential. Never repair an unversioned route with direct SQL.

Deployment acceptance additionally requires a clean immutable revision, frozen install,
wheel/image SBOM and vulnerability evidence, signed protected-release provenance, actual
provider rotation/revocation, real metrics/SIEM/page delivery, encrypted immutable remote backups,
a verified distinct-target recovery and rollback exercise, production capacity/SLO evidence,
M30/M31, and external security/operator sign-off. Until those actions are operated, the profile is
reference evidence and production/release remains NO-GO.

The repository does not currently prove GitHub release protection. Before publishing, an operator
must create the exact `production-release` environment, require independent reviewers, restrict
eligible refs, and provision the environment-only
`SCHEMABRIDGE_RELEASE_APPROVAL_SENTINEL`. The release workflow validates this 32–128-character
sentinel as its first step and reruns the clean exact-revision release audit before registry
authentication. A missing environment or sentinel blocks every publish/attestation step.
