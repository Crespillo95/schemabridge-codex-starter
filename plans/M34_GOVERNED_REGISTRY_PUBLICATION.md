# M34 — Governed registry publication and activation handoff

## Status

- State: complete; accepted locally
- Production/release gate: **NO-GO**
- Depends on: accepted local M22, M23, M24, M29 and M33
- Followed by: M30 production evaluation/security certification

## Objective

Turn one exact M33 `ready_for_publication` proposal into an immutable DataHub semantic-registry
version without giving the web/API process a DataHub writer credential. The final version must
retain every physical authority fact observed during onboarding, be explicitly approved after its
complete snapshot is known, survive retries and partial failure, pass exact independent read-back,
and finish as `activation_ready`. Activation, rollback and reconciliation remain the separate M23
control-plane workflow.

M34 does not make SchemaBridge commercially releasable. It removes the generic-publication
blocker; M30 evaluation/security evidence, M31 operated pilot evidence and external legal/SRE
controls remain mandatory.

## Security decisions

1. DataHub dataset URNs are copied only from exact retained catalog observations. No display name,
   `schema.table`, platform default or similarity rule may create an authority identity.
2. A format-v2 registry carries a complete fingerprinted physical binding for every active
   mapping: workspace, connection, generation/vector, locator, observed URN and metadata
   fingerprints.
3. Format v1 remains readable for the already accepted compatibility fixtures but is never a
   valid base for generic M34 merge or activation-ready publication.
4. The API may reserve a target, inspect, approve and cancel a tenant-scoped job. It has no
   DataHub token and no registry-pointer write privilege.
5. A dedicated publisher process has only its queue capability and an operation-scoped DataHub
   writer/reader secret. It has no source, LLM, preview, connector-execution or activation
   capability.
6. M33 preparation is not publication authorization. A fresh publisher explicitly confirms the
   complete candidate fingerprint, target, binding and decision closure after deterministic
   assembly.
7. External success is never inferred from a transport return. The exact document, approval,
   audit and related assets must be read back; ambiguous failure remains retry/readback-required.
8. Publication never activates. M23 loads the strict immutable version and performs a separately
   approved CAS transition, rollback and reconciliation.

## Supported authoring vertical

- One new logical model per M33 proposal.
- One or more approved mappings covering all new fields.
- Initial empty base or additive merge onto an exact active v2 base.
- Existing logical models, mappings and join contracts are preserved byte-for-byte in meaning.
- New joins, field/model replacement, removal and cross-connection registries are rejected until
  a later typed change contract exists.
- First registry with zero joins is valid and carries an empty join-provenance decision set.

## Durable lifecycle

```text
queued -> leased(preparing) -> awaiting_approval -> approved
         approved -> leased(publishing) -> activation_ready
         leased -> retry_wait -> leased
         waiting/leased -> cancel_requested -> cancelled
         leased -> failed | dead_lettered
```

- Submission atomically reserves `(workspace, catalog_scope, registry_id, target_version)` and is
  exactly idempotent.
- Claims use database time, a hashed lease capability and monotonically increasing fencing token.
- Preparation revalidates the exact proposal, active base and retained catalog authority before
  storing a candidate.
- Approval binds the candidate, registry/binding fingerprints, target, actor, recent session and
  closed confirmation.
- Publication revalidates all authority immediately before the first external write.
- A crash after upsert is recovered by reading the same immutable target. Different content is a
  terminal conflict; exact content records the approval actually observed in DataHub.
- Cancellation is cooperative. Once an external write may have occurred, read-back wins over a
  cancellation label.

## Acceptance criteria

### Registry v2 and assembly

1. An empty base plus one approved M33 model creates a complete v2 registry with zero joins.
2. Every active mapping has exactly one physical binding; every related asset is an observed URN.
3. Missing, forged, duplicated or cross-workspace/connection binding fails before DataHub I/O.
4. Additive merge preserves the exact v2 base, appends only a new model/mappings and keeps joins.
5. Base drift, catalog drift, v1 base, identifier collision, physical-meaning collision, rejected
   decision leakage or any registry bound fails closed.
6. Join provenance may be empty only when the join registry is empty; no synthetic decision is
   invented.

### Approval and queue

7. Only authenticated publisher/admin principals with a fresh session can submit/approve.
8. The approval is created only after the complete candidate exists and binds every immutable
   fingerprint and the exact target.
9. A target version has one durable reservation; concurrent proposals cannot both reach DataHub.
10. Exact retries are idempotent and altered retries conflict.
11. Claim, heartbeat, expiry, retry, cancellation, fencing and dead-letter transitions reject stale
    workers and survive process restart.
12. Public inspection is tenant-bound, bounded and contains no token, DSN, SQL or source value.

### DataHub and activation boundary

13. The writer identity and granted privileges equal the configured bounded contract.
14. The v2 document has a closed property set, exact snapshot/approval/audit/binding and exact
    observed `relatedAssets` read-back.
15. Existing exact content becomes `activation_ready` with the approval observed in the document;
    different content dead-letters without overwrite.
16. API/web/normal worker cannot obtain the writer secret; publisher cannot update active pointers.
17. Successful publication leaves the active pointer unchanged.
18. A separate M23 prepare/approve/commit can activate the strict v2 version; rollback and
    reconciliation remain functional.

### Evidence and release truth

19. Unit, PostgreSQL integration, DataHub adapter and end-to-end acceptance tests cover every
    criterion above, including a deliberately non-conventional URN.
20. Internal-browser/operator evidence shows queued → approval → activation-ready and no automatic
    activation.
21. `make check` passes and state/decision/current-task/handoff documentation matches the bytes.
22. Completion changes M34 only; global production/release remains **NO-GO** pending M30/M31 and
    external operated controls.

## Required test matrix

- `tests/unit/test_registry_publication_v2.py`
- `tests/unit/test_registry_publication_jobs.py`
- `tests/unit/test_registry_publication_worker.py`
- `tests/unit/test_datahub_semantic_registry_v2.py`
- `tests/unit/test_registry_publication_http.py`
- `tests/unit/test_registry_publication_schema_migration.py`
- `tests/integration/test_registry_publication_postgres.py`
- `tests/integration/test_datahub_semantic_registry_integration.py`
- `tests/acceptance/test_m34_registry_publication.py`
- M23 activation/rollback/reconciliation regression suites

## Implementation order

1. Format-v2 physical authority and zero-join contracts.
2. Pure additive assembler and candidate/approval contracts.
3. DataHub v2 writer and strict reader compatibility.
4. Queue domain, ports, PostgreSQL v14 migration and fenced store.
5. Authenticated API transitions and isolated publisher runtime/secret resolver.
6. M23 activation-ready handoff and atomic catalog-authority guard.
7. Focused, integration, acceptance and internal-browser tests.
8. Full gate, independent review, durable state and handoff.

## Manual operator test

1. Create and approve an M33 proposal whose catalog asset has a non-derived DataHub URN.
2. Submit it and verify the target is reserved but DataHub and the active pointer are unchanged.
3. Run one publisher preparation iteration and inspect the complete v2 candidate.
4. Approve the exact candidate as a fresh publisher and run publication.
5. Verify exact read-back, `activation_ready`, immutable audit and unchanged active pointer.
6. Use the separate M23 operator flow to prepare and approve activation; commit the CAS.
7. Reconcile the projection, activate another test version, then rollback to the first transition.
8. Repeat with a stale catalog, missing URN, competing target and killed worker; verify zero unsafe
   mutation and deterministic recovery.
