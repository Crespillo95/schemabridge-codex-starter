# Milestone handoff

## Summary

- Milestone: M24 — Authenticated API and durable background workers
- Status: complete — accepted locally on 2026-07-23
- Recommended operator decision: accept M24 locally; retain global production/release NO-GO
- Proposed commit message: `feat: add authenticated fenced execution workers`

All 25 M24 acceptance criteria now pass under schema v3. Fresh full, integration, real-socket,
coverage, wheel, image, release-audit, diff, control-plane, and Codex internal-browser evidence
closes the milestone locally. This is not a production/release sign-off: the tree is dirty and the
later operated infrastructure, scale, matching, operations/security, and supply-chain gates remain
open.

## Implemented

- One closed `execute_workflow_preview` job with immutable scoped authorization, idempotency
  fingerprints, cancellation, finite retry/dead-letter, terminal immutability, and summary-only
  results.
- Signed OIDC bearer verification before strict claim mapping, with fixed asymmetric algorithms,
  bounded same-origin JWKS, no redirects, cache/refresh policy, and sanitized failures.
- Development-only constant-time local bearer verification that retains only a SHA-256 digest.
- Deny-by-default submit/inspect/cancel use cases bound to the exact workflow workspace/owner,
  checkpoint, revision, plan fingerprint, confirmation, and authorization window.
- Bounded FastAPI routes:
  - `GET /health/live`
  - `GET /health/ready`
  - `POST /v1/workflows/{workflow_id}/execution-jobs`
  - `GET /v1/execution-jobs/{job_id}`
  - `POST /v1/execution-jobs/{job_id}/cancel`
- PostgreSQL atomic idempotent submission, `FOR UPDATE SKIP LOCKED` claims, digest-only lease
  capability, monotonic fencing, cancellation, retry/dead-letter, and append-only events.
- Periodic heartbeat supervision throughout synchronous work plus a final fenced heartbeat before
  transition; lost ownership fails closed.
- Current submitter plus immutable historical workflow workspace/owner scope, with API and worker
  continuity only through verified opaque identity lineage. Owner and workspace-wide
  `platform_admin` grants validate the exact historical workspace+submitter pair; invalid lineage
  fails before protected I/O.
- Exact rejected-row totals with bounded per-code materialization, exact unclassified residual,
  and completeness/truncation flags; no persistence-only sentinel reaches HTTP.
- Separate API and worker roles/processes with exact-schema preflight, graceful shutdown, and no
  automatic migration. Uvicorn access logging is disabled.
- Outer API response buffering catches unexpected exceptions before Starlette/Uvicorn, logs only
  request ID plus error type, and returns a sanitized problem without re-raising.
- Cooperative cancellation is checked before preview, before rejection inspection, and before
  each governed source statement.
- Worker revalidation of grant, owner-scoped workflow, active registry, typed plan, deterministic
  compiler, independent AST guard, and bounded read-only source preview.
- Runtime migrations packaged in the wheel, a non-root UID-10001 runtime image, and separate
  hardened API/worker Kubernetes reference manifests.
- No M24 LLM call. API and worker neither require nor receive `OPENAI_API_KEY`.
- M25 retains the separate requirement for tenant-dynamic 10- and 5,434-table inventories without
  weakening the three-table/two-join query bound.

## Files changed

- `src/schemabridge/domain/background_jobs.py`: pure authorization, lifecycle, lease, result, and
  transition contracts.
- `src/schemabridge/application/api_workflows.py`: authenticated submit/inspect/cancel use cases.
- `src/schemabridge/application/job_worker.py`: bounded worker iteration and revalidation.
- `src/schemabridge/application/ports/authentication.py`,
  `src/schemabridge/application/ports/background_jobs.py`: transport/storage ports.
- `src/schemabridge/adapters/identity/`: local and signed OIDC bearer verification.
- `src/schemabridge/adapters/control_plane/postgres_jobs.py`: PostgreSQL job/lease/event adapter.
- `src/schemabridge/adapters/control_plane/threaded_heartbeat.py`: periodic fenced supervision.
- `src/schemabridge/adapters/workflows/read_only.py`: worker execution-only workflow boundary.
- `src/schemabridge/entrypoints/http/`, `src/schemabridge/entrypoints/worker/`: independent
  processes and strict HTTP schemas.
- `src/schemabridge/config.py`, `src/schemabridge/bootstrap.py`: isolated component configuration
  and composition.
- `migrations/control_plane/0002_authenticated_api_jobs.sql`: job/event schema; SHA-256
  `4e828aacfc9cc0db35a5b05838556e35360d7e0322c16e1ed008c94faf4f4efc`.
- `migrations/control_plane/0003_reject_expired_job_success.sql`: schema-v3
  authorization-expiry guard; SHA-256
  `fba8bf2fec35c56be95487c21452ac46a45b3598c5b56cf382414aa57c7240f0`.
- `scripts/smoke_runtime_wheel.py`, `Dockerfile.runtime`, `deploy/kubernetes/`: package and
  deployment-shaped runtime evidence.
- `tests/unit/test_threaded_heartbeat.py`, job/auth/API/worker unit modules: pure/adversarial
  regressions.
- `tests/integration/test_background_jobs_postgres.py`,
  `tests/integration/test_m24_identity_lineage_postgres.py`,
  `tests/integration/test_m24_process_lifecycle.py`: real PostgreSQL, identity, and process proof.
- `tests/acceptance/test_authenticated_api_socket_acceptance.py`: real-socket local/OIDC paths.
- Architecture, security, pipeline, test, runbook, deployment, UI, browser, ADR, README, and task
  documents: contracts, evidence, and explicit remaining gates.

## Commands executed

| Command | Result | Notes |
|---|---|---|
| `make control-plane-migrate` | pass | Forward-only migrations 0001, 0002, and 0003 applied |
| `make control-plane-check` | pass | Five roles current/expected 3, pending none; source/control distinct |
| `make check` | pass | Ruff, format, strict mypy; 1,074 passed, 99 deselected |
| `make test-api-integration` | pass | 16 passed |
| `make test-worker-integration` | pass | 21 passed |
| `make test-integration` | pass | 84 passed, 1,089 deselected; 6 expected DataHub warnings |
| `make test-acceptance` | pass | 19 passed, 1,154 deselected; 1 expected warning |
| `make evaluate` | pass | 11 tables, 465 rows, exact corpus SHA; live LLM `not_run` |
| `make coverage` | pass | 1,173 passed; 7 expected warnings; 81.62% |
| `make runtime-wheel-smoke` | pass | Packaged migrations 1, 2, and 3 loaded outside checkout |
| runtime image build/run inspection | pass | `schemabridge-runtime:m24-final`, UID 10001, schema v3, reader secret `0600`, worker probe ready |
| `.venv/bin/python scripts/release_audit.py` | pass | 507 candidate files, 23 direct licenses; expected dirty-tree warning only |
| `git diff --check` | pass | No whitespace errors |
| Codex internal-browser acceptance | pass | Real API/worker lifecycle; clean 1280 and 390x844; zero protected-data hits |

## Automated test results

- Focused tests: all M24 unit/PostgreSQL/process/HTTP selections passed, including exact
  expiry-at-success, transient/permanent source classification, real restart, identity lineage,
  store failure, heartbeat, cancellation, and role-denial regressions.
- `make check`: Ruff/format, strict mypy over 180 source files, 1,074 passed, 99 deselected.
- API integration: 16 passed. Worker integration: 21 passed.
- Complete integration: 84 passed, 1,089 deselected, six expected DataHub warnings.
- Acceptance: 19 passed, 1,154 deselected, one expected warning; signed OIDC/JWKS, real
  control/source PostgreSQL, replay/collision, cancellation, retry/dead letter, reclaim/fencing,
  tenant denial, and protected-data scans passed.
- Evaluation: passed over the deterministic 11-table/465-row corpus with global SHA-256
  `487388495115265d5fac5a17675e05c236fd4eb4c430613ef02050a5f6010654`;
  live LLM was explicitly `not_run`.
- Coverage: 1,173 passed, seven expected warnings, 81.62%.
- Packaging/release hygiene: wheel smoke, final runtime image, release audit, and diff check passed.

## Operator manual test

Completed through Codex's internal browser after every schema-v3 automated gate passed:

1. Open the sanitized `/health/ready` response and verify exact schema-v3 readiness.
2. Use a reviewed authenticated synthetic workflow at execution approval.
3. Submit through the API and visibly observe `queued → leased → succeeded`.
4. Inspect the summary and verify that it contains no rows or protected material.
5. Replay the exact idempotency identity; collide it with changed content and verify safe `409`.
6. Submit/cancel a queued job with the worker stopped; verify no source I/O.
7. Verify the real-socket leased cooperative-cancellation result, then exercise
   wrong-tenant/role denial visibly.
8. Inspect console/network/log/database metadata and repeat at 390x844.

Expected result:

```text
health ready for schema v3
first submit 202; exact replay 200/same job; changed payload 409
governed job queued -> leased -> succeeded with summary only
queued cancellation terminal before source I/O
leased cancellation remains requested until current-worker acknowledgement
wrong tenant/role indistinguishable from unavailable
console warnings/errors []
no token, claim, key, DSN, SQL, parameter, prompt, source value, or preview row
document/body scrollWidth == clientWidth at 390x844
```

Result: **pass**.

- Real Streamlit created and confirmed workflow
  `m20-2b52cb54ed654526b2193b656b569713` revision 13, plan fingerprint
  `05605abf395ada68f36a2b53cb9db2a39dc466448745076fcee1c64d28ec8945`, plus the
  distinct reclaim workflow.
- API readiness was 200; submit 202 queued; exact replay 200 same job; changed payload 409.
- Queued cancellation reached `cancelled` at attempt 0; transient source unavailability reached
  `retry_wait`.
- A real holder `SIGKILL` left the second job leased; a replacement reclaimed attempt 2 and
  succeeded with stage `publication_proposed`, row count 3, rejected count 3, `truncated=false`,
  and preview fingerprint
  `e7df94d70592eb7be8cccb130bdc8b857d27d59c8609425b46461f35eddafddb`.
- Wrong role and cross-tenant calls both returned `404 execution_job_unavailable`.
- Desktop 1280 and mobile 390x844 had no warnings, errors, or horizontal overflow. Browser
  responses and three jobs/events/logs produced zero protected-data hits.
- Direct navigation to port 8520 was blocked by `ERR_BLOCKED_BY_CLIENT`. An ephemeral, uncommitted
  same-origin relay on 8510 retained the bearer server-side and projected sanitized real-API
  responses. It was acceptance instrumentation, not product UI.

## Architecture and security review

- Dependency direction: domain remains pure; application uses ports/domain; framework/database
  imports stay in adapters/entrypoints; bootstrap remains the composition root.
- Source database writes: none authorized. Worker reaches only the reader/read-only transaction,
  allowlist, row-cap, and timeout path; source `UPDATE`, `CREATE`, and `SET ROLE` were denied.
- SQL/LLM validation: jobs contain no SQL; worker regenerates typed plans, compiles
  deterministically, and reparses/guards final SQL. M24 makes no OpenAI request.
- DataHub mutation approval: worker publication is disabled and no writer credential is composed.
- Secrets/proprietary data: synthetic only. Tokens, claims, keys, capabilities, DSNs, SQL,
  parameters, prompts, values, and rows have no durable/API representation.
- HTTP error containment: unexpected exceptions become bounded `500` problems at the outer
  middleware boundary; only request ID/error type is logged and Uvicorn access logging is disabled.
- Fanout/semantic risks: approved join contracts and the three-table/two-join policy are
  revalidated inside worker execution.
- Post-audit corrections: schema v3 rejects expired success; pre-rotation owner and workspace-wide
  jobs validate the historical workspace+submitter pair; workflow-access `STORE_FAILURE`
  dead-letters as unexpected external state. Fresh complete evidence passes.

## Decisions made

- D070 / ADR 0010: expose one approved preview command and document at-least-once delivery.
- D071: dynamic tenant inventory belongs to M25 and does not change query width.
- D072: supervise synchronous work with periodic and final fenced heartbeat.
- D073: preserve historical workflow scope through verified opaque identity lineage.
- D074: retain exact rejection totals with bounded sampled codes and explicit residual.
- D075: enforce authorization expiry in PostgreSQL success transitions under schema v3.
- D076: dead-letter workflow-access store failure as unexpected external state.
- D077: accept M24 locally with at-least-once/fencing/browser proof while preserving global NO-GO.

## Known limitations or unverified items

- Delivery is at least once; cancellation cannot undo a completed external read.
- API exposes summaries, not asynchronous preview-row downloads.
- No per-principal rate, tenant quota, queue-depth policy, measured pool sizing, autoscaling,
  dashboards, alerts, HA, or SLO; M25/M26 own them.
- Production Kubernetes, TLS, NetworkPolicy, external secrets, IdP, SIEM, remote retention, and
  clean-release rollout are unverified.
- Catalog scale at 5,434 tables remains M25; field-description matching remains M27.
- Final operations/security and supply-chain proof remains M29.
- The dirty combined tree is development evidence, not a release candidate.

## Blockers

- No M24-local blocker remains.
- Production/release remains blocked by the known limitations above and later milestones.

## Next milestone readiness

- Dependencies satisfied: yes for M25; M25 has not started.
- Recommended next prompt: execute M25 as a tenant-scoped 10/5,434-table
  cursor-pagination/indexing/incremental-refresh/load-test vertical slice.
- Required operator prerequisites: preserve schema v3, synthetic service isolation, the reviewed
  role matrix, and the independent three-table/two-join query safety bound.
