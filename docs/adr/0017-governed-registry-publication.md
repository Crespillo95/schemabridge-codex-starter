# ADR 0017: Observed physical authority and a dedicated publication worker

- Status: accepted for M34 implementation
- Date: 2026-08-03

## Context

M33 produces an immutable proposal only after tenant-bound semantic review, but it is a one-model
delta rather than a complete runtime registry. The existing M22 publisher is safe for its recorded
fixture and exact immutable target, yet it derives DataHub dataset URNs from `schema.table` names.
That derivation is not catalog authority and cannot distinguish connections or tenant assets.
Web/API also intentionally lacks a DataHub writer credential, while the M24 execution worker is a
different trust domain.

The existing registry v1 drops the M33 connection, catalog generation, locator, observed URN and
metadata fingerprints. It also cannot represent truthful empty join provenance. Publishing M33
through the old adapter would therefore discard authority, invent related assets and couple a web
request to an external mutation.

## Decision

M34 introduces registry format v2. Every active mapping is covered by a frozen physical binding
that preserves its exact workspace, connection, retained catalog generation/vector, asset and
field locator, observed DataHub dataset URN and metadata fingerprints. The v2 registry fingerprint
covers those bindings. DataHub `relatedAssets` is the sorted unique projection of observed URNs
only.

A pure assembler consumes an exact M33 proposal and either an explicit empty base or a strict v2
base. The initial vertical adds one new model and its mappings. It rejects replacement, removal,
new joins, v1 bases, cross-connection merges, collisions, stale base/catalog facts and every
missing observed URN. Review decisions for rejected alternatives remain in M33 audit but never
enter active registry provenance. Empty join provenance is permitted only for an empty join set.

Publication is a two-phase durable job:

1. an authenticated publisher/admin submits and reserves the exact target;
2. a dedicated publisher process revalidates and assembles the complete candidate;
3. a fresh authenticated publisher approves that exact candidate and closed confirmation;
4. the dedicated process revalidates, writes the immutable DataHub document and reads it back;
5. the terminal handoff is `activation_ready`, with the approval actually observed at the target.

The queue uses database time, hashed lease capabilities, heartbeat, fencing, bounded retry,
cooperative cancellation and append-only events. A post-write transport failure is not called
failed or cancelled until exact read-back resolves the immutable target.

The publisher process and PostgreSQL role are distinct from web, API, execution worker, catalog,
reconciler and runtime activation identities. Its remote secret document is exact-version,
operation-scoped and contains only DataHub server, token and expected actor. It cannot update the
active pointer. API can reserve and approve queue state but cannot read the secret or mutate
DataHub.

Publication never invokes activation. M23 remains authoritative for a second prepare/approve/CAS
transition, rollback, projection outbox and reconciliation. The activation bridge accepts only a
strict v2 read-back receipt and atomically rechecks the retained catalog authority under the same
workspace lock before committing the pointer.

## Consequences

- A tenant can publish its first zero-join registry and later add non-colliding models without
  repository fixtures.
- Physical authority remains auditable through publication and can no longer be reconstructed from
  names.
- Existing v1 fixtures remain readable for compatibility, but generic merge or activation-ready
  publication requires an explicit reviewed migration to v2.
- A publisher must perform an extra confirmation after assembly; M33 preparation alone is not
  authorization to mutate DataHub.
- The 256-related-asset DataHub document limit is a deliberate v2 publication bound even though a
  registry may contain more field mappings. Exceeding it fails before write until a versioned
  chunking design exists.
- M34 completion removes one local productionization blocker but does not prove semantic accuracy,
  production security, SLOs or commercial readiness.

## Rejected alternatives

1. Reuse name-derived M22 related assets: rejected because names are not authority.
2. Put the writer token in API/web: rejected because request handling is not the mutation trust
   boundary.
3. Reuse the M24 execution worker: rejected because source execution and catalog publication must
   not share capabilities.
4. Treat M33 preparation as final approval: rejected because the full v2 snapshot/fingerprint does
   not yet exist.
5. Auto-activate after read-back: rejected because publication and runtime selection require
   separate approvals and recovery paths.
6. Merge a v1 base by guessing bindings: rejected because missing authority cannot be repaired by
   derivation.
