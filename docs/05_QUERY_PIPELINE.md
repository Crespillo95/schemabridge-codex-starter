# Governed query pipeline

## Principle

Natural language never jumps directly to executable SQL.

```text
user input
  → AnalyticalRequest
  → ambiguity check
  → semantic resolution
  → ResolvedQueryPlan
  → deterministic SQL AST/compiler
  → SQL policy validation
  → read-only preview
  → result validation
  → optional approved context publication
```

## Input modes

### Guided mode

The user selects approved logical fields, operators, aggregates, and grains. This mode is
implemented first and is the reference behavior.

M09 implements the logical boundary through `request-demo`. Its available model/field choices come
from an explicitly labeled approved-context port, while operations and grains come from closed
enums. Raw SQL, physical assets, arbitrary expressions, and natural-language parsing are not input
fields. Validation completes before the request can cross the planner port:

```text
guided primitive selections
  → closed-enum parsing
  → immutable AnalyticalRequest
  → approved field/type/role validation
  → unique shortest approved logical path or typed rejection
  → ValidatedAnalyticalRequest
  → governed physical resolution (M10)
  → restricted QueryPlan
  → deterministic compile and independent SQL guard
  → optional bounded read-only preview and rejected-source report
```

`request-demo` retains the M09 fake acknowledgement for a logical-only demonstration.
`governed-demo` invokes M10's separate planning/execution use cases. The recorded planning context
is visible in output and is never a hidden fallback for a failed live catalog.

### Natural-language mode

M11 converts a question into the same typed `AnalyticalRequest`. The parser port receives only the
current language and a bounded logical vocabulary; it never receives credentials, raw samples,
physical assets, write tools, or an SQL/execution capability. The key-free deterministic adapter is
the default for CI and demos. The explicitly selected live adapter uses provider structured output,
stores no response, and exposes no tools.

Provider output is reparsed through the immutable domain schema and checked again against the exact
vocabulary and current approved logical context. Hallucinated fields/models, unsupported enums,
unknown role values, SQL-like filter values, and language mismatches fail before confirmation. The
entrypoint previews interpretations and alternatives; it invokes semantic planning only after a
fingerprinted `IntentConfirmation`, and it does not compile or execute SQL in the M11 command.

## Ambiguity handling

The north-star phrase “group all customers” can mean a distinct customer count, relationship count, or row listing. The parser should produce:

```yaml
interpretation: count_distinct_customers_by_registration_date
requires_confirmation: true
alternatives:
  - count_holder_relationships
  - list_individual_customers
```

Execution requires a confirmed interpretation.

For guided M09 input, ambiguity is structural rather than linguistic: zero approved paths yields
`missing_approved_join_path`, and multiple equally short paths yield
`ambiguous_approved_join_path`. Both fail before the planner port is called. Natural-language
interpretation adds closed codes for count-versus-list, customer-distinct-versus-holder-relationship
count, date meaning, unknown role values, and unresolved instruction-like text. The latter is
treated only as untrusted business text and cannot change tools, approval, or safety policy.

## Semantic resolution

For every referenced logical field, the planner finds:

- an approved physical mapping;
- a supported transformation plan;
- a dataset that satisfies the request;
- approved join contracts connecting required models;
- cardinality and fanout implications;
- any version mismatch or stale validation.

The planner should choose the shortest approved path that satisfies all concepts, then explain its choice. It must not invent joins from an LLM response.

## Query IR

The SQL compiler consumes a restricted IR, not strings. Expected nodes include:

- dataset scan;
- transformed expression;
- projection;
- typed predicate;
- approved join;
- aggregation;
- date grain;
- group by;
- order by;
- limit.

No raw expression node exists in the MVP.

### M03 implemented boundary

The M03 compiler accepts only the typed `QueryPlan`. It builds a SQLGlot expression tree and emits
PostgreSQL with psycopg `%s` placeholders; filter values and approved mapping values are carried in a
separate parameter tuple. Requested limits are capped by the preview policy, and a missing request
limit becomes the policy limit.

The final rendered SQL is then reparsed by a separate SQLGlot guard. Only a guarded
`ValidatedQuery` reaches the PostgreSQL preview port. The guard checks statement count and root
type, nested destructive nodes, assets, columns, functions, joins, table count, placeholders, and
the literal preview limit independently of compiler metadata. The psycopg adapter additionally sets
the transaction read-only flag, applies a transaction-local statement timeout, caps fetched rows,
and reports the observed database identity and safety settings.

The current compiler interprets only the M03 north-star transformation subset. Other closed M02
operations fail with `unsupported_transformation`; no fallback expression or SQL path exists.

## Fanout

If a left entity metric crosses a one-to-many join, the planner must:

1. detect the risk;
2. choose an approved mitigation, commonly `COUNT DISTINCT` or pre-aggregation;
3. expose the mitigation to the user;
4. fail when no valid mitigation is defined.

M09 identifies the approved logical fanout path. M10 repeats the control using the complete
versioned contract: an explicit Customer `count_distinct` remains unchanged, while a Customer
`count` is changed only to the contract-defined `count_distinct` mitigation and records the
requested operation, applied operation, contract, and reason. A relationship count on
`AccountHolder.customer_key` remains a relationship count. Other unsupported left-entity
aggregates fail closed.

## Validation layers

1. Pydantic/domain validation of intent and plan.
2. Compiler allowlist of supported operations.
3. SQL parser validation of one statement and root statement type.
4. Asset and column allowlists.
5. Join predicate and Cartesian-product checks.
6. Table-count and result-limit checks.
7. Database read-only role, read-only transaction, and timeout.
8. Result-shape and rejection validation.

## North-star compiled behavior

The M10 planner normalizes padded CRM IDs and finite integral account-holder floats, rejects unsafe
float ranges, maps role synonyms to `SECONDARY`, joins through the approved one-to-many contract,
and counts distinct customer keys by registration date.
