# M33 milestone handoff

## Summary

- Milestone: M33 — Generic governed semantic onboarding
- Status: complete; accepted locally on 2026-08-02
- Recommended operator decision: accept the bounded local M33 scope; retain production/release
  **NO-GO**
- Proposed commit message: `feat: add governed semantic onboarding`

## Implemented

- Read-only authenticated preflight derives one exact active catalog generation, physical
  observations and active-registry base in a tenant-scoped snapshot; creation must confirm its
  fingerprint.
- Generic logical model/mapping drafts persist under control-plane schema v13 with exact
  workspace/connection/generation/locator/fingerprint/type authority, stable advisory locking and
  atomic revalidation before mutation.
- Every model and mapping starts `needs_review`; confidence and exact names never approve. Steward
  decisions are append-only, evidence-bound and CAS-checked.
- A distinct current publisher may prepare one immutable audited `ready_for_publication` proposal.
  M33 has no publication, activation, SQL, preview, source, DataHub writer or LLM capability.
- Physical identity uses the exact workspace/connection/generation/`schema.table.column`
  coordinate, so asset aliases cannot authorize two meanings. Nested/quoted/mixed-case physical
  identifiers fail closed.
- Verified opaque identity lineage persists the actor paired with the historical workspace,
  retains separation of duties and exact concurrent replay, and rejects incomplete/ambiguous
  aliases.
- Transformation authoring validates ordered types. Regex is an anchored linear PostgreSQL-safe
  ASCII subset, identifier padding is capped at 256, and `parse_date` is rejected by authoring,
  compiler and guard until shape/calendar parsing is total.
- Recent inspection is bounded to 25 rows by default and 50 maximum with explicit truncation;
  durable exact replay stores one root plus append-only decisions instead of quadratic snapshots.
- The Spanish commercial usage plan states the exact current support matrix, LearnSQL benchmark
  coverage, capacity bounds, client onboarding flow and M34/M30/M31/GA gates.

## Files changed

- `src/schemabridge/domain/semantic_onboarding.py`: immutable onboarding contracts and bounds.
- `src/schemabridge/application/semantic_onboarding.py`: preflight/create/review/inspect/prepare use
  cases.
- `src/schemabridge/application/semantic_onboarding_authorization.py`: closed authenticated RBAC and
  separation of duties.
- `src/schemabridge/adapters/catalog/postgres_semantic_onboarding.py`: exact retained catalog
  observation resolver.
- `src/schemabridge/adapters/storage/postgres_semantic_onboarding.py`: tenant-bound durable CAS,
  audit, history and replay.
- `src/schemabridge/adapters/storage/identity_resolving_semantic_onboarding.py`: verified historical
  workspace/actor routing across opaque-id rotation.
- `migrations/control_plane/0013_semantic_onboarding.sql`: schema-v13 tables, constraints, indexes,
  grants and atomic authority checks.
- `src/schemabridge/entrypoints/http/app.py`, `src/schemabridge/entrypoints/http/schemas.py`, and
  `src/schemabridge/bootstrap.py`: authenticated bounded HTTP composition.
- `src/schemabridge/domain/resolution.py`, `src/schemabridge/domain/transformations.py`,
  `src/schemabridge/adapters/sql/compiler.py`, and `src/schemabridge/adapters/sql/guard.py`: ordered
  transformation typing and fail-closed dialect/runtime bounds.
- `scripts/m33_semantic_onboarding_scenario_app.py`: synthetic local presentation path using the
  real M33 application layer and no external adapter.
- `docs/19_COMMERCIAL_USAGE.md`, `docs/adr/0016-generic-semantic-onboarding.md`,
  `plans/M33_GENERIC_SEMANTIC_ONBOARDING.md`: support, decision and acceptance contracts.
- `tests/unit/test_semantic_onboarding*.py`, `tests/integration/test_semantic_onboarding_postgres.py`,
  and `tests/acceptance/test_m33_semantic_onboarding.py`: boundary, persistence and presentation
  evidence.

## Commands executed

| Command | Result | Notes |
|---|---|---|
| Required document/plan inspection with `sed`/`rg` and worktree inspection with `git status`, `git diff` | pass | No source mutation during audit; unrelated user work was not discarded |
| Focused transformation/compiler/guard/M33 pytest selections | pass | Final independent selection: 225 unit cases; all focused reruns green |
| `.venv/bin/pytest -q tests/integration/test_semantic_onboarding_postgres.py` | pass | 7 PostgreSQL cases after the conservative `parse_date` close |
| `.venv/bin/pytest -q tests/acceptance/test_m33_semantic_onboarding.py` | pass | 2 acceptance cases |
| Relevant `.venv/bin/ruff format`, `.venv/bin/ruff check`, and `.venv/bin/mypy ...` | pass | Final independent Ruff and seven-module mypy selections pass |
| `git diff --check` | pass | No whitespace errors |
| `make demo-reset` followed by exact socket acceptance retry | pass | An earlier extended selection had one `source_unavailable` while the synthetic demo DB was down; reset restored it and the exact retry passed |
| `make check` before state/handoff finalization | pass | 3,682 passed, 233 deselected in 725.36 s; supply-chain, release audit, 664-file format, Ruff and 331-module mypy passed |
| `make check` after all state/handoff updates | pass | 3,682 passed, 233 deselected; this is the exact final working-tree gate |
| Internal browser flow against local Streamlit port 8513 | pass | Desktop 1280×720 and mobile 390×844; cleanup completed |

Two diagnostic invocations did not represent product failures: one earlier pytest selection named a
nonexistent test path, and one ad-hoc mypy call supplied overlapping test module roots. Ruff also
reported one import-order issue introduced during the regex hardening; it was fixed, then all final
Ruff/mypy/test gates passed. The release auditor correctly warned that the precommit working tree
was dirty; this handoff does not claim clean-room release evidence.

## Automated test results

- Focused tests: **PASS** — 225 independent unit cases; P0=0 and P1=0 independent review.
- `make check`: **PASS** — two final passes, each 3,682 passed / 233 deselected; the first took
  725.36 seconds.
- Integration tests: **PASS** — 7 M33 PostgreSQL cases, including v12→v13 migration, exact
  snapshot/authority drift, tenant isolation, CAS, replay, append-only recovery and restart.
- Acceptance tests: **PASS** — 2 M33 application/presentation cases.
- Coverage, where applicable: not rerun for M33; `make check` is the milestone quality gate and no
  new coverage percentage is claimed.

## Operator manual test

1. Start the synthetic scenario:

   ```bash
   SCHEMABRIDGE_M33_SCENARIO_TOKEN=m33-manual-20260802 \
     .venv/bin/streamlit run scripts/m33_semantic_onboarding_scenario_app.py \
     --server.address 127.0.0.1 --server.port 8513 --server.headless true
   ```

2. Open `http://127.0.0.1:8513/` in the Codex internal browser.
3. Confirm `not_configured`, zero external writes, zero generated SQL and one exact retained
   3-field preflight.
4. Create the draft; confirm revision 1, model plus three mappings all `needs_review`, including
   confidence 1.00.
5. As the steward, approve model and all three mappings with `ticket:SEM-301` through
   `ticket:SEM-304`; confirm four append-only decisions and revision 5.
6. As a distinct publisher, confirm the exact fingerprint and prepare the future-worker handoff.
7. Check desktop and 390×844 layout, console, controls and audit; stop Streamlit with `Ctrl-C`.

Expected and observed result:

```text
status=ready_for_publication
revision=6
mappings=3
draft_fingerprint=ff6e79e393fd...
proposal=proposal-commerce-orders-onboarding-v1
proposal_version=1
proposal_fingerprint=9919ad0d95ca...
audit=draft_created + 4 decision_recorded + publication_prepared
external_writes_performed=false
generated_sql=0
product_publish_activate_execute_controls=0
console_errors_or_warnings=0
desktop_width=1280 client=1280 scroll=1280
mobile_width=390 client=390 scroll=390
```

The final product controls were only the Streamlit chrome and
`Descargar handoff JSON no ejecutable`. App script/font/image assets were served from
`127.0.0.1:8513`. The browser capability also reported its own
`webhooks.fivetran.com` transport, which has no repository reference; therefore this is not used as
evidence of a product-originated external call. No product adapter or external write was composed.

## Architecture and security review

- Dependency direction: domain remains I/O-free; application uses ports; PostgreSQL/catalog/HTTP
  code remains in adapters/entrypoints; `bootstrap.py` is the composition root.
- Source database writes: none. M33 reads catalog control-plane metadata only and never receives a
  source credential.
- SQL/LLM validation: M33 invokes neither. Historical `parse_date` payloads deserialize but cannot
  be authored or compiled; `TO_DATE` is not guard-allowlisted.
- DataHub mutation approval: M33 has no writer. The handoff is explicitly non-executable and M34
  must supply a dedicated queued writer/readback boundary.
- Secrets/proprietary data: synthetic data only; no new production dependency or secret added.
- Fanout/semantic risks: M33's first proposal has zero joins; generic join onboarding remains out
  of scope. Name similarity/confidence are evidence only and cannot approve.

## Decisions made

- Decision: accept M33 locally while keeping global production/release NO-GO.
- Reason: all bounded M33 criteria and final local gates pass, but publication/readback,
  production evaluation/security verification and an operated pilot do not exist yet.
- Logged in: D126 in `tasks/DECISION_LOG.md` and ADR 0016.

## Known limitations or unverified items

- PostgreSQL is the only SQL dialect; output remains for the same governed database/context.
- M33 accepts exact lowercase unquoted `schema.table.column` fields only. Nested, mixed-case and
  quoted identifiers are unsupported.
- `parse_date` is deliberately unsupported until total shape/calendar validation exists.
- One create body is capped at 64 KiB. Incremental/batch onboarding near the 2,000-mapping storage
  ceiling is not implemented.
- Inspection exposes a bounded recent window, not a cursor-based full audit export; operated
  per-tenant quotas/retention remain GA work.
- Identity-lineage behavior and PostgreSQL persistence are tested separately; a combined real
  PostgreSQL rotation-plus-M33 mutation test is a non-P0/P1 follow-up.
- Internal-browser evidence is local synthetic presentation evidence, not production UX, network,
  durability or SLO evidence.

## Blockers

- No blocker remains for bounded local M33 acceptance.
- Production/release remain blocked by M34 publication/readback and activation bridging, M30
  evaluation/independent security verification, M31 operated pilot, and the external provider,
  cluster, observability, recovery, legal/privacy and release-governance controls in the commercial
  plan.

## Next milestone readiness

- Dependencies satisfied: yes for M34 planning/implementation; M33 supplies the immutable scoped
  proposal and exact decision closure.
- Recommended next prompt: `prompts/M34_GENERIC_REGISTRY_PUBLICATION.md` after that milestone prompt
  and plan are written and reviewed.
- Required operator prerequisites: dedicated worker identity/queue, exact observed DataHub target
  allowlist, readback contract, failure-recovery semantics, separate activation approval and no
  writer credential in web/API.
