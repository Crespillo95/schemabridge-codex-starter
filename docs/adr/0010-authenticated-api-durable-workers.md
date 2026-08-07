# ADR 0010: Signed bearer API and PostgreSQL fenced workers

- Status: accepted and locally evidenced
- Date: 2026-07-23

## Context

SchemaBridge already has strict OIDC claim mapping, deny-by-default workflow RBAC, immutable
workspace ownership, a migrated PostgreSQL control plane, and a guarded read-only execution path.
Those controls are currently reached through Streamlit or synchronous CLI/application calls.
There is no cryptographically verified bearer boundary for a public API, durable work queue, lease
ownership, cancellation contract, or bounded delivery retry.

The first asynchronous operation must not create a generic remote-command surface. Persisting a
model prompt, SQL, parameters, token, identity claims, or preview rows would also weaken the
existing data-minimization and deterministic-compiler boundaries.

## Decision

1. Expose one closed M24 command: enqueue the exact preview execution of a workflow already paused
   at its human execution-approval checkpoint. Generic workflow commands and publication are not
   part of this worker.
2. Verify bearer JWT signature and fixed asymmetric algorithm, issuer, audience, authorized party,
   key ID, and timestamps against a bounded same-origin JWKS endpoint before reusing the existing
   OIDC principal mapper. A constant-time fixed-token adapter exists only for local development.
3. Derive actor and workspace from the authenticated principal. The request may provide only the
   expected workflow revision, plan fingerprint, exact confirmation, and a high-entropy
   idempotency key.
4. Store the idempotency-key digest and immutable authorization metadata, never the raw key. An
   exact replay returns the original job; binding the same key to different content fails.
5. Use PostgreSQL for job and append-only event state. Workers claim with
   `FOR UPDATE SKIP LOCKED`, database time, an expiring lease, and a monotonically increasing fence.
   Heartbeat and every terminal transition require the current lease token and fence. A separate
   supervisor renews the lease periodically throughout synchronous governed work and performs a
   final heartbeat before a terminal transition.
6. Document delivery as at least once. A crashed read-only execution may be reclaimed, but an
   ambiguous workflow external trace is recovered to a typed human-retry state rather than blindly
   replayed.
7. Cancellation is terminal before claim and cooperative after claim. A worker checks before the
   preview, before rejection inspection, and before each governed source statement, then
   acknowledges cancellation explicitly. Source statement timeout remains the final bound once
   I/O has begun.
8. Persist only job status plus workflow stage/revision, row count, result fingerprint, exact
   rejected-row total, bounded rejection counts/codes, and an exact unclassified residual. SQL,
   parameters, prompts, credentials, tokens, claims, and preview rows are excluded from jobs,
   events, responses, and logs.
9. Run API and worker as separate processes and least-privilege PostgreSQL roles. API has no source,
   OpenAI, DataHub-writer, reconciler, or migrator credential. Worker has only the control-worker
   and source-reader credentials.
10. API/worker startup checks exact migration state and never applies DDL. Migrations remain an
    explicit pre-deploy operation.
11. The API uses a dedicated `schemabridge_api` control role; the worker uses
    `schemabridge_worker`. Migration `0002_authenticated_api_jobs.sql` introduces the job schema
    and is reviewed by exact SHA-256
    `4e828aacfc9cc0db35a5b05838556e35360d7e0322c16e1ed008c94faf4f4efc`.
    Final M24 schema version 3 adds `0003_reject_expired_job_success.sql` as a database guard
    against success after authorization expiry. Its SHA-256 is
    `fba8bf2fec35c56be95487c21452ac46a45b3598c5b56cf382414aa57c7240f0`.
12. M24 contains no LLM operation. API and worker do not receive `OPENAI_API_KEY`; any later
    description matching remains an independently typed M27 feature.
13. Preserve the current submitter and immutable original workflow workspace/owner scope on the
    job. API authorization may resolve verified OIDC identity lineage; managed workers read only
    persisted opaque same-lineage bindings and receive no OIDC/JWKS/pseudonymization material.
    Both owner and workspace-wide `platform_admin` grants validate the exact historical
    workspace+submitter pair.
14. Treat workflow-access store failure as unexpected external state and dead-letter it; do not
    misstate infrastructure uncertainty as an ordinary authorization mismatch.

## Consequences

- Read-only duplicate attempts can occur under crash/lease expiry, so metrics and capacity work
  must measure attempts separately from successful logical jobs.
- A caller cannot download asynchronous preview rows in M24; it receives a bounded summary and may
  use the existing interactive preview surface for visible rows.
- Cancellation cannot promise rollback of an already completed external read.
- Publication needs a distinct worker and DataHub-writer credential if it is added later.
- PostgreSQL is sufficient for this vertical slice. M25 adds bounded Psycopg pools and a local
  single-replica load report; Redis/Celery, autoscaling, and production replica sizing remain
  unjustified without production workload evidence.
- Catalog inventory capacity is independent from the per-query safety limit. M25 loads
  tenant-scoped connection/table inventory dynamically and proves local paginated bounded behavior
  for 10 and 5,434 tables; a query remains capped at three physical tables/two joins.

## Rejected alternatives

- Trusting decoded claims without signature verification: permits forged principals.
- Reusing the Streamlit cookie or `X-Forwarded-User`: couples unrelated transports and trusts an
  unverified caller-controlled identity boundary.
- Accepting arbitrary task names or SQL: bypasses typed intent, deterministic compilation, and
  independent AST validation.
- Running in-process framework background tasks: loses work on restart and cannot prove leasing,
  fencing, or cancellation.
- Claiming exactly-once execution: PostgreSQL state and an external read cannot share one atomic
  commit.
- Persisting preview rows in the control plane: violates M23 result minimization and creates an
  undeclared result-retention product.
- Reusing the OpenAI key for job execution: M24 already has the exact reviewed typed workflow and
  deterministic compiler, so an LLM would add cost and a new untrusted execution boundary without
  a product need.

## Acceptance state

M24 was accepted locally on 2026-07-23 after the schema-v3 correction and fresh final evidence.
All five control roles reported `current=expected=3`, no pending migration, and verified
source/control separation. The complete gates passed: `make check` 1,074/99 deselected,
integration 84/1,089 deselected, acceptance 19/1,154 deselected, evaluation over the exact
11-table/465-row corpus, and coverage 81.62%. Packaged-wheel migrations 1–3, the non-root
`schemabridge-runtime:m24-final` image, mode-0600 reader-secret copy, worker readiness probe,
release audit, and diff hygiene also passed.

Codex's internal browser observed a real API/worker lifecycle: readiness `200`, submit `202`,
exact replay `200`, changed-payload collision `409`, terminal queued cancellation, transient
`retry_wait`, real holder `SIGKILL`, lease reclaim at attempt 2, and summary-only success. Wrong
role and cross-tenant inspection both returned the same `404 execution_job_unavailable`. Desktop
1280 and mobile 390x844 were clean, and a scan of three jobs, events, and logs found zero protected
tokens, idempotency material, DSNs, SQL, parameters, prompts, or rows. Because the internal browser
blocked direct navigation to port 8520 with `ERR_BLOCKED_BY_CLIENT`, the acceptance run used an
uncommitted ephemeral same-origin relay on port 8510. It kept the bearer server-side and projected
only sanitized responses from the real API; it is test instrumentation, not SchemaBridge product
UI.

This accepts the ADR and M24's local at-least-once/fencing implementation. It does not approve a
production release: the combined tree remains dirty, and operated Kubernetes/TLS/NetworkPolicy/
external-secret proof, M25 dynamic scale, M27 description matching, and M29 operations/security
and supply-chain gates remain open.
