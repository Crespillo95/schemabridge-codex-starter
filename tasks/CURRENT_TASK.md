# Current task

- Current milestone: M34 — Governed registry publication and activation handoff
- Status: complete; accepted locally
- Prompt: `prompts/M34_GOVERNED_REGISTRY_PUBLICATION.md`
- Plan: `plans/M34_GOVERNED_REGISTRY_PUBLICATION.md`
- ADR: `docs/adr/0017-governed-registry-publication.md`
- Handoff: `tasks/M34_HANDOFF.md`
- Production/release GO: **NO**

## Objective delivered

Publish one exact M33 `ready_for_publication` proposal as an immutable DataHub registry-v2
document through a tenant-bound durable queue and isolated writer identity. Publication requires a
fresh approval of the complete assembled candidate, independent exact read-back, and finishes at
`activation_ready`; a separate M23 approval/CAS path performs activation.

## Final evidence and retained boundaries

- Registry v2 preserves exact workspace, connection, catalog generation/vector, physical locator,
  opaque observed DataHub URN and metadata fingerprints for every active mapping.
- Schema v14 reserves one target, stores bounded payloads/events, uses database-time leases,
  capability digests, fencing, retry/dead-letter and a pre-write cooperative cancellation boundary.
- API can submit, inspect, authorize and cancel but has no writer secret; the publisher has no
  source/LLM/OIDC/activation credential and cannot update active pointers.
- DataHub success requires exact typed read-back of candidate, authorization, audit and observed
  related assets. An absent target is explicit; ambiguity, altered content and partial privilege
  responses fail closed.
- M23 loads only an exact `activation_ready` v2 handoff and atomically rechecks retained catalog
  authority under the workspace lock before pointer CAS. Rollback/reconciliation remain separate.
- Required M34 matrix passes 118 tests with 5 explicit live-DataHub skips; fresh PostgreSQL
  publication/M23/observer regressions pass 17 tests and deployment/runtime tests pass 215.
- Internal-browser evidence covers the exact queued → approval → activation-ready path with one
  immutable write, one version, opaque URN, unchanged pointer and no browser errors. One advanced
  M32 Spanish request also produced a twice-guarded 106-line standalone PostgreSQL query without
  execution.
- The final full gate passes supply-chain/release policy, Ruff, mypy over 343 source files and
  3,779 unit tests with 238 explicit deselections. Independent review and exact commands are
  recorded in `tasks/M34_HANDOFF.md`.

## Commercial status

M34 closes bounded local publication/read-back and the activation-ready bridge only. Live DataHub
evidence on these exact bytes, live-provider/holdout quality, multi-dialect output, independent
security verification, operated SLO/scale evidence, M30 and M31 remain mandatory.

---

## Previous milestone snapshot — M33

M33 remains accepted locally under D126. It supplies the exact tenant/catalog-bound,
`ready_for_publication` proposal consumed by M34 and still performs no external write or
activation.

---

## Previous milestone snapshot — M32

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
