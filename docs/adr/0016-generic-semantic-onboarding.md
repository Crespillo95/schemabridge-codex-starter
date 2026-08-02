# ADR 0016: Tenant-bound semantic onboarding stops before managed publication

- Status: accepted for M33 implementation
- Date: 2026-08-02

## Context

SchemaBridge can index large tenant catalogs and can consume an approved semantic registry, but a
new customer cannot author that registry without changing recorded repository bundles. The legacy
canonical review accepts untrusted actor strings and represents physical identity only as
`schema.table.field`. It therefore cannot distinguish identical names across connections or prove
which retained catalog observation was approved.

The existing DataHub registry publisher also derives related PostgreSQL dataset URNs from names,
and the managed web process intentionally has no writer credential or publication queue. Calling
that adapter from a new onboarding API would violate tenant isolation, separation of duties and the
no-hidden-mutation boundary.

## Decision

M33 introduces a separate authenticated onboarding aggregate and PostgreSQL workflow.

- Workspace, actor and permissions derive from `AuthenticatedPrincipal`.
- A draft is bound to an exact catalog scope, connection, retained generation/vector, asset/field
  locators and metadata fingerprints, normalized type, and base-registry state.
- The PostgreSQL MVP accepts exactly one catalog field-path segment and one
  `schema.table.column` physical reference. The physical authority key is the exact
  workspace/connection/generation/physical-coordinate tuple, so alternate asset identifiers cannot
  grant the same column two meanings.
- Before creation, a read-only preflight derives those physical facts and the active-registry base
  in one tenant-scoped repeatable-read snapshot. The client confirms its deterministic fingerprint;
  it never supplies schema/table/type/URN authority from an out-of-band constant.
- Logical model and mapping decisions are explicit, append-only and revision-checked.
- Confidence and name similarity remain advisory only.
- Mapping transformations must form one ordered, type-correct path from the observed physical type
  to the declared canonical type; merely containing a cast operation is not sufficient.
- Regex validation is restricted to an anchored linear ASCII subset compatible with PostgreSQL,
  and identifier padding is capped at 256 characters before compilation. `parse_date` remains
  readable as a historical typed step but is rejected by M33 authoring and the compiler because
  PostgreSQL `TO_DATE` can normalize non-matching input or abort on invalid calendar values.
- A publisher can prepare an immutable, audited `ready_for_publication` proposal only after exact
  revalidation and separation-of-duties checks.
- M33 performs no DataHub mutation, source read, LLM call, SQL compilation/execution, registry
  activation, or recorded fallback.
- Create, decision and preparation acquire the same stable workspace advisory lock used by registry
  activation, lock/recheck catalog authority and compare the active base before their CAS write.
- Exact historical decision replay uses one immutable root plus append-only decisions. Decision
  operations store revision/fingerprint metadata rather than a full draft snapshot, keeping growth
  linear. Public inspection is a declared recent window, never an unbounded response.
- Opaque identity rotation resolves an active principal to bounded verified workspace/actor pairs.
  Mutations persist the exact actor paired with the historical workspace; separation of duties and
  exact replay compare lineage, while incomplete, duplicate or cross-coordinate aliases fail
  closed. A retry that becomes visible immediately before the atomic store call is delegated to
  that store for exact replay-vs-conflict validation.

Managed publication will be implemented in M34 through a dedicated queue/worker. That milestone
must preserve exact observed DataHub asset identities, publish and read back one atomic registry
version, then hand the immutable version to the separate M23 activation workflow.

## Consequences

- A new tenant can define and govern semantic context without editing code or fixtures.
- Physical-name collisions across connections cannot collapse into one authority identity, while
  aliases for the same physical coordinate cannot evade the one-meaning rule.
- The product remains NO-GO after M33 because prepared context is intentionally non-executable.
- The legacy review remains available for bounded demo/local compatibility but is not the M33
  authenticated commercial boundary.
- A zero-join first model is valid; join onboarding follows the same authority model when the
  routed profiling/worker path is available.
- Documentation and UI must distinguish `ready_for_publication` from published or active.
- Only unquoted-canonical lowercase PostgreSQL physical identifiers within the 63-byte segment
  limit enter this path. Quoted/mixed-case and nested field paths remain explicit unsupported
  cases until the resolver and compiler have matching end-to-end semantics.
- Text-to-date normalization is an explicit unsupported case until a total PostgreSQL calendar
  parser has positive, malformed, impossible-date and non-canonical-shape integration evidence.

## Rejected alternatives

1. Reuse `canonical_review` directly: rejected because actor/workspace authorization and exact
   catalog identity are absent.
2. Publish from the web process: rejected because it would require a writer credential and bypass
   the missing queue/worker boundary.
3. Derive DataHub URNs from schema/table names: rejected because names are not catalog authority.
4. Auto-approve exact matches: rejected because similarity is evidence, never semantic authority.
5. Continue using recorded YAML per customer: rejected because it requires repository edits and
   cannot support tenant lifecycle safely.
