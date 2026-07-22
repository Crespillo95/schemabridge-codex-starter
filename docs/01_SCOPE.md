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
- Enterprise authentication, multi-tenancy, billing, or granular RBAC.
- Model training or a custom embedding infrastructure.
- A general-purpose BI charting product.
- Automatic query cost optimization beyond bounded safeguards.

## Scope gate

A proposed feature enters the MVP only when it improves the complete north-star journey or one of the five judging criteria without jeopardizing completion.

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
