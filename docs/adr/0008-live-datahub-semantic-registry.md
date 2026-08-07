# ADR 0008: Immutable live DataHub semantic-registry versions

- Status: accepted for M22
- Date: 2026-07-23

## Context

M21 established one atomic `GovernedSemanticRegistrySnapshot`, but its active implementation was a
checked-in synthetic bundle. Selecting live DataHub still could not reconstruct the definitions,
mappings, transformations, contracts, decisions, evidence, risks, versions, scope, and provenance
needed by guided requests and deterministic planning.

Loading those artifacts independently would reintroduce version drift. Falling back to the
recorded bundle after a missing, partial, corrupt, unauthorized, or unavailable DataHub read would
also mislabel synthetic evidence as current governed state. M22 therefore needs one bounded,
workspace-scoped live object behind the existing registry port.

## Decision

1. DataHub stores each approved registry revision as one immutable version document. Its
   deterministic ID is
   `schemabridge-semantic-registry-{registry_id}-v{version}-w{workspace_sha256_prefix}`. The
   24-character workspace suffix is derived from the opaque workspace ID; that ID is not
   interpolated into the URN or exposed as a standalone index property. The exact opaque workspace
   binding remains inside the approval envelope and is validated on read-back.
2. The document contains one typed registry snapshot plus the exact publication approval, one
   successful per-target audit record, the complete ordered semantic-decision closure, and links to
   the exact governed physical assets. It contains no SQL, result rows, samples, prompts,
   credentials, tokens, or private identity claims.
3. Preparing the document changes the registry, logical-context, and provenance source labels to
   the immutable `datahub:` target before calculating the canonical fingerprint. For the current
   local-demo workspace, `synthetic_enterprise` version 1 has source
   `datahub:schemabridge-semantic-registry-synthetic_enterprise-v1-w3f92b9304d7d1f8f0cbf410c`
   and fingerprint
   `ef480eb7370924ff4c94131a2c6c4063d85652cc83aaec9059538cdc2cf9c1b9`.
4. Publication requires the exact prepared fingerprint, the closed
   `publish-approved-registry-version` confirmation, actor/time, workspace, registry identity,
   catalog scope, target, and all 37 semantic decision IDs. The application validates the returned
   result and audit before appending it to the common SQLite publication ledger.
5. The writer checks an existing immutable target before mutation, rejects different content,
   writes through the bounded SDK adapter, and requires exact post-write read-back. An identical
   target is `already_current`; it is not rewritten.
6. The live reader has a separate protocol and concrete HTTP client with no upsert method. It reads
   only the configured exact URN, rejects redirects, uses bounded response/property sizes and a
   15-second timeout, rejects duplicate JSON keys, and validates the complete typed snapshot,
   scope, version, fingerprint, approval, audit, decision closure, document status, and related
   assets. Search and recorded-manifest fallback are not part of this path.
7. The reader credential must have no target edit grant. The stock local DataHub policy may still
   expose `generatePersonalAccessTokens`; M22 tolerates that observed platform privilege but never
   calls it, and independently requires every checked document-edit capability and exact-target
   mutation grant to be absent.
8. Registry mode and version are deployment configuration. `hosted-demo` remains recorded;
   staging and production require both a live catalog and live registry. Local-demo live reads are
   restricted to development with `SCHEMABRIDGE_ALLOW_LOCAL_LIVE_READS=true`.
9. The operator flow is explicit: `registry-prepare` performs no write, `registry-publish` requires
   the exact approval values, `registry-show` uses only the selected adapter, and
   `make datahub-registry-check` verifies the live 7/31/5 registry and 37-decision closure.

## Verified synthetic document shape

The local synthetic version contains:

```text
registry ID / version: synthetic_enterprise / 1
catalog scope: synthetic-demo
logical models / mappings / joins: 7 / 31 / 5
semantic decisions: 37
related physical assets: 7
live fingerprint: ef480eb7370924ff4c94131a2c6c4063d85652cc83aaec9059538cdc2cf9c1b9
```

The seven assets are the approved `crm.customers`, `bank.account_holders`, `bank.accounts`,
`commerce.products`, `sales.orders`, `sales.order_lines`, and `fulfillment.shipments` datasets.
This is synthetic integration evidence, not production-scale validation.

## Alternatives rejected

- **Reconstruct a snapshot with DataHub search:** search results and pagination cannot prove one
  exact immutable revision.
- **Keep the recorded fingerprint after changing provenance:** source and provenance are part of
  the canonical snapshot and must be integrity-protected.
- **Give the reader the writer client:** capability separation should exist by construction, not
  only by convention.
- **Fall back to the manifest on a live error:** this would conceal missing or stale governance.
- **Use a mutable current document in M22:** activation, rollback, and migration require the
  reconciliation controls assigned to M23.

## Consequences and residual work

Live guided requests, planning, guarded execution, evaluation composition, and UI composition can
now consume the same atomic port from DataHub. The local document proves 7 models, 31 mappings, 5
contracts, 37 decision references, and 7 related assets without manifest access.

DataHub has no compare-and-swap for this write, and DataHub plus the SQLite audit ledger do not
provide a distributed transaction. Publication remains serialized/single-writer; active pointers,
registry migrations, rollback, key rotation, backup/restore, and reconciliation remain M23.

The checked-in query-recipe fixture still identifies the recorded M21 source and fingerprint. A
live-registry evaluation therefore reports that recipe as stale rather than pretending it is
reusable; regenerating and governing live recipe provenance belongs with M23 change management.

Names and brief field descriptions remain evidence only. Registry-wide free-text matching,
ambiguity handling, and dynamic Query Studio controls remain M27. M22 internal-browser acceptance
verified `live:datahub`, the exact immutable registry identity, guarded `2, 1, 1` execution, three
typed rejection classes, an empty warning/error console, and no horizontal overflow at 390x844.
