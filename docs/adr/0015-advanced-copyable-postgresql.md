# ADR 0015: Representability-routed typed queries and standalone PostgreSQL artifacts

- Status: accepted for M32 implementation
- Date: 2026-07-30

## Context

SchemaBridge produces parameterized PostgreSQL for bounded read-only preview execution. Its
historical natural-language and guided request contracts are flat: filters are implicitly
conjoined, and aggregate filtering, conditional metrics, staged CTEs, and window calculations are
outside that version-1 language. The existing SQL presentation can contain driver placeholders and
hidden bindings, so it is not necessarily one autonomous query that a user can paste into another
SQL client.

The product direction is copy-first: the user wants both simple and advanced natural-language
requests converted into exact standalone SQL, with execution inside SchemaBridge as optional,
separately authorized validation.

Adding raw SQL generation to the model would violate the central compiler and safety boundary.
Adding optional fields to existing frozen request models would also alter historical
serialization and fingerprints.

“Give the system all table context” creates a separate scaling and authority question. Search must
cover the complete current governed registry, but a model and one executable plan must not receive
an unbounded catalog dump. Physical discovery also cannot silently become semantic approval.

Selecting an “advanced” path by prompt length or words such as “rank” is not semantically sound. A
long request can be completely representable by version 1, while a two-word ranking request
requires a window operation.

## Decision

Search the complete current approved registry, then construct one deterministic bounded closure of
at most three logical models, twelve approved fields, and two approved joins. The closure includes
the current logical definitions, roles, types, governed values, and join
cardinality/fanout summaries needed by the interpreter. Deterministic server-side resolution later
reloads and validates the current approved mappings, transformations, complete join contracts,
evidence, risks, and freshness bindings. The preview shows mapping and join confidence/evidence/
risks; transformation identity remains bound indirectly by the resolved-plan fingerprint rather
than exposed as a standalone review row. A physical field without a current approved mapping remains in M27's separate
`needs_mapping_review` discovery lane and is not an M32 retrieval candidate; ambiguity, staleness,
missing context, and over-limit closure fail before compilation.

Introduce parallel version-2 typed request, validation, resolution, and query-plan contracts. They
add only bounded closed operations: boolean predicates, row count, conditional aggregates, numeric
buckets, aggregate predicates, compiler-owned staged CTEs, rankings/tiles, partition/running/moving
calculations, offsets/deltas/percentages, post-window filtering, and final alias ordering.

The natural-language boundary has two typed provider stages: source-grounded mention extraction
and interpretation against the bounded approved closure. It may emit only a version-1/version-2
intent or closed ambiguity codes and cannot emit SQL, physical identifiers, join predicates,
tools, approvals, or execution instructions.

After interpretation, the server selects the smallest request version that can represent the
complete meaning. Version 1 is used only for its historical flat field/aggregate/filter/order
language. Version 2 is used for every valid feature outside that language. Prompt length,
keywords, language, and confidence have no routing authority.

The use-case API is split:

- prepare a natural-SQL preview: retrieve, interpret, validate, and deterministically resolve
  approved datasets/mappings/joins/fanout into review evidence; never compile or expose SQL;
- confirm the exact preview and generate a copy artifact: reload/revalidate context and
  fingerprints, re-resolve, compile, guard, render, and guard again; never execute.

The PostgreSQL compiler remains deterministic. It emits parameterized SQL first, and the
independent guard reparses and validates that form. Simple shapes use a direct `SELECT`; shapes
that require aggregate/window evaluation boundaries use at most the compiler-owned `aggregated`
and `windowed` CTEs plus one final `SELECT`.

For copy/download, a separate deterministic renderer replaces placeholders with typed SQLGlot
literal nodes, emits PostgreSQL, and submits the standalone form to the same guard with zero
bindings. Placeholder/value association follows stable textual parameter indexes, not AST
traversal order. This renderer is not a compiler extension point and accepts no text expression.

Only parameterized SQL may reach the preview executor. Standalone SQL is transient presentation
output. Audit and telemetry may retain its SHA-256 and public capability metadata, never its SQL
or literal values.

PostgreSQL remains the sole supported dialect. Other engines require distinct compiler, guard,
literal-rendering, and acceptance work.

The artifact names the governed `schema.table` closure but is not bound to an arbitrary destination
database. It is intended for the same governed PostgreSQL database/context shown in the preview.
An unrelated database with homonymous schemas is not interchangeable. Approved PostgreSQL
physical identifiers are currently unquoted-canonical lowercase ASCII names within the 63-byte
server limit; quoted/mixed-case physical identifiers require a future end-to-end contract.

M32 explicitly supports bounded ranking/top-N/`NTILE`, partition averages, duplicate/group
threshold queries, running/moving calculations, `LAG`/`LEAD`, delta/percent change, conditional
metrics, and numeric buckets. It explicitly rejects cross/self joins, arbitrary subqueries, set
operations, recursion, and gaps/islands. `ROLLUP` remains rejected until a future contract exposes
reviewed `GROUPING()` flags so subtotal `NULL` is distinguishable from a genuine governed `NULL`.
Unsupported families are reported; they are never approximated.

## Consequences

- Users can copy one autonomous, twice-guarded PostgreSQL statement.
- Simple requests retain their existing version-1 semantics where exactly representable.
- Advanced requests can produce statements spanning tens of formatted lines while remaining a
  closed semantic language; line count is neither a capability nor a correctness boundary.
- Retrieval can use all approved registry context without giving one model or plan unbounded
  authority.
- Version-1 payloads and fingerprints remain stable.
- SQL generation can complete without a source connection after approved semantic context is
  available.
- Natural-language ambiguity, unsupported intent, stale evidence, or unapproved context still
  produces no SQL.
- The guard must understand CTE output scope and exact version-2 topology.
- Copy SQL intentionally contains user-confirmed literal filter values and therefore must not be
  persisted or logged.
- The feature does not support arbitrary SQL or claim cross-dialect portability.
- Query complexity is determined by typed semantics, not text length.

## Rejected alternatives

- Let the model emit SQL directly: rejected because SQL would bypass typed semantic validation and
  deterministic compilation.
- Export `%s` placeholders plus a separate values list as the primary artifact: rejected because
  it is not directly executable in a normal SQL editor.
- Interpolate strings into SQL text: rejected because escaping and type semantics would become a
  second unsafe compiler.
- Add advanced defaults to version-1 models: rejected because historical fingerprints and stored
  payload semantics would change.
- Route by keyword, prompt length, or requested line count: rejected because these are not
  representability proofs.
- Send the whole catalog to the model: rejected because it is unbounded, leaks unnecessary
  metadata, and confuses retrieval evidence with executable authority.
- Treat a physical search result as an approved field: rejected because name/definition similarity
  cannot establish semantic equivalence.
- Treat SQLGlot transpilation as multi-dialect support: rejected because semantic and safety
  equivalence requires per-dialect proof.
- Enable `ROLLUP` without `GROUPING()` output flags: rejected because subtotal `NULL` would be
  observationally ambiguous with an approved source `NULL`.
