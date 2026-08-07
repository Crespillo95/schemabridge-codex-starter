# Scope

## MVP capabilities

### Semantic mapping

- Discover datasets and fields through DataHub.
- Profile a bounded sample through a read-only PostgreSQL adapter.
- Rank candidate physical fields for a logical concept using multiple signals.
- Propose canonical name, type, definition, normalization plan, evidence, confidence, and risks.
- Approve, edit, reject, or mark a field as a different concept.

### Logical models and governance

- Represent `Customer`, `AccountHolder`, and `Account` as logical models.
- Link approved physical datasets and fields to logical equivalents where supported.
- Attach glossary terms, descriptions, structured properties, and a decision document.
- Require explicit approval for each mutation.

### Relationship mapping

- Propose joins using declared constraints, lineage, historical query context, names, definitions, value overlap, uniqueness, and cardinality evidence.
- Represent approved relationships as versioned join contracts.
- Support `one_to_one`, `one_to_many`, `many_to_one`, and `many_to_many` classifications.
- Explain fanout implications and required mitigations.

### Query studio

- Guided input for entity, dimensions, metrics, filters, date grain, order, and limit.
- Natural-language input that resolves to the same typed analytical request.
- Projection, filter, `INNER JOIN`, `LEFT JOIN`, grouping, ordering, and bounded result limit.
- Aggregations: `COUNT`, `COUNT DISTINCT`, `SUM`, `AVG`, `MIN`, and `MAX`.
- Date grains: day, week, month, and year.
- Maximum three physical tables in an MVP query.

### Safe execution

- Typed request and typed resolved query plan.
- Deterministic PostgreSQL compiler.
- SQL AST validation and policy checks.
- Dedicated read-only identity and transaction.
- Statement timeout, result limit, allowlist, single-statement enforcement, and no Cartesian products.
- Preview and rejected-record report.

### Reuse and evaluation

- Save approved mapping decisions, join contracts, and validated query recipes.
- Load prior approved context before making a new proposal.
- Ground-truth dataset for semantic mappings, joins, and analytical requests.
- Reproducible metrics and acceptance tests.

## Explicitly outside the MVP

- Physical schema migrations or automatic column renaming.
- Any source database write.
- Production employer data, identifiers, screenshots, or SQL.
- More than PostgreSQL as an executable dialect.
- Arbitrary SQL, recursive CTEs, self joins, window functions, or free-form expressions.
- More than three tables per request.
- Fully autonomous approval of semantic mappings or DataHub changes.
- Enterprise authentication, multi-tenancy, billing, or granular RBAC in the hackathon MVP.
- Model training or a custom embedding infrastructure.
- A general-purpose BI charting product.
- Automatic query cost optimization beyond bounded safeguards.

## Scope gate

A proposed feature enters the MVP only when it improves the complete north-star journey or one of the five judging criteria without jeopardizing completion.

## Production expansion after the MVP

The operator expanded the project beyond the hackathon MVP. M20 adds a bounded production identity
slice: provider-neutral browser OIDC, five closed roles, workspace/owner isolation for UI workflows,
fixed deployment profiles, versioned HMAC pseudonyms, and result redaction outside analyst/admin
roles. It does not add billing, invitations, SCIM, identity-provider administration, immediate
revocation of an already issued token, tenant-specific live adapter credentials, per-principal
quotas, or a general organization console. Those remain outside scope until a later production
milestone explicitly plans them.

M21 replaces the split north-star planning recordings with one atomic, scoped governed semantic
registry. The verified synthetic registry contains seven logical models, 31 approved physical
mappings, and five approved join contracts. Registry capacity is deliberately independent from
query capacity: one request still uses at most three physical tables and two joins. The deterministic
source corpus now contains 465 rows across eleven tables and eight schemas, including separate
commerce, sales, fulfillment, and support cases that exercise homonyms, heterogeneous identifiers,
categorical mappings, decimals, timestamps, nulls, malformed values, and fanout.

M21 does not reconstruct the complete registry from live DataHub; M22 owns that adapter behind the
same application port. Registry publication, activation, migration, and reconciliation belong to
M23. The natural-language parser remains the bounded Customer/AccountHolder parser delivered in
M11. Matching a free-form request or a short field description against every field in the active
registry, and presenting catalog-driven guided controls, belongs to M27. A field definition may be
retained as governed context or used as one piece of candidate evidence, but it is not sufficient
by itself to establish semantic equivalence.

M25 adds a tenant-scoped, dynamically sized physical catalog inventory. Connection, asset, and
field counts are durable data: one workspace may expose 10 tables while another exposes 5,434 or
more across multiple connections. Metadata is streamed into invisible PostgreSQL generations and
served through signed bounded keyset pages; no product constant or full-list request defines the
tenant's size. A version-checked operator command changes each workspace's durable capacity
without a release and records an immutable revision. Field search includes definitions, native
types, tags, and glossary terms as bounded candidate evidence. This does not make an indexed asset
executable and does not change the
one-connection, three-table, two-join query limit. M25 indexes definitions, types, tags, and terms
for later candidate retrieval but makes no OpenAI request or semantic-equivalence decision.

M26 adds governed semantic-change management over that dynamic inventory. Every approved physical
mapping is observed through an explicit workspace/catalog/connection/asset/field binding, so
homonymous resources in another connection cannot replace it. Catalog-generation changes fan out
to the active semantic registries in the same scope; a separately operated reconciler pages the
managed workflow/recipe dependency set in pages of 50, with one fail-closed snapshot capped at
10,000 artifacts/100,000 edges, re-profiles approved joins through workspace-and-connection-bound
aggregate-only jobs, and records immutable reports and decisions. Incomplete dependency coverage,
unavailable evidence, cross-connection joins, or affected drift blocks planning before compilation
or source I/O. Unrelated changes among 5,434 or more tables do not block an unaffected request.
M26 makes no OpenAI request and does not add description matching, connector routing, federation,
or a wider query limit.

M32 adds a copy-first natural-language SQL capability after the locally accepted M27–M29
boundaries. Both simple and advanced requests search the complete current governed registry, but
one interpretation receives only the relevant approved closure of at most three logical models,
twelve fields, and two joins. Physical discovery remains non-executable until an explicit mapping
approval exists. Query capacity remains one connection, three physical tables, and two approved
joins regardless of catalog size.

M32 preserves the existing version-1 request/plan contracts and introduces separate version-2
contracts for bounded boolean predicates, row counts, conditional aggregates, numeric buckets,
`HAVING`, ranking/tiling, partition/running/moving calculations, offsets/deltas/percentages,
post-window filters, and final output-alias ordering. Automatic v1/v2 selection is based only on
whether the complete typed meaning is representable; request length, line count, keywords,
language, and model confidence have no routing authority.

Version 2 permits at most four derived window outputs and eight window AST nodes, in addition to
the unchanged one-connection/three-table/two-join limits.

The primary M32 result is standalone, PostgreSQL-only SQL for copy/download. Preparation returns a
typed interpretation plus deterministic resolved-plan fingerprint, selected approved
datasets/mappings/joins, fanout facts, and review evidence/risks; it produces no SQL. Explicit
confirmation reloads context, revalidates and re-resolves that same signed request, then performs
deterministic compilation, independent AST validation, typed-literal rendering, and a second
independent validation with zero placeholders. That operation never executes the query and
reports `executed=false`. Optional preview remains a separate read-only path that accepts only
parameterized guarded SQL.

“Copy into another client” means a PostgreSQL editor connected to the same governed database
context shown in the preview. M32 does not bind or validate an arbitrary destination database,
and does not claim that homonymous schemas elsewhere have equivalent meaning. Approved physical
identifiers for this lane are currently unquoted-canonical lowercase ASCII names within
PostgreSQL's 63-byte identifier limit.

The advanced-query benchmark scope includes bounded ranking/top-N/`NTILE`, partition averages,
duplicate detection and grouped thresholds, running/moving calculations, `LAG`/`LEAD`,
delta/percentage change, conditional metrics, and numeric buckets. It explicitly excludes
cross/self joins, arbitrary subqueries, set operations, recursion, and gaps/islands. `ROLLUP`
remains excluded until a reviewed contract exposes `GROUPING()` flags that distinguish subtotal
`NULL` from genuine governed `NULL`.

M32 does not add multi-dialect execution or transpilation, arbitrary SQL/expression input,
automatic semantic approval, a wider query/table/join boundary, production-quality guarantees, or
source/DataHub mutation authority. Ambiguous, stale, unsupported, or unapproved requests fail
before SQL rather than receiving an approximate query.

## Definition of done

The MVP is done when:

1. the north-star request returns the ground-truth result;
2. duplicate holder links do not overcount customers;
3. `127.5`, `NaN`, and `NULL` join keys are handled as specified;
4. raw LLM SQL cannot reach the executor;
5. source write attempts are blocked independently at SQL policy and database-role layers;
6. approved mappings and query context are visible in DataHub;
7. the second run reuses approved context;
8. repository setup, examples, live demo, and sub-three-minute video are complete.
