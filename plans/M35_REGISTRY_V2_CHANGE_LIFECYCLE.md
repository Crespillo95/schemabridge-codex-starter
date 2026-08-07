# M35 — Governed registry-v2 change lifecycle

## Status

- State: complete; accepted locally under D129
- Production/release gate: **NO-GO**
- Depends on: accepted local M23, M25, M26, M29, M33 and M34 contracts
- Followed by: M30 production evaluation/security verification

## Objective

Let an authenticated tenant evolve one exact active registry-v2 without editing repository
fixtures or mutating an existing DataHub document. M35 adds two bounded change families:

1. approve and publish a new join between already active logical models and exact mappings; and
2. replace/remediate one active logical model and its mappings, including explicit updates or
   removals for every affected join.

Every change starts from an immutable active v2 base, carries complete impact and authority
evidence, requires explicit semantic decisions, is assembled into a new complete version, and
uses the M34 queue, isolated writer, exact read-back and separate M23 activation flow.

M35 does not make SchemaBridge commercially releasable. M29 operated controls, M30 quality and
security certification, M31 pilot evidence, legal/service controls and every external release
gate remain mandatory.

## Why this milestone precedes M30

M34 deliberately supports only an initial zero-join registry or an additive new model. A new
tenant can therefore publish independent models but cannot create the approved relationships
needed for multi-table requests, and an M26 blocking drift report cannot be remediated through the
v2 publication path. Certifying that incomplete lifecycle would exclude the core commercial use
case of changing customer tables and relationships.

M30 campaign execution also remains blocked by M29's unchecked operated criteria. Provider-free
M30 contract work may be prepared after M35, but no live campaign or production claim may start
until its preconditions have executed evidence.

## Non-negotiable security decisions

1. Existing registry versions are immutable. Every accepted change creates exactly the next v2
   version and never edits the active document or pointer in place.
2. A join candidate is not authority. Equal names, matching definitions, similarity or high
   confidence never approve a relationship.
3. New joins use only exact active logical fields, exact active physical mappings and their
   transformation plans. Both endpoints must resolve to the same governed connection.
4. Relationship evidence is aggregate-only and reuses the M26 read-only, allowlisted, timed
   profiling boundary. No key, row, sample, SQL, parameter, DSN or credential is retained.
5. Many-to-many joins remain non-executable. One-to-many joins require the existing explicit
   fanout mitigation and risk.
6. A replacement/remediation is bound to the exact active base and complete M26 impact set. A
   blocking finding cannot be waived against the old version.
7. Every active field after replacement has an approved mapping and exact v2 physical binding.
   No physical coordinate may acquire two active logical meanings.
8. Every incident join is preserved exactly, explicitly replaced, or explicitly removed. A stale
   join key cannot survive a mapping or model change.
9. Steward decisions and publisher authorization are separate. Managed publication keeps the M34
   post-assembly approval and the writer credential outside web/API.
10. Publication never activates. M23 performs a second approved compare-and-swap and M26 inspects
    the new evidence before affected execution can become current.

## Bounded supported changes

### Phase A — add one join

- One join per change draft between two different active models.
- Both logical/physical keys and transformations must match active registry mappings exactly.
- One current aggregate relationship observation bound to workspace, connection, catalog vector,
  active registry fingerprint and proposal fingerprint.
- One explicit steward decision over cardinality, join type, fanout policy, evidence and risks.
- Join id and approval decision id must be new; replacement through this phase is rejected.

### Phase B — replace or remediate one model

- One existing model id, with the next exact model/field version.
- Complete replacement mapping set and physical bindings for every retained/new field.
- Optional explicit join upserts/removals, but every join incident to the model must be accounted
  for and every upsert receives a fresh relationship observation and decision.
- `planned_change` requires a complete current dependency snapshot.
- `m26_remediation` additionally requires one current blocking M26 report and names every resolved
  finding. A compatible-revalidation report cannot authorize structural replacement.
- Removing a model, bulk multi-model mutation, quoted/mixed-case identifiers and cross-connection
  changes remain unsupported until separate typed contracts exist.

## Durable lifecycle

M35 authoring uses tenant-bound change drafts with append-only decisions and exact idempotency.
Phase A join authoring retains the historical terminal supersession state:

```text
needs_review -> ready_for_publication -> superseded
```

Phase B replacement/remediation has an explicit outer steward decision before publication:

```text
needs_review -> approved -> ready_for_publication
             \-> rejected
```

One immutable M33 replacement-source proposal may authorize at most one Phase B outer draft. A
rejected draft, authority drift, or stale base is not edited or rebound: the operator must create
and approve a new immutable M33 proposal, which receives a new source identity. This one-shot
source rule is deliberate fail-closed provenance, not an implicit retry or a `superseded` Phase B
transition.

The prepared immutable change proposal enters the existing M34 publication lifecycle:

```text
queued -> leased(preparing) -> awaiting_approval -> approved
         approved -> leased(publishing) -> activation_ready
```

The proposal kind is explicit in storage and in the publication job. Existing M33 proposal bytes
remain readable and retain their accepted fingerprints. A migration may add discriminators and
closed decision kinds, but it must not rewrite migrations 0001–0014 or historical payloads.

Each profiling attempt has a fresh deterministic identity derived from the complete pre-I/O
authority **and its trusted request instant**. This permits an expired aggregate profile to be
requested again without reusing the old job or result. Request submission is crash-safe in two
durable phases: persist request/idempotency/audit, enqueue the exact job, then atomically bind one
immutable requested-job snapshot. A worker claim for an M35 job is rejected until that binding and
audit witness exist; after the one-way `NULL -> bound` transition, the operation is immutable and
replays the historical response instead of a changing live lease.

## Acceptance criteria

### A. Exact base, tenancy and authorization

1. Only an authenticated steward/admin can decide; only a distinct fresh publisher/admin can
   prepare and authorize managed publication.
2. Unknown and cross-tenant draft/proposal/job identities are indistinguishable.
3. Creation, decision and preparation bind one exact active strict registry-v2 pointer/version,
   catalog scope, connection and complete dependency watermark.
4. Base, catalog, profile, dependency or actor-lineage drift causes zero proposal, DataHub or
   activation mutation.
5. Existing M33 onboarding proposals and M34 jobs remain byte-compatible and pass unchanged.

### B. New join onboarding

6. A join over two exact active mappings can start only after a current aggregate profile exists.
7. The candidate always starts `needs_review`; name similarity and confidence grant no status.
8. Many-to-many, self/cross joins, cross-connection endpoints, unknown fields, stale mappings,
   altered transformations and incomplete profile evidence fail closed.
9. One-to-many approval requires the existing distinct-left-entity mitigation and explicit risk.
10. Approval requires a meaningful rationale and non-name evidence; exact replay is idempotent and
    altered replay conflicts.
11. Assembly appends exactly one logical summary, physical contract and join provenance decision
    while preserving every base model, mapping, binding and unrelated join exactly.

### C. Replacement and M26 remediation

12. Replacement names one existing model and uses the next exact definition version.
13. The resulting registry contains complete approved mappings/bindings for every replacement
    field and no retired mapping/binding for that model.
14. Every incident join is preserved only when both exact keys remain valid; otherwise a fresh
    approved upsert or explicit removal is mandatory.
15. A blocking M26 remediation binds the current report, full impacts, base pointer/registry and
    resolved finding set. Missing or unrelated findings fail closed.
16. A corrected candidate cannot claim M26 recovery. After activation, a new M26 inspection and
    explicit baseline approval remain required before affected source I/O.

### D. Publication, activation and evidence

17. M34 target reservation, fencing, cancellation, retry/dead-letter, post-assembly approval,
    exact DataHub v2 read-back and observed-URN rules apply unchanged to every M35 proposal.
18. API/web never receives the DataHub writer secret and the publisher cannot activate.
19. Successful publication leaves the active pointer unchanged; M23 separately activates the
    exact read-back handoff.
20. PostgreSQL migration proves old/new payload parsing, closed kind checks, role grants,
    append-only history and restart recovery.
21. Unit, PostgreSQL integration and acceptance tests cover all criteria, including concurrency,
    stale evidence, fanout, altered job payloads and historical M33/M34 regressions.
22. Internal-browser evidence shows a join draft through `activation_ready`, no automatic
    activation and no SQL/source/DataHub write before the isolated publisher step.
23. `make check`, relevant integration/acceptance gates and `git diff --check` pass; state and
    handoff documents match actual evidence.

## Implementation order

1. Freeze this plan, prompt and ADR; add red pure-contract tests.
2. Implement Phase A join-change contracts and exact v2 assembler.
3. Generalize publication proposal typing without changing historical M33 fingerprints.
4. Add tenant-bound authoring/decisions and the additive schema migration/role matrix.
5. Compose authenticated HTTP/operator and isolated publisher authority checks.
6. Prove Phase A with PostgreSQL, M23/M26 and browser regressions.
7. Implement Phase B replacement/remediation and its complete incident-join accounting.
8. Run the full gate, independent review and final documentation.

## Manual operator test

1. Activate a strict two-model registry-v2 whose models have exact same-connection bindings but no
   join.
2. As a steward, create a join draft over the two exact active mappings and observe only aggregate
   relationship evidence.
3. Attempt a self join, an altered transformation, a cross-connection key and many-to-many
   approval; verify every path returns no publication candidate and performs zero external write.
4. Approve the safe join with rationale and non-name evidence, then prepare it as a distinct
   publisher.
5. Submit to M34, run preparation, inspect the complete v2 candidate and authorize that exact
   fingerprint.
6. Run publication and verify exact read-back, `activation_ready`, unchanged pointer and immutable
   old version.
7. Activate through the separate M23 flow, run M26 inspection/baseline approval, and verify one
   bounded two-table query becomes eligible.
8. Repeat a blocking field/type drift remediation and prove affected execution remains blocked
   until the corrected version is separately activated and re-inspected.
