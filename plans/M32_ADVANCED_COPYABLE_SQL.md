# M32: Simple and advanced natural language to copyable PostgreSQL

- Status: complete and accepted locally for the deterministic/synthetic product-capability scope;
  manual browser evidence and all production/release gates remain open
- Started: 2026-07-30
- Timebox: one vertical product milestone
- Dependency: M29 locally accepted baseline; M30 and M31 retain their reserved production roles
- Track: product capability; M32 does not imply production, pilot, release, or GA readiness

## Objective

Turn a simple or advanced natural-language analytical request into one exact, complete,
standalone PostgreSQL statement that the user can copy into another PostgreSQL client connected to
the same governed database/context shown in the preview. Execution inside SchemaBridge is an
optional, separately authorized validation path; it is not the primary result and is disabled by
default.

M32 extends the governed typed language instead of accepting arbitrary SQL. A model may produce
only bounded structured mentions and a typed intent or closed ambiguity codes. Deterministic
server code retrieves approved semantic context, validates and resolves the intent, selects a
compatible request/plan version, compiles PostgreSQL, guards the AST, renders typed literals for
the copy artifact, and guards that final statement again.

“Exact” has a deliberately bounded meaning: every material clause must be represented in the
confirmed typed request, every identifier must resolve through current approved context, and the
same request plus context must compile deterministically. It is not a promise that ambiguous
language will be guessed correctly, that unapproved source fields will become executable, or that
every possible fifty-line SQL program is supported. An unresolved or unsupported request produces
no SQL.

## User outcome

The primary copy-first flow is:

1. enter a natural-language request, whether simple or advanced;
2. let SchemaBridge search the complete current governed registry for relevant approved concepts;
3. inspect the bounded interpretation preview: fields, metrics, predicates, grouping, aggregate
   filters, windows, ordering, tie policy, limit, selected models, joins, assumptions, and risks;
4. resolve any typed ambiguity and explicitly confirm the exact interpretation fingerprint;
5. receive normalized standalone PostgreSQL with no driver placeholders, ready to copy/download;
6. optionally request the existing bounded, read-only validation/execution lane as a separate
   action.

No compiler, SQL guard, copy renderer, cost preflight, or source executor is called before the
confirmation required by step 4.

## Governed context: complete search, bounded authority

“Knows the tables in the system” means retrieval traverses every current approved logical model
and field in the active registry scope. Lexical scoring uses model/field names, definitions, and
governed values, with role compatibility only as a bonus after a lexical hit. The bounded closure
then exposes and validates canonical types, roles, and allowed values. Deterministic server code
also reconstructs the connected closure and resolves/revalidates its current approved physical
mappings, transformation policies, join contracts, cardinalities, fanout mitigations, and
freshness bindings. Types and resolution facts are not free-text search tokens, and the complete
catalog is never copied into one model prompt.

For each request:

- mention extraction returns at most twelve source-grounded mentions;
- retrieval searches the complete approved semantic registry in the tenant/workspace scope;
- the executable/model-visible closure is capped at three logical models, twelve approved fields,
  and two approved joins;
- closure construction includes the approved join path and every field needed to express and
  validate the request;
- M32 retrieval reads only the approved logical registry; the separate M27 physical-discovery
  lane labels catalog-only results `needs_mapping_review`, and no such result can enter M32;
- a tied meaning, ambiguous join path, missing approved field, stale binding, unsupported
  operation, or closure that exceeds the query bounds produces a typed failure before SQL.

Catalog size and query size remain independent. A tenant may govern thousands of tables while one
M32 query still uses one connection, at most three physical tables, and at most two approved
joins.

## Natural-language boundary

The boundary is two-stage and authority-minimized:

1. **mention extraction** receives the untrusted request and returns only bounded purpose-labelled
   spans grounded in the exact input;
2. **typed interpretation** receives those mentions plus the approved 3/12/2 closure and returns
   either a version-1/version-2 analytical request or closed ambiguity codes.

Provider output contains no SQL, SQL fragment, physical table/column identifier, join predicate,
credential, tool call, approval, execution instruction, DataHub mutation, or arbitrary
explanation that can change policy. Server-side validation independently proves source-span
grounding, vocabulary membership, value/type compatibility, stage-correct alias references,
shape limits, join closure, and the request/context fingerprints.

The live provider path, when configured, must reuse M27's tenant opt-in, metadata-egress approval,
screening, admission, reservation, settlement, and sanitized audit boundary. Fake/recorded
acceptance remains key-free and cannot silently fall back from a live failure.

## Automatic v1/v2 selection

Routing is a deterministic representability decision after typed interpretation. It is never based
on prompt length, line count, keywords, language, or model confidence.

Use version 1 only when the request is fully representable by the existing flat contract:

- aggregate-mode dimensions/date grains;
- standard `COUNT`, `COUNT DISTINCT`, `SUM`, `AVG`, `MIN`, or `MAX` metrics with required fields;
- a flat conjunction of ordinary field filters;
- field-based ordering and the existing bounded limit;
- no conditional aggregate, bucket, aggregate predicate, window, output predicate, advanced alias,
  `OR`, `NOT`, row-count-without-field, or advanced grouping mode.

Use version 2 whenever any valid typed feature is not representable in version 1, including simple
row selection, `COUNT_ROWS`, boolean trees, conditional metrics, buckets, `HAVING`, window
calculations, output-alias filters/order, or advanced grouping. A long but v1-representable
request must remain v1; a short request such as “rank products by revenue” must route to v2.

Existing `AnalyticalRequest`, `ValidatedAnalyticalRequest`, `ResolvedSemanticPlan`, and
`QueryPlan(version=1)` payloads/fingerprints remain byte-stable. Version 2 uses separate request,
validation, resolution, and plan envelopes so advanced fields cannot be erased by historical
containers.

## Supported typed language

Version 2 supports:

- row and aggregate query modes;
- bounded `AND`, `OR`, and `NOT` predicate trees;
- `COUNT_ROWS`, `COUNT`, `COUNT DISTINCT`, `SUM`, `AVG`, `MIN`, and `MAX`;
- conditional aggregates through typed predicates;
- numeric buckets through a closed `CASE`-equivalent contract;
- aggregate predicates compiled as `HAVING`;
- `ROW_NUMBER`, `RANK`, `DENSE_RANK`, and `NTILE`;
- partition average and percentage of partition total;
- running `SUM`/`AVG` and bounded moving `SUM`/`AVG`;
- `LAG`, `LEAD`, delta, and percentage change;
- post-window predicates for top-N-per-group and equivalent bounded filters;
- final ordering over selected or derived aliases; `RANK`/`DENSE_RANK` may intentionally preserve
  peers, while `ROW_NUMBER`, `NTILE`, and order-sensitive value windows require a deterministic
  total order;
- at most two compiler-owned, non-recursive CTE stages, four derived window outputs, and eight
  window AST nodes;
- deterministic PostgreSQL pretty printing.

Predicate trees are capped at four levels, sixteen leaves, and eight operands per boolean node.
Window frames, offsets, tile counts, aliases, and ordering references are typed and bounded; users
cannot submit frame or expression text.

## Benchmark capability matrix

M32 uses the families illustrated in
[25 Ejemplos de Consultas SQL Avanzadas](https://learnsql.es/blog/25-ejemplos-de-consultas-sql-avanzadas/)
as a capability benchmark, not as permission to accept arbitrary SQL.

| Benchmark family | M32 status | Governed representation |
|---|---|---|
| Ranking, top/bottom/Nth and top-N per group | supported within bounds | `ROW_NUMBER`, `RANK`, `DENSE_RANK`, typed output predicate; peer-preserving rank or deterministic total order according to the selected operation |
| Quantiles/groups | supported within bounds | `NTILE` with a bounded positive tile count |
| Partition comparison/average | supported | partition average and typed derived output |
| Duplicate detection and grouped thresholds | supported | `COUNT_ROWS`, `COUNT`, `COUNT DISTINCT`, grouping and `HAVING` |
| Running totals and running averages | supported | fixed cumulative window frames |
| Moving totals and moving averages | supported | bounded compiler-owned moving frames |
| Previous/next value, delta and percentage change | supported | `LAG`, `LEAD`, delta, percent change with explicit ordering |
| Percent of group total | supported | typed percentage-of-total with guarded zero denominator |
| Conditional metrics | supported | typed conditional aggregate predicates |
| Numeric bands/buckets | supported | closed numeric bucket definitions |
| Cross/self-join combinations | rejected | repeated assets and Cartesian/self joins remain forbidden |
| Arbitrary scalar/correlated subqueries | rejected | no raw or user-shaped subquery node exists |
| `UNION`, `INTERSECT`, `EXCEPT` | rejected | set operations are outside the typed IR and guard allowlist |
| Recursive hierarchies | rejected | recursive/user-named CTEs remain forbidden |
| Gaps and islands | rejected in M32 | requires a separately reviewed typed pattern |
| `ROLLUP`/subtotal output | rejected in M32 | no claim until `GROUPING()` flags distinguish subtotal `NULL` from genuine governed `NULL` |

An unsupported family must return `unsupported_request`; it must never be approximated by a
different query.

## Compiler stages

The deterministic PostgreSQL compiler emits the smallest valid topology:

- direct version-1 or simple version-2 `SELECT` when no stage boundary is required;
- otherwise `aggregated`, with approved scans/joins, bounded `WHERE`, explicit projection,
  grouping, aggregates, and optional `HAVING`;
- optionally `windowed`, with explicit columns from `aggregated` plus closed window expressions;
- one final `SELECT`, with explicit columns, optional post-window predicate, deterministic final
  ordering, and one literal result limit.

PostgreSQL cannot filter a window alias in the same `SELECT`; the compiler-owned stage is therefore
semantic, not cosmetic. No stage uses `SELECT *`. CTE names, expression shapes, and window frames
are compiler-owned constants.

## Standalone copy artifact

Compilation first produces parameterized PostgreSQL plus typed bindings. The independent guard
reparses and accepts that executor form.

For copy/download only, a deterministic renderer:

1. walks the accepted SQL text with quote/dollar-quote awareness and numbers each `%s`
   placeholder as `$1`, `$2`, … in textual order;
2. reparses the numbered statement and replaces each numbered parameter with one typed SQLGlot
   literal node;
3. preserves `NULL`, boolean, integer, finite decimal, string, date, timestamp, and timestamp-with-
   time-zone semantics;
4. emits normalized PostgreSQL text;
5. reparses and applies the complete independent guard again with zero bindings.

AST traversal order is not used to associate values with placeholders. Non-finite numbers, NUL,
an unsupported value type, count mismatch, parse drift, or remaining parameter fails closed.

The artifact carries dialect, plan version, request/plan fingerprints, target identity where
applicable, SQL SHA-256, and `executed=false`. SQL text and embedded literals are transient
user-facing output and are forbidden from audit logs, telemetry, provider payloads, recipes, and
durable reports. Only bounded fingerprints and capability metadata may persist.

The standalone artifact never reaches the executor. The optional preview lane accepts only the
parameterized, guarded query and existing typed bindings.

## Independent guard additions

The final guard is scope-aware and validates:

- exactly one read-only outer `SELECT`;
- no recursive CTE, hidden subquery, set operation, `OFFSET`, wildcard, unsupported `DISTINCT`,
  aggregate `FILTER`, `QUALIFY`, named window, user-shaped grouping set, utility, DDL, or DML;
- at most two CTEs, three `SELECT` nodes, four derived window outputs/eight window AST nodes,
  three physical tables, and two joins;
- exact compiler-owned version-2 topology when staged;
- every physical and CTE column against its relation/output scope;
- no forward CTE references or unknown CTE output columns;
- function, aggregate, boolean operator, window argument, ordering, and frame allowlists;
- no repeated physical asset, Cartesian join, unknown asset/column, or unsafe fanout;
- one outer literal `LIMIT` equal to compiler metadata;
- exact parameter count for executor SQL and zero parameters for copy SQL.

## Reference acceptance request

Spanish:

> Para cada mes, en pedidos completados, calcula por categoría de producto los ingresos netos,
> unidades y pedidos distintos. Conserva solo las categorías con al menos 4 pedidos distintos;
> ordénalas por ingresos dentro de cada mes, desempatando alfabéticamente por categoría; asigna
> una posición única, calcula su porcentaje sobre los ingresos de las categorías elegibles del
> mes y el ingreso acumulado, y devuelve como máximo las tres primeras categorías de cada mes.

The exact typed interpretation is:

- primary entity: `SaleLine`;
- dimensions: month of `SalesOrder.ordered_at`, `Product.category`;
- metrics: `SUM(SaleLine.net_amount) AS net_revenue`,
  `SUM(SaleLine.quantity) AS units`, and
  `COUNT_DISTINCT(SalesOrder.order_key) AS distinct_orders`;
- `WHERE SalesOrder.order_status = COMPLETED`;
- `HAVING distinct_orders >= 4`;
- `ROW_NUMBER` partitioned by month and ordered by revenue descending/category ascending;
- percentage of eligible monthly revenue;
- running revenue in the same deterministic order;
- post-window `revenue_rank <= 3`;
- final month/rank/category ordering and limit 100.

The percentage denominator is explicitly the categories that survive `HAVING`.

## Acceptance criteria

- [x] Historical version-1 request/plan schemas, serialization, SQL, and fingerprints are
  byte-identical.
- [x] A long simple request routes to v1 and a short advanced request routes to v2; routing tests
  prove that length and keyword heuristics have no authority.
- [x] The complete governed registry is searchable while each interpretation closure remains at
  most 3 models/12 fields/2 joins and contains only current approved context.
- [x] M32 accepts only approved logical-registry context; the separate M27 physical lane keeps
  physical-only matches `needs_mapping_review`, while ambiguous, stale, over-limit, and
  unsupported M32 requests produce no SQL.
- [x] Mention extraction is source-grounded and capped at twelve; interpretation output has no SQL
  or physical-identifier field and cannot reference context outside the supplied closure.
- [x] Advanced request/plan constructors reject invalid stages, references, values, aliases,
  operation/type pairs, shape limits, frames, and SQL-like payloads.
- [x] Simple row, simple aggregate, and reference advanced Spanish requests produce their exact
  reviewed typed interpretations.
- [x] No compiler, guard, copy renderer, cost preflight, or source executor is invoked before exact
  fingerprint-bound confirmation.
- [x] The reference request resolves through exactly three approved datasets and two approved
  contracts.
- [x] Its compiler output is one PostgreSQL statement with two non-recursive CTEs, `HAVING`, three
  closed window calculations, a post-window filter, deterministic ordering, and `LIMIT 100`.
- [x] Every supported benchmark family above has a positive typed compiler/guard test; every
  rejected family has an explicit `unsupported_request` or SQL-policy regression.
- [x] `ROLLUP` is not advertised or accepted without reviewed `GROUPING()` output flags and
  `NULL`-preservation tests.
- [x] Standalone SQL contains no `%s`/`$n` placeholders, reparses, passes the independent guard
  with zero bindings, and has a stable reviewed SHA-256.
- [x] Primary acceptance reports `executed=false` and proves zero preview-executor calls.
- [x] Optional PostgreSQL integration, when deliberately selected, returns the five reviewed
  ground-truth rows through the read-only source role.
- [x] Adversarial tests reject recursive/extra/forward CTEs, unknown CTE columns, subqueries/set
  operations, forged windows/frames, excessive complexity, comments, multiple statements,
  unknown assets/columns, literal injection, placeholder reordering, and logging/persistence of
  copy SQL.
- [x] Streamlit and CLI make copy/download the primary result and label validation/execution as
  optional, separate, and disabled by default.
- [x] Operator documentation explains PostgreSQL-only portability and the difference between
  parameterized executor SQL and standalone copy SQL.
- [x] Relevant focused tests, integration/acceptance tests, and `make check` pass on final bytes.

## Reference ground truth

The synthetic corpus must return:

| month | category | net revenue | units | distinct orders | rank | percent | cumulative |
|---|---:|---:|---:|---:|---:|---:|---:|
| 2026-01-01 | BOOKS | 1496.45 | 25 | 9 | 1 | 37.85 | 1496.45 |
| 2026-01-01 | SPORTS | 988.80 | 16 | 6 | 2 | 25.01 | 2485.25 |
| 2026-01-01 | ELECTRONICS | 938.65 | 23 | 7 | 3 | 23.74 | 3423.90 |
| 2026-02-01 | BOOKS | 988.00 | 16 | 5 | 1 | 60.77 | 988.00 |
| 2026-02-01 | HOME | 637.80 | 12 | 4 | 2 | 39.23 | 1625.80 |

## Manual test

Use the final M32 application boundary through the two named operations
**prepare natural-SQL preview** and **confirm preview / generate copy artifact** with:

1. one simple projection/filter request;
2. one long but v1-representable aggregate request;
3. the reference advanced Spanish request;
4. one ambiguous field request;
5. one explicitly unsupported recursive/gaps-and-islands request.

For the first three, inspect the complete typed interpretation before confirmation and verify that
no SQL exists. Confirm the exact fingerprint, then verify `dialect=postgresql`,
`executed=false`, the selected v1/v2 route, normalized SQL SHA-256, and downloadable standalone
SQL with no placeholders. Reparse the downloaded file independently. The ambiguity and
unsupported cases must produce no SQL.

For the CLI, prefer `sql-from-natural --review-and-confirm`: it prints and confirms one exact
in-memory preview without repeating the language stages. The two-invocation
`--confirm-fingerprint` form deliberately re-prepares and fails closed if live interpretation
changes.

Run the separate optional integration action only against the synthetic read-only PostgreSQL
service and compare all five advanced rows. Record the eventual entrypoint name/flags and every
result in the M32 handoff after that interface is final; do not invent or predeclare a passing
command.

## Non-goals and handoff limits

M32 does not claim:

- guaranteed correctness without exact context and human confirmation;
- support for every possible query or every example's unrestricted SQL syntax;
- `ROLLUP`/subtotal safety before explicit `GROUPING()` flags exist;
- portability across PostgreSQL, MySQL, SQL Server, BigQuery, Snowflake, or other dialects;
- portability to another database merely because it has schemas/tables with the same names; the
  artifact is for the governed PostgreSQL database/context shown in the preview;
- automatic semantic approval from a name, definition, search score, or model answer;
- production evaluation, penetration testing, pilot, release, or GA readiness;
- authorization to write source databases or DataHub.
