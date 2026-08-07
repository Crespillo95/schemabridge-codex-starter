# M32 implementation prompt

Implement `plans/M32_ADVANCED_COPYABLE_SQL.md` as one governed vertical milestone.

The primary result is an exact, confirmed simple-or-advanced natural-language request converted
into standalone copyable PostgreSQL. Execution is optional and separately authorized; the normal
flow performs zero source execution. Preserve every existing invariant: a model emits only bounded
typed data, deterministic code resolves and compiles it, ambiguity requires confirmation, final
SQL is independently reparsed, and no source write is possible.

Search the complete current governed registry, but expose to interpretation only the relevant
approved closure of at most three models, twelve fields, and two joins. Unapproved physical
matches remain `needs_mapping_review`. Keep mention extraction grounded in exact source spans and
bounded to twelve mentions. Never place a full catalog export, source rows, raw samples, SQL,
parameters, credentials, or private physical identifiers in model input or output.

Keep version-1 payloads, fingerprints, and compiled behavior byte-stable. Add separate version-2
request/validation/resolution/plan contracts. Select v1 or v2 only by exact representability after
typed interpretation—never by prompt length, line count, language, keywords, or confidence. Prove
with a long simple v1 case and a short advanced v2 case.

Version 2 may contain only the closed, bounded operations in the plan: boolean trees, row count,
conditional aggregates, numeric buckets, `HAVING`, rankings/`NTILE`, partition average/percent,
running/moving aggregates, `LAG`/`LEAD`, delta/percent change, post-window predicates, final alias
ordering, and at most two compiler-owned CTEs/four windows. Do not admit raw SQL, arbitrary
expressions, user CTEs, recursion, arbitrary subqueries, set operations, cross/self joins, more
than three tables/two joins, or another dialect.

Treat the LearnSQL advanced-query article as a benchmark matrix, not an arbitrary-SQL contract.
Provide explicit positive coverage for the supported families and typed rejection for cross/self
joins, arbitrary subqueries, set operations, recursion, and gaps/islands. Do not advertise or
accept `ROLLUP` until the output includes reviewed `GROUPING()` flags that distinguish subtotal
`NULL` from genuine governed `NULL`.

The M32 use-case boundary has two explicit operations:

- `prepare natural-SQL preview`: retrieve and validate context and return the typed interpretation
  plus ambiguities/fingerprint; it never compiles or exposes SQL;
- `confirm preview / generate copy artifact`: revalidate current context and exact fingerprints,
  resolve, compile, guard, render, and guard again; it never executes.

Produce two SQL forms only after exact confirmation:

- parameterized guarded SQL, which remains the only possible executor input;
- standalone copy SQL, rendered from that accepted statement and typed values, guarded again with
  zero bindings, and exposed only as transient user output.

Bind placeholders to values by stable textual parameter index, not AST traversal order. The copy
artifact must contain no remaining placeholder and must carry dialect, plan version, semantic
fingerprints, SQL SHA-256, and `executed=false`. Never persist or log its SQL or embedded literals.

Prove the exact Spanish reference request, reviewed golden SQL/hash, simple-query behavior, no SQL
before confirmation, zero execution in the primary path, optional five-row PostgreSQL ground
truth, adversarial failures, focused tests, and the full quality gate. Update product, scope,
architecture, security, pipeline, testing, evaluation, runbook, browser acceptance, README,
state, decision log, and current task without rewriting M27–M29 historical evidence or claiming
tests that were not run.
