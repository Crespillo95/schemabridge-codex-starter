# Domain model

## Core aggregate boundaries

### LogicalModel

Represents a governed business entity independently of any physical table.

```yaml
id: Customer
name: Customer
description: A person or organization registered as a customer.
fields:
  - customer_key
  - registration_date
status: approved
version: 1
```

### CanonicalField

```yaml
id: Customer.customer_key
canonical_name: customer_key
canonical_type: string
definition: Stable identifier for a customer across source systems.
format_policy:
  null_policy: preserve
  leading_zero_policy: strip
```

### PhysicalField

Contains stable asset identity, field path, native type, descriptions, governance metadata, and bounded profile signals. Raw samples must not be persisted by default.

### TransformationPlan

A closed, typed sequence of operations. The MVP algebra includes:

```text
identity
trim
empty_to_null
validate_regex
validate_finite
validate_integral
strip_leading_zeros
pad_left
cast_integer_to_string
cast_timestamp_to_date
parse_date
normalize_decimal_scale
map_values
preserve_null
reject_invalid
```

Arbitrary Python and arbitrary SQL fragments are forbidden.

The M02 identifier specialization adds an immutable `IdentifierNormalizationPlan` with explicit
`null_policy`, `leading_zero_policy`, optional `pad_to_length`, whitespace handling, and empty-string
handling. Its pure interpreter accepts ASCII digit strings, integers, exact integral decimals, and
finite integral floats within the IEEE-754 safe integer range. Booleans, negative identifiers,
fractional values, non-finite values, malformed strings, unsafe floats, and padding overflow return
typed rejected outcomes with stable codes; they are never truncated or silently repaired. A
preserved `NULL` is a typed accepted outcome whose canonical value is `NULL`, not an invalid value.

The generic serialized `TransformationPlan` remains a closed discriminated union of the listed MVP
operations. The pure M02 interpreter remains specialized to identifiers. The deterministic SQL
compiler implements the closed SQL forms needed by the governed registry, including timestamp to
date, decimal-scale normalization, and value mapping. `parse_date` remains in the serialized union
so historical contracts can be read, but authoring and compilation reject it until PostgreSQL
shape/calendar validation is total; `TO_DATE` alone is not accepted. Regex uses an anchored linear
ASCII subset and identifier padding is capped at 256 characters. An operation without a safe
compiler implementation fails closed; the algebra never expands to raw SQL or callbacks.

### ColumnMapping

```yaml
logical_field: Customer.customer_key
physical_field: crm.customers.customer_id
confidence: 0.97
status: approved
evidence:
  - normalized_name_similarity
  - definition_similarity
  - normalized_value_overlap
risks:
  - leading_zero_semantics
transformation_plan: [...]
```

### JoinContract

```yaml
id: customer_to_account_holder
left_key:
  logical_field: Customer.customer_key
  physical_field: crm.customers.customer_id
  transformation_plan:
    version: 1
    steps:
      - operation: trim
      - operation: validate_regex
        pattern: ^[0-9]+$
      - operation: strip_leading_zeros
      - operation: reject_invalid
right_key:
  logical_field: AccountHolder.customer_key
  physical_field: bank.account_holders.gf_customer_id
  transformation_plan:
    version: 1
    steps:
      - operation: validate_finite
      - operation: validate_integral
      - operation: cast_integer_to_string
      - operation: reject_invalid
cardinality: one_to_many
default_join_type: inner
fanout_policy: require_distinct_for_left_entity_metrics
status: approved
version: 1
approval_decision_id: customer-holder-approval-v1
```

A join candidate cannot be used for execution until approved. M08 candidates retain eight named
signals, missing evidence, risks, and a pure cardinality assessment. Every executable contract
binds both logical endpoints to physical keys through closed transformation plans and cites its
immutable approval decision. One-to-many contracts require distinct left-entity metrics;
many-to-many execution is unsupported and fails closed in the MVP.

### AnalyticalRequest

A vendor-neutral representation of user intent:

```yaml
primary_entity: Customer
dimensions:
  - field: Customer.registration_date
    grain: day
metrics:
  - operation: count_distinct
    field: Customer.customer_key
filters:
  - field: AccountHolder.holder_role
    operator: equals
    value: SECONDARY
order_by:
  - field: Customer.registration_date
    direction: asc
limit: 500
```

M09 completes this boundary with closed enums for metric operations, filter operators, date grains,
and sort directions. Intrinsic validation requires at least one metric, matches filter value shape
to its operator, rejects non-finite values, prevents duplicate selections/aliases, and permits
ordering only by a selected logical field.

`ApprovedLogicalContext` contains approved logical models, fields, canonical types, field roles,
and logical join summaries only; it contains no physical dataset or column identities. Pure
context validation rejects unknown fields, incompatible roles/types, unsupported grains and
operators, absent join paths, multiple equally short approved paths, and paths beyond three models.
It identifies when a plain Customer count needs the approved one-to-many mitigation; the M10
planner applies that exact contract policy and records the change. A `ValidatedAnalyticalRequest`
records the exact context source/version/fingerprint, required logical models, and approved
join-contract IDs. It is still logical intent—not a `ResolvedQueryPlan` and not executable SQL.

The checked-in M09 context is explicitly labeled
`recorded:demo/ground_truth/approved_logical_context.yml`. It is synthetic ground-truth input for
the offline builder and tests, not a claim that every field has been read back from the live
DataHub logical model. Loading a local request draft always repeats validation against the current
context.

M21 preserves that file only as historical/test evidence. Active composition no longer loads it
independently: the logical context is one member of the atomic governed semantic registry described
below.

### Natural-language intent

M11 keeps model output inside `IntentModelOutput`: a current user language, an optional existing
`AnalyticalRequest`, and closed ambiguity codes. The schema has no SQL, physical-field, tool,
approval, or execution field and forbids extras. `IntentVocabulary` contains only the approved
Customer/AccountHolder models, four logical fields, their short definitions, the approved join
summary, bounded role values, and the exact enum operations needed by the north-star request.

An `IntentConfirmation` binds one closed alternative to the SHA-256 fingerprint of the exact
interpretation shown to the user. Confirmation fails if output validation found a hallucinated
field, unsupported value, SQL-like value, language mismatch, stale fingerprint, unavailable
alternative, or unresolved ambiguity. Only a newly validated `ValidatedAnalyticalRequest` may
then enter the unchanged M10 planner. Stable request and resolved-plan fingerprints prove that the
confirmed Spanish and guided north-star modes converge before compilation.

The M21 registry is larger than this vocabulary, but M21 does not widen the M11 parser. Product,
order, sale-line, and shipment requests are exercised as typed requests, not as claims of
registry-wide language understanding. Matching a short free-form field description to an active
logical field is a separate M27 capability. Definitions in the registry provide governed context;
they do not authorize a mapping and are never sufficient evidence of equivalence by themselves.

### ResolvedQueryPlan

Binds logical intent to approved physical mappings and joins. It contains no raw user SQL.

```yaml
physical_datasets:
  - crm.customers
  - bank.account_holders
join_contracts:
  - customer_to_account_holder
assumptions:
  - count unique customers, not holder relationships
fanout_mitigations:
  - count_distinct Customer.customer_key
```

M03 implements the executable boundary as an immutable `QueryPlan` restricted to declared dataset
scans, physical columns, closed transformation plans, date grains, typed predicates, approved join
contracts, aggregates, grouping, ordering, and an optional requested limit. Relation and output
aliases are validated inert identifiers. Filter values are `ParameterValue` objects and cannot
appear as SQL text. A plan validates every field against its declared scan, permits at most three
distinct physical datasets, rejects self joins, requires approved join contracts, and requires
non-aggregate projections to be grouped when aggregates are present.

For a one-to-many or many-to-many join, the IR fails closed unless the declared fanout policy is
implemented. `require_distinct_for_left_entity_metrics` converts `COUNT` only when its source is the
exact approved one-side key. Explicit `COUNT DISTINCT`, `MIN`, and `MAX` are invariant under row
duplication; arbitrary attribute counts, `SUM`, and `AVG` are never silently reinterpreted. Every
join predicate and normalization plan must exactly match its approved contract.

M10 introduced `SemanticPlanningContext`, `GovernedFieldMapping`, `ResolvedSemanticPlan`, and typed
resolution/rejection outcomes. M21 retains the old context name only as a compatibility alias and
makes `GovernedSemanticRegistrySnapshot` the active aggregate. A governed mapping carries its
approved decision/version, logical-field version, declared physical value type, closed
transformation plan, evidence, risks, and physical identity. The pure planner verifies the
validated logical-context fingerprint, mapping and contract approval/version, type compatibility,
unique shortest path, connected per-model datasets, three-table limit, and fanout policy before
constructing the query IR. The result records every selected mapping, contract, assumption,
mitigation, allowlist policy, and bounded rejected-source check.

`QueryPolicy` independently names exact allowed datasets and columns plus the three-table, preview
row, and timeout bounds. Neither `QueryPlan` nor any expression node has a raw SQL or free-form
expression field.

### GovernedSemanticRegistrySnapshot

M21 groups all planning truth needed for one deployment revision:

```yaml
format_version: 1
registry_id: synthetic_enterprise
version: 1
catalog_scope: synthetic-demo
source: recorded:demo/ground_truth/registries/synthetic_enterprise.yml
logical_context: ...
mapping_set: ...
join_contracts: ...
provenance: ...
```

`SemanticRegistryScope` separately binds the load to an opaque workspace, catalog scope, and
registry ID. `ScopedSemanticRegistrySnapshot` is valid only when the requested registry/catalog
exactly match the snapshot. The current synthetic registry contains seven approved logical models,
31 approved mappings, and five approved join contracts. Its canonical full-snapshot fingerprint is
`0710148874049078f751ac96f0a18131cf01daa41f27dc034ca52a8b212dd966`; the manifest independently
binds its bytes with SHA-256
`4f086f8b9f0679d6405beab45d48c6601520b9b6e0c5f20d7dc0ad987485a723`.

Construction validates the registry as one unit:

- every active logical field has an approved, current physical mapping;
- one physical field has at most one active logical meaning in the scope;
- mapping approval decisions and logical-field versions agree;
- logical join summaries and physical contracts have exactly the same identities and semantics;
- each join key is backed by the exact approved mapping and transformation;
- provenance covers logical models, mappings, and joins and cites every active decision.

The registry accepts more models and contracts than one request. The unchanged executable boundary
still permits at most three physical tables and two joins. A fresh registry load and resolution
occur before execution/retry; fingerprint or approval drift invalidates the prepared result and
requires confirmation again.

### ActiveRegistryPointer and RegistryActivationTransition

`ActiveRegistryPointer` is the one PostgreSQL-authoritative selection for a
`SemanticRegistryScope`. It binds a monotonically increasing generation to one exact immutable
DataHub registry version, fingerprint, target URN, transition, actor/time, and canonical decision
closure. Its fingerprint covers the complete pointer, including workspace scope.

`RegistryActivationProposal` describes a read-only compare-and-swap intent against generation 0
or the complete current pointer. `RegistryActivationApproval` adds the exact actor, time, action,
confirmation, and deterministic ID. `RegistryActivationTransition` is the immutable committed
fact: its active generation is exactly the proposal's expected generation plus one. Rollback uses
the same types and points to a previously active strict version; it never decrements generation or
rewrites an earlier transition.

### RegistryProjectionOutboxItem and RegistryReconciliationReport

`RegistryProjectionOutboxItem` is created atomically with the pointer and transition. It binds one
transition to one exact desired `RegistryProjectionState` and a closed delivery status. It is
delivery intent, not planning authority. `RegistryReconciliationReport` compares the PostgreSQL
pointer/history/outbox and exact immutable target with the observed DataHub projection and
HMAC-chain state. Its closed findings, inspection time, actor, and fingerprint are the only input
eligible for a matching repair approval. An ahead/conflicting/corrupt/audit-gap result cannot be
converted into overwrite permission.

### WorkflowExecutionRecord and transient preview rows

`WorkflowExecutionRecord` can represent a durable minimized result with `rows=()`, while retaining
columns, exact `row_count`, preview fingerprint, read-only execution facts, and bounded rejection
summary. The absent rows do not mean zero rows: consumers use `observed_row_count`.

The entrypoint-only `TransientExecutionResult` may carry exact preview rows within one
authenticated Streamlit session. It binds those rows to actor, workspace, workflow, revision,
registry fingerprint, activation generation, active-pointer fingerprint, and the recomputed
preview fingerprint. It is discarded on any mismatch and is never a durable domain authority.

### ControlPlaneBackupManifest and ControlPlaneRestoreVerification

`ControlPlaneBackupManifest` binds one repeatable-read archive to the source database fingerprint,
exact schema version/checksum, archive bytes, complete control-state SHA-256, table counts, audit
key version, and timezone-aware creation time under an HMAC. `ControlPlaneRestoreVerification` is
emitted only after a distinct fresh target reproduces that schema and state and verifies every
workspace audit chain plus pointer, transition, outbox, and quarantine counts.

### IdentityRotationState and IdentityRotationCompletion

`IdentityRotationState` contains only opaque current owners, bounded historical opaque bindings,
key/policy/provenance versions, and a revision. A deterministic `IdentityRotationPlan` maps every
verified old workspace/owner binding to the new key version. Approval reservation and
`IdentityRotationCompletion` are separate; completion is idempotent and the type fixes
`historical_payloads_rewritten` to `false`. `IdentityAuthorizationScope` lets a current principal
resolve one verified same-lineage historical scope without changing the historical workflow or
grant.

### Catalog inventory, refresh, and capacity

M25 keeps catalog identity separate from SQL identity:

- `CatalogConnectionId` is the opaque connection ID within an already scoped workspace;
- `CatalogAssetLocator` binds connection plus opaque catalog asset ID;
- `CatalogFieldLocator` adds a bounded field path;
- generation-scoped asset/field summaries carry only public metadata and deterministic
  fingerprints;
- `TenantCapacityPolicy` supplies durable connection, asset, field, per-minute request, and
  nonterminal-job bounds; `TenantCapacityPolicyChange` binds a revision to the current expected
  version, exact confirmation, and operator actor;
- `CatalogRefreshState` has one closed mode/state, immutable base/target generation facts, bounded
  progress, and sanitized failure;
- typed source changes are only asset/field upserts or deletes, and a source page carries a
  bounded next checkpoint plus completion fact;
- keyset page keys are compound ordered values, not decimal offsets.

An active generation is a complete immutable snapshot. A staging generation is never readable as
current catalog state. Asset identity/fingerprint does not absorb the field collection; field
fingerprints independently cover type, definition, key/nullability flags, tags, and terms.
Catalog cardinality is selected per tenant through policy and bounded pages rather than a
deployment-sized fixture. The domain retains high defensive validation ceilings
(100,000,000 assets and 1,000,000,000 fields), but these are not company defaults. One query
remains separately limited to one governed connection and three tables/two joins.

### Semantic change, scans, and aggregate profile jobs

M26 adds three related immutable contract groups.

`GovernedResourceBinding` connects one approved `GovernedMappingRef` to one exact
`CatalogFieldLocator`, catalog generation, asset/field metadata fingerprints, definition
fingerprint, and term fingerprint. `SemanticBindingSelectionSet` is the canonical human-supplied
selection when candidate evidence is ambiguous. Selection scope, mapping decision/version,
physical field, connection, asset, and field path must all agree; an equal name or definition is
not a binding.

`SemanticEvidenceObservation` covers every mapping in one exact active registry and profiles only
joins whose two endpoints have complete bindings. A join profile is
`SemanticJoinProfileProposal(connection_id, JoinProposal)` and the resulting
`AggregateJoinProfile` retains aggregate counts, cardinality, policy fingerprint, reader identity,
read-only fact, and timeout only. Both endpoints must use the same `CatalogConnectionId`.
`SemanticEvidenceBaseline` exists only after exact approval of complete field evidence and the
dependency index. Its approved revision is the resulting compare-and-swap head revision, not the
proposal's previous expected revision. Reports classify closed informational, review-required, and
blocking changes and carry a canonical `SemanticImpactSet` across mappings, joins, workflows, and
recipes.

`SemanticChangeScanRequest` has the closed lifecycle
`requested → leased → completed|retry_wait|failed|superseded`; lease ownership uses a capability
digest and monotonic fence. Catalog-generation requests include workspace, catalog scope, registry
ID, and connection; registry-pointer requests include the exact registry generation.
`SemanticJoinProfileJob` separately carries its exact workspace, `connection_id`, proposal
fingerprint, aggregate-only result, retry state, lease, and fence. A worker may claim/reclaim only
the workspace/connection pair it was configured to read and revalidates that pair before source
I/O.

`SemanticDependencyIndexState` and its manifests distinguish a complete empty source from an
incomplete traversal. One snapshot admits at most 10,000 artifacts and 100,000 mapping/join edges;
exceeding either bound is incomplete rather than truncated-complete. `SemanticPlanDependencies`
contains only the mappings and joins used by one resolved plan. `SemanticContextGateAssessment`
can be eligible only with a current approved baseline, exact current live dependency index, and no
reason code. Every other state is fail-closed before compilation/source I/O.

### DecisionRecord

Tracks who approved what, when, from which version, with evidence and known risks. An approval cannot be inferred from confidence alone.

### AuthenticatedPrincipal and WorkflowAccessGrant

M20 distinguishes verified browser identity from the historical free-form actor label.
`AuthenticatedPrincipal` contains only an opaque actor ID, opaque workspace ID, closed roles,
authentication method, and bounded issue/expiry times. Provider subject, tenant label, email,
tokens, and raw claims are not domain values. The identity adapter derives the opaque IDs with a
deployment-secret, version-prefixed HMAC; changing the key/version intentionally changes both IDs.

`WorkflowAccessGrant` immutably binds one workflow ID to a workspace, owner actor, and creation
time. Its first persistence is atomic with the initial workflow draft. Authorization first checks
the role permission and then this resource scope. The existing
workflow decision `actor` remains a string for serialization compatibility, but the authenticated
UI can populate it only from `AuthenticatedPrincipal.actor_id`; no browser command contains an
actor field.

## Important invariants

1. Canonical identifiers default to string unless the business definition proves otherwise.
2. Float identifiers must be finite and integral; conversion cannot repair earlier precision loss.
3. A high confidence score is a recommendation, not an approval.
4. Every physical field maps to at most one approved logical meaning within a scoped model/version unless an explicit exception is recorded.
5. Every join contract names its cardinality and fanout policy.
6. A query plan references approved versions of mappings and joins.
7. A request with unresolved ambiguity cannot be executed.
8. Transformation plans are serializable, versioned, deterministic, and testable.
9. Model output cannot grant approval, select physical assets, carry SQL, or invoke execution.
10. A managed browser decision actor is always derived from a current authenticated principal.
11. No workflow read or mutation crosses an opaque workspace boundary.
12. A workflow ID cannot exist as both an authenticated resource and a different/legacy ownership
    binding; draft and grant creation commit or roll back together.
13. Logical choices, physical mappings, join contracts, and provenance enter planning as one
    integrity-checked registry revision, never as independently current fragments.
14. Registry capacity never raises the per-query maximum of three physical tables and two joins.
15. Field-name or definition similarity alone cannot create an approved semantic equivalence.
16. An active registry generation increases monotonically; rollback is another immutable
    transition, never a historical rewrite.
17. PostgreSQL is the active-pointer authority. The DataHub pointer document is a repairable
    projection and cannot select planning state.
18. Durable managed workflows contain result metadata and fingerprints but no preview rows;
    transient rows are usable only under their exact authenticated session binding.
19. Identity rotation changes opaque authorization bindings only and never rewrites historical
    workflow, grant, decision, recipe, or publication payloads.
20. Catalog identity always includes workspace and connection; equal `schema.table` or field paths
    in different connections are not semantically or physically equal.
21. A catalog refresh cannot replace the active generation until every bounded page, count, quota,
    base generation, source completion, and fingerprint check agrees.
22. Catalog inventory size never grants mapping, join, or query authorization.
23. Every managed mapping baseline identifies one exact workspace, catalog scope, connection,
    asset, field path, generation, mapping version, and approval decision.
24. Aggregate join profiling is valid only when both approved endpoints resolve to the same
    connection and the worker is configured for that connection.
25. A dependency index is complete only after every paginated workflow and scope-qualified current
    recipe source reaches validated EOF under the current scan lease.
26. An incomplete dependency index or affected latest finding is blocking; unrelated catalog
    changes do not invalidate an exact unaffected plan.
27. Catalog or registry changes enqueue scope-qualified durable scans; no configured singleton
    registry or full catalog materialization defines tenant capacity.
