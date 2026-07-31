# Current task

- Current milestone: M32 — Simple and advanced natural language to copyable PostgreSQL
- Status: complete and accepted locally for the bounded deterministic/synthetic capability;
  manual browser and all production/release gates remain open
- Prompt: `prompts/M32_ADVANCED_COPYABLE_SQL.md`
- Plan: `plans/M32_ADVANCED_COPYABLE_SQL.md`
- ADR: `docs/adr/0015-advanced-copyable-postgresql.md`
- Evaluation: `reports/m32-copyable-sql-deterministic-evaluation.md`
- Handoff: `tasks/M32_HANDOFF.md`
- Production/release GO: **NO**

## Objective delivered

Turn simple or advanced Spanish analytical requests into an exact reviewed typed interpretation
and, only after explicit confirmation, one deterministic standalone PostgreSQL statement for a
PostgreSQL client connected to the same governed database/context. The primary flow never executes
the copy artifact.

## Implemented locally

- Complete approved logical-registry traversal with a bounded 3-model/12-field/2-join closure.
- Two-stage language boundary: source-grounded mentions followed by strict typed v1/v2 intent;
  neither provider schema can emit SQL or physical identifiers.
- Semantic routing by representability, including a long simple v1 case and short ranking v2 case.
- Closed advanced language for boolean predicates, aggregates, buckets, `HAVING`, fourteen window
  operations, output filters/order, four derived window outputs, and eight window AST nodes.
- Deterministic semantic resolution before preview signing and re-resolution after confirmation,
  with selected datasets, mapping/join confidence/evidence/risks, fanout facts, and fingerprints.
- Parameterized PostgreSQL compilation, independent scope-aware AST guard, typed standalone
  literal rendering, and a second zero-binding guard.
- Copy-first Streamlit and CLI surfaces; optional validation/execution is separate and disabled by
  default. The copy artifact is `executed=false` and cannot reach the executor.
- Deliberate no-SQL outcomes for ambiguity, stale context, unapproved physical-only context,
  cross/self joins, arbitrary subqueries, set operations, recursion, gaps/islands, and `ROLLUP`.

## Final local evidence

| Gate | Result |
|---|---|
| M32 targeted unit/acceptance/read-only integration | **PASS** — 304 passed in 22.38 s |
| Spanish governed retrieval corpus | **PASS** — 14/14 |
| Simple/route-control Spanish end-to-end journeys | **PASS** — 4/4 |
| Advanced reference standalone SQL | **PASS** — SHA-256 `ec589a1527d0d641f4f7f7eb7e052ca1ace5b092013b437da72542ce4145d4f3` |
| Optional PostgreSQL integration | **PASS** — 1/1, five exact rows through the read-only role |
| Automated Streamlit acceptance | **PASS** — 3/3 |
| Focused Ruff/mypy | **PASS** |
| `make check` | **PASS** — 3,587 passed, 224 deselected; repeated successfully after final state/handoff updates |
| `git diff --check` | **PASS** |
| Manual in-app browser matrix | **NOT RUN** — browser list was empty; operator test remains documented |

## Retained boundaries

- The deterministic fixtures are not a live-provider or holdout-quality claim.
- PostgreSQL is the only output dialect and the destination is the same governed database/context.
- M32 consumes only approved logical context. Physical-only discovery remains the separate M27
  `needs_mapping_review` lane.
- It does not support arbitrary SQL, every example in the public benchmark, or error-free
  interpretation of every natural-language request.
- M30/M31, live-provider production evaluation, independent security verification, operated
  release controls, pilot, and GA remain separate NO-GO gates.
