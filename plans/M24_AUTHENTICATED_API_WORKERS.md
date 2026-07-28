# M24: Authenticated API and durable background workers

- Status: complete — accepted locally on 2026-07-23
- Timebox: 24 hours
- Recommended Codex: GPT-5.6 Sol — Extra High (xhigh)
- Dependencies: accepted M20 identity boundary and accepted M23 durable control plane

## Objective

Expose one production-shaped, authenticated HTTP vertical slice for submitting, inspecting, and
cancelling governed preview-execution jobs. A separate PostgreSQL-backed worker must execute only
an already explicit workflow approval, revalidate the exact workflow and active registry before
read-only I/O, and publish a sanitized result summary. Delivery is at least once; leases, fencing,
idempotency, cooperative cancellation, bounded retry, and dead-letter behavior make that contract
explicit.

M24 does not make the LLM an executor. Jobs never contain bearer tokens, raw OIDC claims, source
credentials, SQL, parameters, prompts, preview rows, or arbitrary commands. The deterministic
compiler, AST guard, source allowlist, table/row/timeout limits, fanout policy, and current-registry
checks remain mandatory inside the worker.

## Scope assumptions

1. The existing Streamlit workflow remains the creation and human-review surface. The first API
   command accepts only an exact workflow already paused at `execution_approval`.
2. Production and staging use signed bearer OIDC tokens. The existing `OidcPrincipalMapper` is
   reused only after cryptographic JWT verification succeeds.
3. A development-only fixed bearer token may be configured as a `SecretStr`; it is forbidden in
   hosted-demo, staging, and production and is never written to logs or state.
4. The HTTP process uses a dedicated control-plane API credential and receives no source DSN,
   OpenAI key, DataHub writer token, reconciler credential, or migrator credential.
5. The worker uses a dedicated control-plane worker credential plus the existing read-only source
   credential. It receives no migrator, reconciler, or DataHub writer credential.
6. The submitted authorization expires no later than the authenticated session. An expired queued
   authorization becomes terminal without protected I/O.
7. Cancellation is immediate while queued and cooperative while leased. Once bounded source I/O
   begins, statement cancellation and the existing statement timeout are the hard limit; the API
   never claims an effect was undone.
8. Preview rows remain process-local. Durable job state exposes only stage, revision, row count,
   deterministic result fingerprint, rejection counts, and sanitized failure codes.
9. M25 owns load testing, pool sizing, quotas, autoscaling, large-list pagination, and measured
   SLOs. M29 owns the final dependency lock/SBOM and production supply-chain proof.

## Public contracts

### HTTP

- `GET /health/live` is unauthenticated liveness and performs no database I/O.
- `GET /health/ready` is unauthenticated, sanitized readiness for exact schema/control-plane state.
- `POST /v1/workflows/{workflow_id}/execution-jobs` requires bearer authentication, the exact
  workflow revision, the exact validated-plan fingerprint, an explicit execution confirmation, and
  an `Idempotency-Key` header.
- `GET /v1/execution-jobs/{job_id}` returns only the authenticated tenant/owner-visible job summary.
- `POST /v1/execution-jobs/{job_id}/cancel` requests cancellation idempotently.

Every JSON request forbids extra fields and has a bounded content length. Resource IDs, actor, and
workspace are derived server-side. Errors use `application/problem+json`, stable safe codes, and a
bounded request ID without stack traces or protected-resource disclosure. Interactive OpenAPI
documentation is disabled in staging/production. CORS is disabled by default.

### Job state

The closed states are:

`queued → leased → succeeded`

`queued → cancelled`

`leased → cancel_requested → cancelled | succeeded | failed | dead_lettered`

`leased → retry_wait → leased`

`leased | retry_wait → dead_lettered`

The only M24 job kind is `execute_workflow_preview`. One immutable authorization envelope binds the
job to workspace, workflow, submitting actor, operation, expected workflow revision, expected plan
fingerprint, authenticated/authorized/expiry times, payload fingerprint, and request fingerprint.
The raw idempotency key is retained only long enough to calculate its digest.

## Deliverables

- Add pure domain models and transition rules for job kinds, statuses, immutable authorizations,
  leases, cancellation, retry/dead-letter classification, and sanitized result summaries.
- Add application ports/use cases for authenticated submission, tenant-scoped inspection,
  cancellation, claim/heartbeat, and one bounded worker iteration.
- Add bearer-verification adapters:
  - OIDC JWT signature/JWKS verification with fixed algorithms, issuer, audience, `azp`, `kid`,
    `iat`, `nbf`, `exp`, bounded token/JWKS sizes, HTTPS in managed profiles, bounded network
    timeout, cache, and fail-closed refresh;
  - constant-time local token verification only in development.
- Add an explicit PostgreSQL migration `0002_authenticated_api_jobs.sql` with immutable job/event
  history, request idempotency, claim indexes, state/lease constraints, and append-only guards.
- Add forward-only migration `0003_reject_expired_job_success.sql` so PostgreSQL itself rejects a
  success transition after the immutable authorization expiry; application checks alone are
  insufficient.
- Add dedicated `schemabridge_api` and `schemabridge_worker` roles. Provisioning is separate from
  migration; every excess grant is covered by a negative integration test.
- Implement atomic submission/idempotency, `FOR UPDATE SKIP LOCKED` claim, database-time lease
  expiry, monotonic fencing token, heartbeat, stale-owner rejection, reclaim, cancel acknowledgement,
  bounded retry scheduling, and terminal dead letter.
- Add a worker handler that reloads the workflow/access grant, verifies the exact authorization,
  revalidates the active semantic registry and deterministic SQL, and uses the existing bounded
  read-only preview path. It never executes job/LLM-provided SQL.
- Add an ASGI entrypoint and a separate worker CLI. Neither starts migrations or the other process.
- Add typed configuration and composition-root wiring without placing framework or database imports
  in domain/application code.
- Add direct, bounded API/runtime dependencies and document why each is required. M24 must not rely
  on accidental transitive imports.
- Add local service commands, health checks, logs with secret redaction, graceful shutdown, and
  internal-browser acceptance support.
- Update architecture, security, query-pipeline, test-strategy, deployment, runbook, UI, ADR,
  examples, project state, decision log, and handoff.

## Implementation sequence

1. Specify typed job/authentication contracts and adversarial unit tests.
2. Add migration v2, role provisioning, and PostgreSQL queue/idempotency/event adapter tests.
3. Add application submission/cancellation/inspection and worker-iteration use cases.
4. Add OIDC/local bearer verification and adversarial token tests.
5. Add ASGI routes, problem responses, request bounds, readiness, and API unit tests.
6. Wire the separate API and worker processes through `bootstrap.py` and CLI commands.
7. Prove two-worker claim/fencing, crash/reclaim, cancellation, retry/dead-letter, restart, role
   denials, schema mismatch, and source read-only enforcement against real PostgreSQL.
8. Exercise authenticated HTTP → durable job → worker → sanitized result using the synthetic OIDC
   provider and the live M23 control plane.
9. Run internal-browser desktop/mobile acceptance, console inspection, response/state/log secret
   scans, full gates, and documentation/state closure.

## Acceptance criteria

- [x] Managed bearer authentication verifies the JWT signature and fixed cryptographic policy
      before claims are mapped; `alg=none`, algorithm confusion, unknown `kid`, wrong issuer,
      audience, `azp`, tenant/group, timestamps, redirected/oversized/malformed JWKS, and outage
      without a valid cached key fail closed.
- [x] Local bearer mode uses one configured secret with constant-time comparison, works only in
      development, and never exposes or stores that secret.
- [x] API requests derive actor/workspace from the authenticated principal, forbid arbitrary
      operation/SQL/credential fields, reject extra or oversized input, and emit only sanitized
      problem responses with request IDs.
- [x] Cross-tenant, unknown-resource, unauthorized-role, and expired-principal requests produce
      indistinguishable safe denial and zero protected workflow/source I/O.
- [x] Submission requires `workflow:execute`, the exact owner/workspace grant, execution checkpoint,
      workflow revision, plan fingerprint, explicit confirmation, and an authorization expiry.
- [x] The raw idempotency key is not persisted. Same key/scope/payload returns the same job; the same
      key with a different payload returns `409` and creates no new job/event.
- [x] Job payload/state/events contain no bearer token, raw claim, secret, DSN, SQL, parameter,
      prompt, preview row, or source value.
- [x] Submission and its initial append-only event commit atomically before `202`; a failure leaves
      neither partial job nor orphan idempotency identity.
- [x] Two concurrent workers cannot own one job simultaneously. Claim uses database time and
      `FOR UPDATE SKIP LOCKED`; every lease has expiry plus a monotonic fencing token.
- [x] Heartbeat/completion/retry/cancel acknowledgement require the exact current lease token and
      fence. An expired or stale worker cannot mutate terminal state.
- [x] A crashed lease is reclaimable. Delivery is documented as at least once; an ambiguous prior
      external operation is recovered or left for explicit human retry, never blindly replayed.
- [x] Automatic retries use a closed transient-error allowlist, deterministic bounded backoff, and
      finite attempts. Functional/stale/authorization failures become terminal immediately;
      exhaustion becomes `dead_lettered`.
- [x] Queued cancellation is terminal before I/O. Leased cancellation is cooperative and does not
      report `cancelled` until the worker acknowledges it. Terminal jobs are immutable.
- [x] Cancellation is rechecked before preview, before rejection inspection, and before every
      governed source statement.
- [x] Rotated owner and workspace-wide `platform_admin` jobs validate the exact persisted
      workspace+submitter pair through verified lineage; invalid lineage performs no protected I/O.
- [x] Workflow-access store failure is dead-lettered as unexpected external state and is never
      relabeled as a normal authorization mismatch.
- [x] Before source I/O, the worker reloads access/workflow/current registry, verifies the exact
      revision/plan/authorization, recompiles and guards SQL, and checks cancellation again.
- [x] Worker execution preserves one read-only `SELECT`/`WITH ... SELECT`, allowlists, three-table
      maximum, no Cartesian join, fanout policy, row limit, and statement timeout. Source DDL/DML
      remains impossible through both AST policy and database grants.
- [x] Durable success exposes only workflow/stage/revision/count/fingerprint/rejection summaries;
      preview rows remain absent from PostgreSQL, API responses, job payloads, events, and logs.
- [x] API, worker, runtime, reconciler, and migrator roles have tested least privilege. API cannot
      claim/complete jobs; worker cannot submit arbitrary jobs, migrate, activate the registry, or
      write the source; neither can assume another role.
- [x] API and worker start independently, require exact schema v3, never auto-migrate, shut down
      gracefully, and expose bounded liveness/readiness without leaking topology or credentials.
- [x] Unexpected HTTP exceptions are contained by the outer boundary as bounded `500` problems;
      Uvicorn access logging, sentinel text, traceback text, and ASGI exception leakage are absent.
- [x] Integration proves exact idempotency, two-worker contention, lease expiry/reclaim, stale
      fencing, cancel races, retry/dead-letter, process restart, schema mismatch, role denial, and
      no source writes.
- [x] End-to-end acceptance proves authenticated submission, replay, collision, successful governed
      preview summary, queued cancellation, cooperative cancellation, dead letter, and cross-tenant
      denial over a real HTTP socket plus real control/source PostgreSQL.
- [x] Internal-browser acceptance visibly proves API readiness and an authenticated job lifecycle,
      with clean console, no secret/error leakage, and no horizontal overflow at 390x844.

## Required automated checks

```bash
pytest tests/unit/test_background_jobs.py \
  tests/unit/test_api_authentication.py \
  tests/unit/test_api_workflows.py \
  tests/unit/test_http_api.py \
  tests/unit/test_worker.py
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

## Manual internal-browser test

1. Start the separately migrated control plane, source database, API, and one worker.
2. Open `/health/ready` and verify the sanitized ready response for schema v3.
3. Open the authenticated application, create/advance a workflow to execution approval, and submit
   the exact job.
4. Inspect the job from the browser and observe `queued → leased → succeeded` plus only the bounded
   result summary.
5. Replay the exact idempotency key and verify the same job; change the payload and verify `409`.
6. Stop the worker, submit/cancel a queued job, restart, and verify no source I/O.
7. Lease a job, stop the worker, allow expiry, restart another worker, and verify reclaim/fencing.
8. Exercise wrong tenant/role and confirm indistinguishable denial with no protected facts.
9. Inspect response bodies, PostgreSQL state, console, and logs for tokens, claims, keys, DSNs, SQL,
   parameters, prompts, or rows.
10. Repeat the visible journey at 390x844 and confirm no horizontal overflow.

## Acceptance result

Accepted locally on 2026-07-23. All 25 criteria above passed.

- `make check`: 1,074 passed, 99 deselected; Ruff, formatting, and strict mypy passed.
- `make test-integration`: 84 passed, 1,089 deselected, with six expected DataHub warnings.
- `make test-acceptance`: 19 passed, 1,154 deselected, with one expected warning.
- `make evaluate`: passed over 11 tables and 465 rows with corpus SHA-256
  `487388495115265d5fac5a17675e05c236fd4eb4c430613ef02050a5f6010654`; live LLM
  remained `not_run`.
- `make coverage`: 1,173 passed with seven expected warnings and 81.62% coverage.
- Wheel smoke loaded packaged migrations 1, 2, and 3. Release audit passed 507 candidate files
  and 23 direct-dependency licenses; its only warning was the expected dirty tree.
- The control plane reported API, worker, runtime, reconciler, and migrator at
  `current=expected=3`, with no pending migration and verified source/control separation.
- Image `schemabridge-runtime:m24-final` ran as UID 10001, discovered schema v3, consumed the
  copied reader secret at mode `0600`, and passed the worker readiness probe.
- Codex's internal browser proved readiness, submit/replay/collision, queued cancellation,
  retry-wait, real-process crash/reclaim at attempt 2 (with stale-fence rejection independently
  covered by the process suite), successful summary-only completion,
  indistinguishable role/tenant denial, clean desktop/mobile rendering, and a zero-hit protected
  data scan. The browser host blocked direct navigation to port 8520, so an uncommitted ephemeral
  same-origin relay at port 8510 kept the bearer server-side and rendered only sanitized responses
  from the real API; it was acceptance instrumentation, not product UI.

M24 made no OpenAI request. This is local milestone acceptance, not production or release approval:
the tree is dirty; Kubernetes, TLS, NetworkPolicy, and external-secret operation remain unproved;
M25 owns dynamic tenant scale, M27 owns description matching, and M29 owns the remaining
operations/security and supply-chain proof.

## Explicit non-goals

- Generic workflow command endpoints, asynchronous publication, arbitrary task dispatch, user
  impersonation, webhooks, or result-row downloads.
- Redis/Celery, distributed workflows, autoscaling, quotas, rate limiting, connection-pool sizing,
  bulk list endpoints, production load/SLO claims, or multi-region queues; M25 owns scale.
- Registry-wide short-description matching or natural-language multi-domain selection; M27 owns it.
- New SQL dialects/connectors or query-cost estimation; M28 owns them.
- Final lockfile/SBOM/provenance, operated remote backup/SIEM, or release/GA claim; M29–M31 own them.

## Handoff requirements

Update `tasks/PROJECT_STATE.md`, `tasks/CURRENT_TASK.md`, `tasks/DECISION_LOG.md`, M24 ADR,
architecture/security/query/test/deployment/runbook/UI docs, `.env.example`, service examples, and
the exact browser-acceptance record. Return `tasks/HANDOFF_TEMPLATE.md` with migration/checksum,
role matrix, endpoint contracts, authentication policy, queue/lease evidence, idempotency/cancel/
retry/dead-letter evidence, service commands, browser results, omitted tests, limitations, and one
proposed commit message.
