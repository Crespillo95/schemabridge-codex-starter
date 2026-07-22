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
operations. M02 interprets only the identifier specialization; date, decimal-scale, and value-map
execution remain later bounded use cases.

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
implemented. `require_distinct_for_left_entity_metrics` requires `COUNT DISTINCT` for aggregates
whose source belongs to the contract's left dataset; an explicit aggregate on the right-side
relationship is not silently reinterpreted.

M10 adds `SemanticPlanningContext`, `GovernedFieldMapping`, `ResolvedSemanticPlan`, and typed
resolution/rejection outcomes. A governed mapping carries its approved decision/version, declared
physical value type, closed transformation plan, evidence, risks, and physical identity. The pure
planner verifies the validated logical-context fingerprint, mapping and contract approval/version,
type compatibility, unique shortest path, connected per-model datasets, three-table limit, and
fanout policy before constructing the query IR. The result records every selected mapping,
contract, assumption, mitigation, allowlist policy, and bounded rejected-source check.

`QueryPolicy` independently names exact allowed datasets and columns plus the three-table, preview
row, and timeout bounds. Neither `QueryPlan` nor any expression node has a raw SQL or free-form
expression field.

### DecisionRecord

Tracks who approved what, when, from which version, with evidence and known risks. An approval cannot be inferred from confidence alone.

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
