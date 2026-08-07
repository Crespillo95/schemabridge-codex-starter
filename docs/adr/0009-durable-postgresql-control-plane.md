# ADR 0009: Durable PostgreSQL registry control plane

- Status: accepted; M23 local operator acceptance verified, production release pending
- Date: 2026-07-23

## Context

M22 stores approved semantic-registry versions as immutable DataHub documents, but selecting a
configured version is not a safe production activation mechanism. DataHub does not expose the
compare-and-swap operation needed to serialize concurrent activations, and it cannot atomically
commit an active pointer together with SchemaBridge's transition and audit records.

Production also needs durable workflow/review/request state, explicit schema migrations,
rollback, reconciliation, identity-key rotation, controlled legacy-state adoption, and verifiable
backup/restore. SQLite remains useful for the public recorded demo, but it is not the managed
runtime authority.

## Decision

1. A separate PostgreSQL database is the authoritative managed control plane. The fixed schema is
   `schemabridge_control`; forward-only migrations are checksum-pinned, transactional, serialized
   with an advisory lock, and run only by an explicit migrator command.
2. Runtime, reconciler, and migrator use distinct PostgreSQL roles and credentials. Application
   startup performs an exact read-only schema check and never runs DDL. Control and source
   databases must identify different endpoints. In addition to URL preflight, a read-only probe
   compares PostgreSQL-observed `inet_server_addr`, `inet_server_port`, and
   `current_database`, so DNS/hostname aliases cannot bypass separation. Managed operator
   mutations take their pseudonymous actor and closed roles from trusted deployment configuration
   and reject caller-selected argv identity.
3. One workspace-scoped active pointer is updated with compare-and-swap. The pointer, immutable
   activation/rollback transition, pending projection outbox record, and HMAC-chained control
   audit event commit in the same PostgreSQL transaction.
4. Rollback never rewrites history. It creates a higher activation generation that references a
   previously active, still-valid immutable registry version.
5. DataHub remains the home of immutable governed registry versions. Its active-pointer document
   is a repairable projection only; planning resolves the PostgreSQL pointer and then reads the
   exact immutable version. It never trusts the projection and never falls back to a configured
   version or recorded bundle in a managed profile.
6. Reconciliation is inspect-first and approval-gated. Only an unchanged, safely repairable report
   may update the projection, and exact post-write read-back is mandatory. Ahead, conflicting,
   corrupt, audit-gap, and superseded states fail closed.
7. Managed workflow drafts/access, analytical requests, canonical reviews, join reviews, and
   publication audit state use workspace-scoped PostgreSQL stores. Fake publication and recipe
   stores remain local-demo adapters.
8. Legacy SQLite adoption is offline, dry-run-first, exact-schema-validated, fingerprint-bound,
   idempotent, and quarantine-preserving. Preview rows are not copied into new durable workflow
   state. Exact rows may exist only in a transient authenticated-session envelope bound to actor,
   workspace, workflow, revision, registry fingerprint, activation generation, active-pointer
   fingerprint, and recomputed preview fingerprint; mismatch purges it. Identity-key rotation
   accepts only a short-lived owner-only signed envelope of verified dual-key derivations and
   changes only opaque authorization bindings; historical decision bytes are immutable. Current
   principals resolve historical workflows only through bounded verified same-lineage aliases,
   never row rewrites. Stale recipes require a new current-registry workflow and a new approved
   version.
9. Backups use one exported repeatable-read snapshot, a custom-format `pg_dump`, exact schema and
   state digests, owner-only artifacts, and an HMAC-signed manifest. Restore accepts its target
   only from secret configuration, requires a distinct fresh database, and verifies migration
   history, complete state digest, audit chains, pointer/history/outbox, and quarantine counts
   before any cutover.

## Alternatives rejected

- Treat the DataHub active document as authoritative: it has no cross-system transaction or
  compare-and-swap.
- Auto-migrate from web/API startup: this grants DDL capability to a long-running data-facing
  process and makes rollout failures ambiguous.
- Roll back by editing a pointer or immutable version in place: this destroys monotonic history
  and approval evidence.
- Dual-write PostgreSQL and DataHub without an outbox: a partial failure cannot be retried or
  reconciled deterministically.
- Use name similarity to adopt legacy state or rotate owners: similarity is not authorization or
  semantic evidence.
- Keep SQLite as the staging/production authority: it does not provide the required role
  separation, concurrent compare-and-swap, or managed backup boundary.

## Consequences

- An activation can succeed while its DataHub projection is pending; the registry remains active
  because PostgreSQL is authoritative, and the pending outbox is visible for explicit repair.
- Deployments must provision and rotate separate source, runtime, reconciler, migrator, restore,
  DataHub reader/writer, audit-signing, identity-migration, and pseudonymization credentials.
- Operators must migrate before rollout and prove backup/restore against a fresh database.
- The public hosted demo remains recorded/local and does not claim this managed control plane.
- The local M23 service, fresh-restore, and internal-browser acceptance has been recorded. This
  accepts the architecture and closes the local milestone evidence; it does not make the current
  dirty workspace or synthetic service a production release.
- Production still requires environment-specific TLS/secrets, encrypted remote retention and
  scheduled recovery, monitoring/SIEM, HA/failover, a clean reviewed release artifact, and the
  later API/worker/concurrency/quota controls.

## Verification

The 2026-07-23 operated record verified schema version 1 checksum
`65d9447619165a92e9b8d8c6541a76569564931b6dc9c7ccbd73deb0f3fd09cc` and distinct
runtime/reconciler/migrator roles. It activated version 6 at generation 1, version 7 at generation
2, preserved pending projection state until exact approved reconciliation, and rolled back to
version 6 as generation 3 before reconciling that projection to `delivered`.

Legacy import accepted exactly 2 resources, quarantined 4, skipped 0, and stripped 3 preview rows.
Identity v1→v2 rotation covered 2 bindings, was replay-safe, and rewrote no historical payload.
Live recipe migration created DataHub version 44 from version 43 while exposing neither SQL nor
preview rows.

A signed owner-only backup restored into a distinct fresh database with identical state SHA-256
`22b8fdc5568b81e2bdd8626b3315e525c16651f19d3966c538b87cc7948ec2e5`. The fresh
browser showed generation 3/version 6, projection `delivered`, 7/31/5 registry counts, console
`[]`, and no horizontal overflow at 390x844. The final post-fix `make check` passed 767 no-service
tests plus Ruff, formatting, and strict mypy over 160 source files.
