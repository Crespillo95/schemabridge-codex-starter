# M33 — Generic governed semantic onboarding

## Status

- State: complete; accepted locally on 2026-08-02
- Production/release gate: **NO-GO**
- Depends on: accepted local M20–M29 and M32 contracts
- Followed by: M34 dedicated registry publication worker and generic DataHub binding

## Objective

Let an authenticated tenant start from an exact retained catalog generation and create a governed,
tenant-bound semantic onboarding draft without editing repository fixtures or redeploying the
service. A steward must explicitly review the logical model and every physical mapping. A
publisher may then prepare, but not yet publish, one immutable `ready_for_publication` proposal
whose complete decision closure and source observations are fingerprinted.

M33 closes the unsafe gap between the dynamic physical inventory and registry authoring. It does
not claim that the product is commercially releasable: managed DataHub mutation, activation,
production evaluation, independent security verification, and an operated pilot remain later
gates.

## Security decision

M33 deliberately stops before DataHub mutation.

- The managed web/API process must not receive a DataHub writer credential.
- The current DataHub adapter derives related PostgreSQL URNs from names and is not a generic
  authority boundary.
- Publication requires a dedicated queue/worker and an observed, allowlisted asset identity.
- M23 activation remains separate and cannot run until M34 has durably published and read back the
  exact immutable registry version.

This is fail-closed behavior, not a feature toggle or recorded fallback.

## In scope

1. New pure onboarding contracts, independent of legacy demo review contracts.
2. Closed onboarding RBAC derived only from `AuthenticatedPrincipal`.
3. Tenant-scoped PostgreSQL persistence for drafts and append-only decisions.
4. Exact binding to workspace, catalog scope, connection, retained generation, catalog vector,
   asset locator, asset fingerprint, field locator, field fingerprint, physical type, and observed
   DataHub asset URN when one exists.
5. A read-only tenant-bound preflight that derives the active catalog generation, exact physical
   observations and active-registry base server-side, then returns one deterministic confirmation
   fingerprint required by creation.
6. Generic logical model and field authoring, including query roles and bounded allowed values.
7. Mapping proposals that always start `needs_review`; confidence and exact name equality never
   approve them.
8. Explicit model and mapping decisions with optimistic revision checks and meaningful human
   rationale.
9. Revalidation of all physical observations before a decision and before proposal preparation,
   plus an atomic authority check inside the PostgreSQL create/decision/preparation transaction.
   Verified workspace/actor lineage preserves the exact historical identity pair after opaque-id
   rotation; incomplete, duplicated or cross-coordinate aliases fail closed.
10. Deterministic immutable `ready_for_publication` registry proposal for one complete onboarding
   draft, with no DataHub/source/LLM I/O.
11. Authenticated HTTP endpoints plus a non-mutating operator/export path.
12. Bounded recent-history inspection and compact exact idempotency replay without copying the
    complete draft for every decision.
13. An operator-facing commercial usage plan that distinguishes current support, safe limits, and
    remaining release gates in `docs/19_COMMERCIAL_USAGE.md`.
14. Empty-tenant, stale-generation, cross-tenant, idempotency, size-bound, and zero-side-effect
    tests.

## Explicitly out of scope

- DataHub mutation from the web/API process.
- Activation or rollback of an unpublished M33 proposal.
- Automatically approving a model, mapping, field, or join.
- Deriving a DataHub URN from `schema.table` or any display name.
- Reading source rows, samples, profiles, DSNs, or credentials.
- Free-form LLM SQL, physical identifiers, approvals, or URNs.
- Generic join profiling and approval. M33 accepts a zero-join first registry; generic join
  onboarding is added behind the same contracts after the dedicated worker/routing path exists.
- MySQL, SQL Server, Oracle, BigQuery, Snowflake, Redshift, or cross-connection execution.
- A production SLA, M30 certification, M31 pilot, or commercial GA claim.

## Domain contracts

### Physical observation

Each proposed mapping carries one immutable observation with:

- `CatalogFieldLocator` (and therefore workspace, connection and asset);
- catalog generation and catalog generation vector fingerprint;
- exact catalog scope;
- asset and field metadata fingerprints;
- exact one-segment PostgreSQL `schema.table.column` `PhysicalFieldRef` used by the current
  compiler; nested field paths fail closed;
- normalized physical value type;
- optional observed DataHub dataset URN copied from catalog identity, never constructed;
- bounded evidence and risks.

The observation is identity evidence. It is not approval.

### Transformation boundary

- Every operation is from the closed typed algebra and is checked in order against the observed
  physical type before a draft can exist.
- Regex validation accepts only an anchored linear ASCII subset shared with PostgreSQL; groups,
  alternation, lookaround, backreferences and dialect-specific escapes fail closed.
- Identifier padding is capped at 256 characters at domain and HTTP deserialization boundaries.
- `parse_date` remains deserializable for historical contract compatibility, but M33 authoring and
  the PostgreSQL compiler reject it. PostgreSQL `TO_DATE` is not total or exact; string-to-date
  parsing stays unsupported until calendar/shape validation cannot throw or normalize bad input.

### Draft

`SemanticOnboardingDraft` contains:

- opaque inert draft id;
- immutable workspace and owner;
- target registry scope and base active pointer/version/fingerprint (or explicit empty base);
- exact connection and retained generation;
- one bounded logical model with 1–100 fields for the initial vertical;
- one or more mapping proposals per logical field, up to 2,000 total;
- revision, lifecycle state and full payload fingerprint;
- model and mapping decision references.

Lifecycle states are `needs_review`, `ready_for_publication`, and `superseded`. Creation can never
produce `ready_for_publication`.

### Decisions

- `approve` or `reject` only;
- actor/workspace come from the authenticated principal;
- steward or platform administrator only;
- minimum meaningful rationale after trimming outer whitespace and at least one evidence item
  other than name similarity for approval;
- append-only decision record;
- exact source revision and resulting revision;
- edits and catalog drift invalidate readiness;
- publisher cannot manufacture semantic decisions.

### Prepared proposal

Preparation is allowed only to a publisher or platform administrator with a current principal. It
must:

- re-read the draft and exact retained catalog facts;
- reject any stale connection, scope, generation, locator, fingerprint, type, base pointer or
  decision closure;
- require a publisher distinct from the owner and all approving actors in managed mode;
- produce a deterministic, immutable proposal fingerprint and version;
- record a durable `ready_for_publication` audit event before returning success;
- perform zero DataHub, source, LLM, compiler, guard, preview, or activation calls.

## HTTP surface

All request schemas use `extra="forbid"`, inherit the global 64 KiB body limit, and reject actor or
workspace fields supplied by clients. Clients first obtain server-derived authority through:

- `POST /v1/semantic-onboarding/preflight`
- `POST /v1/semantic-onboarding/drafts`
- `GET /v1/semantic-onboarding/drafts/{draft_id}`
- `POST /v1/semantic-onboarding/drafts/{draft_id}/model-decisions`
- `POST /v1/semantic-onboarding/drafts/{draft_id}/mapping-decisions`
- `POST /v1/semantic-onboarding/drafts/{draft_id}/prepare-publication`

Draft inspection accepts `history_limit=1..50` (default 25), returns the newest chronological
window, and exposes `history_truncated`. It does not silently return an unbounded audit history.

Cross-tenant and unknown identifiers return the same bounded unavailable response.

## Acceptance criteria

### A. Empty and tenant isolation

1. An empty tenant lists no drafts and receives an explicit `not_configured` query state; no demo
   fixture is loaded.
2. Tenant B cannot read, decide, prepare, replay, or infer a draft belonging to tenant A.
3. Actor/workspace fields in a request body are rejected before protected storage access.

### B. Creation and identity

4. Only an analyst, steward, or platform administrator with a current principal can create a
   draft; only steward/admin can decide.
5. Preflight derives the active generation, catalog vector, physical identities/types and registry
   base in one read-only repeatable-read snapshot; creation requires its exact fingerprint.
6. Creation binds every mapping to the exact enabled connection, retained generation, catalog
   vector, one-segment asset/field locator and fingerprints, validates an ordered type-correct
   transformation path, and checks that authority again atomically before the first durable write.
7. Two connections with the same `schema.table.field` remain distinct.
8. A DataHub asset URN is accepted only when it was observed as the exact asset identity; no URN is
   synthesized.
9. Creation performs no source, DataHub writer, LLM, compiler, guard, preview, or activation I/O.

### C. Governance

10. Every model and mapping starts `needs_review`, including confidence `1.0` and an exact name
   match.
11. Approval without non-name evidence or a meaningful rationale is rejected.
12. The model and every logical field require at least one approved mapping; unresolved proposals
    block readiness.
13. A physical field cannot be approved for two meanings in one proposal, including when multiple
    catalog asset identifiers alias the same physical coordinate.
14. Decisions use CAS; stale revision, stale catalog or replay with a different payload produces
    zero mutation. Exact concurrent retries remain idempotent even when the first operation becomes
    visible between the application lookup and the atomic store call.

### D. Preparation

15. Only a complete current decision closure can produce `ready_for_publication`.
16. Managed preparation enforces separation of duties between owner/stewards and publisher.
17. The prepared proposal has exact scope, target version, catalog/base bindings, decision ids and
    a stable fingerprint.
18. Identical retry is idempotent; altered retry conflicts.
19. Any catalog or base-registry drift before preparation leaves the draft non-executable and
    performs zero external mutation.
20. A first zero-join proposal is valid without inventing a join decision.

### E. Bounds and evidence

21. Text, collection, draft-byte, decision-byte and prepared-proposal-byte limits are enforced in
    domain/application and PostgreSQL storage, including the 256-character padding cap and closed
    PostgreSQL-safe regex subset.
22. Inspection returns at most 50 recent decisions/proposals/audit rows and declares truncation;
    preparation still loads the complete bounded decision closure.
23. Idempotent decision replay stores one immutable root plus append-only decisions and reconstructs
    the exact historical response, avoiding quadratic full-draft snapshot growth.
24. The public failure vocabulary contains no DSN, SQL, secret, source payload, existence fact or
    provider detail.
25. PostgreSQL integration proves durable workspace isolation, authority locking, CAS, append-only
    decisions, idempotent preparation and restart recovery.
26. Browser/AppTest proves empty → draft → review → ready-for-publication with no demo button or
    hidden execution.
27. `make check` and the relevant integration/acceptance gates pass.

## Required test matrix

- `tests/unit/test_semantic_onboarding.py`
- `tests/unit/test_semantic_onboarding_use_cases.py`
- `tests/unit/test_semantic_onboarding_authorization.py`
- `tests/unit/test_semantic_onboarding_http.py`
- `tests/unit/test_semantic_onboarding_identity_resolution.py`
- `tests/unit/test_semantic_onboarding_postgres_store.py`
- `tests/unit/test_semantic_onboarding_registry_base.py`
- `tests/unit/test_semantic_onboarding_schema_migration.py`
- `tests/integration/test_semantic_onboarding_postgres.py`
- `tests/acceptance/test_m33_semantic_onboarding.py`
- manual in-app browser test recorded in `tasks/M33_HANDOFF.md`

## Implementation order

1. Pure domain contracts and red unit tests.
2. Authorization and application ports/use cases with in-memory fakes.
3. Migration and PostgreSQL stores.
4. Authenticated HTTP schemas/routes and bootstrap composition.
5. Prepared proposal export and usage documentation.
6. Focused unit/integration/acceptance tests.
7. Internal browser verification and full `make check`.
8. Project state, decision log, current task and handoff.

## Manual operator test

The operable local command, expected browser evidence, limitations, and cleanup are documented in
`docs/19_COMMERCIAL_USAGE.md`. The final observed result is recorded separately in
`tasks/M33_HANDOFF.md`; documentation is not execution evidence.

1. Authenticate as tenant A and verify the onboarding page is empty.
2. Select an exact retained PostgreSQL catalog generation and create a logical model with three
   fields from a non-demo synthetic asset.
3. Verify every proposal is `needs_review` and no SQL appears.
4. Approve the model and mappings as a steward using non-name evidence and rationale.
5. Authenticate as a distinct publisher and prepare the exact next version.
6. Verify `ready_for_publication`, stable fingerprint, `external_writes_performed=false`, and no
   execution controls.
7. Attempt the same IDs from tenant B and verify indistinguishable unavailable responses.
8. Change the catalog generation and verify the old draft cannot be prepared.

## Completion contract

M33 is complete only when every criterion above has direct evidence, all required tests pass, the
manual browser path is recorded, documentation matches the actual surface, and the project state,
decision log, current task and handoff are updated. Completion of M33 does **not** change the global
commercial/release verdict from NO-GO.
