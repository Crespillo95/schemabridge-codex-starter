# M23: Durable control plane and schema migrations

- Status: complete; local operator acceptance passed, production release evidence pending
- Timebox: 18 hours
- Recommended Codex: GPT-5.6 Sol — Extra High (xhigh)
- Dependencies: accepted M20 identity boundary, M21 atomic registry, and M22 live immutable versions

## Objective

Replace the fixed-version, SQLite-only production boundary with an explicitly migrated PostgreSQL
control plane that governs which immutable DataHub registry version is active. Activation,
rollback, identity-key rotation, legacy-state import, publication reconciliation, and
backup/restore must be typed, approval-gated, replay-safe, scoped, and auditable.

The active registry pointer is authoritative in the control database and uses compare-and-swap.
DataHub continues to hold immutable registry versions and receives only a reconciled active-pointer
projection. Planning never trusts that projection and never falls back to a configured version or
recorded bundle in a managed profile.

## Scope assumptions

1. The control database is a separate PostgreSQL database and credential from every source
   database. A managed runtime must not start when the two endpoints identify the same database.
2. Schema changes are forward-only, checksum-pinned, transactional, and applied only by an explicit
   migrator credential. Web/API startup checks the exact schema version but never runs DDL.
3. The M22 v1 document accepted through the exact historical read shim remains read-only evidence
   and is not eligible for first production activation. M23 publishes and activates a strictly
   approved v2 before exercising rollback.
4. Rollback is a new monotonically increasing activation generation pointing to a previously
   activated immutable version. It never edits or deletes version documents or history.
5. DataHub has no compare-and-swap. Its active document is a repairable projection, not an
   authority; PostgreSQL pointer, transition, outbox, and audit event commit atomically.
6. Historical decision actors are immutable. Planned pseudonym-key rotation changes only
   authorization bindings through a verified old/new identity mapping; it never rewrites workflow
   decisions, publication records, or raw OIDC claims.
7. M23 removes preview rows from new durable workflow state. Existing legacy rows and orphan
   workflows are quarantined during import rather than silently adopted.

## Deliverables

- Add pure registry-control contracts for active pointers, activation/rollback proposals,
  approvals, transitions, projection state, reconciliation findings, and deterministic
  idempotency identities.
- Add application ports/use cases to prepare and commit a compare-and-swap transition, load the
  active registry, inspect/reconcile the DataHub projection, and fail closed on stale or conflicting
  state.
- Add a PostgreSQL control-plane migrator with advisory locking, ordered checksums, exact schema
  history, forward-version rejection, and separate runtime/reconciler/migrator capabilities.
- Add PostgreSQL stores for all durable managed-runtime state currently backed by SQLite:
  workflow drafts/access, publication audit, request drafts, canonical reviews, and join reviews.
  Fake demo publications/recipes remain explicit local-only adapters.
- Add atomic registry-pointer/history/outbox storage and an HMAC-chained append-only control audit.
  A transition conflict performs no DataHub I/O.
- Add a bounded DataHub active-pointer projector and reconciler. Retries verify exact read-back;
  superseded outbox records never overwrite a newer generation.
- Add an active-registry adapter that resolves the PostgreSQL pointer then reads the exact immutable
  DataHub version and verifies version, fingerprint, source, scope, and generation before returning
  it through the existing registry port.
- Add an offline, dry-run-first legacy SQLite importer. It validates the complete known schema and
  typed payloads, strips durable preview rows while retaining counts/fingerprints, and quarantines
  orphan or ambiguous resources.
- Add verified identity provenance/policy versions and a dual-key rotation workflow that persists
  only opaque bindings. Collision, cycle, cross-workspace, incomplete-owner, or stale-plan cases
  fail closed.
- Add governed recipe-provenance migration: a stale recipe must be re-planned, guarded, executed,
  and published as a new version from a completed current workflow; historical recipes are never
  edited or fingerprint-copied.
- Add backup/status/restore operator commands for the PostgreSQL control plane. Backups are
  transaction-consistent, checksum-manifested, owner-only local artifacts intended for encrypted
  platform storage; restore targets a fresh database and verifies migrations, audit chain, pointer,
  history, outbox, and quarantines before cutover.
- Add typed configuration, bootstrap composition, CLI/operator commands, authenticated UI
  visibility, service checks, tests, runbooks, ADR, and browser acceptance.

## Implementation sequence

1. Define domain contracts, error codes, ports, and adversarial unit tests.
2. Add the migration runner and initial PostgreSQL schema; prove role separation and replay.
3. Implement the transactional pointer/history/outbox/HMAC-audit store and CAS tests.
4. Implement exact-version loading, activation/rollback use cases, and active runtime composition.
5. Implement DataHub projection and read-only reconciliation inspection/approved repair.
6. Move managed workflow/access/audit and review/request persistence to PostgreSQL.
7. Add legacy import/quarantine, result-row minimization, identity rotation, and recipe migration.
8. Add backup/restore/status tooling and failure drills.
9. Publish strict v2, activate, reconcile, execute, roll back, and reactivate using local services.
10. Run focused/full/service/coverage/release/diff gates and complete internal-browser acceptance.
11. Update architecture, security, deployment, runbook, UI, state, decisions, ADR, and handoff.

## Acceptance criteria

- [x] Staging/production require a separate PostgreSQL control DSN, exact current schema, active
      pointer, and active registry mode; they never auto-migrate or use SQLite/fixed-version fallback.
- [x] Empty-to-current migration, exact replay, concurrent migrators, checksum drift, future
      version, partial/incompatible legacy schema, and interrupted migration behave deterministically
      and fail with sanitized errors.
- [x] Runtime, reconciler, and migrator roles have exact capabilities; runtime cannot execute DDL,
      reconciler cannot read source data, and no control credential can modify a source database.
- [x] Activation validates a strictly approved immutable DataHub version and binds scope, expected
      generation/fingerprint, target version/fingerprint/URN, semantic decisions, actor/time, action,
      confirmation, and deterministic approval ID.
- [x] Two concurrent activations produce exactly one winner. A stale approval performs zero pointer,
      audit, outbox, or DataHub mutation.
- [x] Pointer, immutable transition, HMAC audit event, and outbox reservation commit in one
      PostgreSQL transaction and survive a fresh process.
- [x] Active loading reads the pointer and exact DataHub version, rejects missing/corrupt/mismatched
      state, and never consults the recorded registry or configured version in managed mode.
- [x] Reconciliation classifies in-sync, missing, behind, ahead, conflicting, superseded,
      version-corrupt, audit-gap, and pending-outbox states. Only an exact approved safe repair
      mutates DataHub; read-back is mandatory.
- [x] A failed projection leaves the committed registry active with a visible pending outbox. Retry
      observes exact prior success or repairs it; an older generation never overwrites a newer one.
- [x] Rollback creates a higher generation to a previously active, still-valid immutable version.
      Unknown, legacy-shim-only, removed, corrupt, or never-active targets are rejected.
- [x] Managed workflow/access/publication/review/request state uses PostgreSQL and preserves tenant
      isolation, optimistic concurrency, append-only decisions, and sanitized failures.
- [x] Legacy import is dry-run-first and idempotent, records source/checksums/counts, removes preview
      rows from durable state, and quarantines every orphan/ambiguous/invalid resource without
      adopting ownership.
- [x] Identity rotation is derived from the same verified OIDC identity under dual keys, preserves
      historical payload bytes, and rejects collisions, cycles, cross-workspace mappings, stale
      approvals, or completion while an owner lacks a new binding.
- [x] Recipe migration creates a new approved version only after current-registry planning,
      independent SQL guarding, bounded read-only execution, exact-result/rejection validation, and
      re-publication; the historical version remains byte-for-byte unchanged.
- [x] Backup/restore to a fresh database preserves schema history, active pointer, transition
      history, pending outbox, quarantine state, identity bindings, and the verified audit-chain
      head. Altered archive/manifest or wrong target fails before cutover.
- [x] A workflow confirmed before activation fails with `stale_registry`; a new workflow uses the
      new generation and retains the three-table/two-join, row, and timeout bounds.
- [x] Internal-browser acceptance visibly proves active generation/version/fingerprint, pending then
      reconciled projection, exact north-star execution, stale-workflow blocking, rollback as a new
      generation, clean console, and no narrow-viewport overflow.

## Required automated checks

```bash
pytest tests/unit/test_registry_control.py \
  tests/unit/test_control_plane_migrations.py \
  tests/unit/test_control_plane_audit.py \
  tests/unit/test_identity_rotation.py \
  tests/unit/test_legacy_control_plane_import.py
make control-plane-reset
make control-plane-migrate
make control-plane-check
make test-integration
make test-acceptance
make evaluate
make check
make coverage
python scripts/release_audit.py
git diff --check
```

## Manual test for the operator

1. Reset/migrate the separate local control PostgreSQL and verify runtime/reconciler/migrator grants.
2. Publish strict registry v2, prepare its exact activation, and activate generation 1.
3. Stop or fault the DataHub projector, activate the next generation, and confirm the application
   remains bound to PostgreSQL while the outbox is visibly pending.
4. Restore the projector, reconcile, and verify exact DataHub read-back and a closed audit event.
5. Open a workflow confirmed before the transition and verify `stale_registry`; start a new one and
   verify exact `2, 1, 1` plus the three rejection classes.
6. Roll back to a previously active version and confirm generation increases rather than history
   being rewritten.
7. Produce a backup, restore it to a fresh control database, and compare the verified state digest.
8. Inspect authenticated Streamlit pages, console, logs, and a 390x844 viewport for leakage,
   stack traces, or document overflow.

## Completion record

- Migration schema version `1`, checksum
  `65d9447619165a92e9b8d8c6541a76569564931b6dc9c7ccbd73deb0f3fd09cc`; all three
  bounded credentials reported current version 1 and source/control separation passed.
- Final authoritative registry: generation 3, version 6, transition
  `registry-transition-v1-175299e04d6f9179733792f3d3c8b724807953533be5b2b8ca5b1ec881e7b187`,
  pointer fingerprint
  `85f131dd53bb5cf2da8538cc171277c4d988b77101e0e5b37da46fed2ef24757`, projection
  `delivered`.
- Exact reconciliation repair report
  `cad8b53d617250342765d2cdfe5881fc13b97735b3658265a64d256521c9060a` was
  approval-gated and read back; the following inspection was `in_sync`.
- The isolated real-CLI legacy drill imported 2 of 6 resources, quarantined 4 with four distinct
  reason codes, stripped 3 preview rows, preserved the source hash, and replayed without new rows.
- The isolated real-CLI identity drill rotated one complete v1→v2 lineage, verified two bindings,
  preserved historical bytes/access, validated a three-event audit chain, and replayed the same
  completion. The replay regression resolves the immutable approved plan by exact fingerprint and
  verified derivations before completion; it never re-prepares against a historical workspace.
- Governed live recipe migration published version 44 from the current workflow while historical
  version 43 retained its exact fingerprint.
- Backup and fresh-target restore verified state SHA-256
  `22b8fdc5568b81e2bdd8626b3315e525c16651f19d3966c538b87cc7948ec2e5`;
  the temporary target and owner-only artifacts were removed after verification.
- Final gates: `make check` 767 passed; integration 58; acceptance 15; coverage 840 at 81.03%;
  deterministic evaluation passed the 11-table/465-row corpus; release audit and diff hygiene
  passed with only the expected dirty-tree warning.
- The internal browser showed generation 3/version 6, projection `delivered`, exact 7/31/5 counts,
  a clean console, and no 390x844 horizontal overflow. A final post-gate reload read the same
  pointer and registry facts.

## Explicit non-goals

- Public HTTP APIs, queues, leases, worker autoscaling, quotas, or webhooks; M24 owns them.
- Production-scale indexing, pagination, load/SLO measurements, or multi-region failover; M25 owns
  them.
- Automatic schema/join drift detection and blast-radius reapproval; M26 owns them.
- Registry-wide free-text/short-description field matching or dynamic Query Studio controls; M27
  owns them.
- New SQL dialects/connectors or database cost estimation; M28 owns them.
- Remote backup retention, SIEM, full SBOM/provenance, and operated disaster-recovery schedules;
  M29 extends the functional M23 baseline.
- A release/GA claim; M30/M31 and the blocked clean-commit/public deployment evidence remain open.

## Handoff requirements

Update `tasks/PROJECT_STATE.md`, `tasks/CURRENT_TASK.md`, `tasks/DECISION_LOG.md`, the M23 ADR,
architecture/security/deployment/test/runbook/UI documents, and examples. Return
`tasks/HANDOFF_TEMPLATE.md` with exact migration versions/checksums, database role evidence,
activation generations/fingerprints, reconciliation/rollback/backup evidence, browser results,
limitations, and one proposed commit message.
