# Current task

- Current milestone: M35 — Governed registry-v2 change lifecycle
- Status: complete; accepted locally
- Prompt: `prompts/M35_REGISTRY_V2_CHANGE_LIFECYCLE.md`
- Plan: `plans/M35_REGISTRY_V2_CHANGE_LIFECYCLE.md`
- ADR: `docs/adr/0018-governed-registry-v2-change-lifecycle.md`
- Handoff: `tasks/M35_HANDOFF.md`
- Production/release GO: **NO**

## Objective delivered

Evolve one exact active registry-v2 without editing fixtures or existing DataHub documents: either
add one explicitly approved same-connection join or replace/remediate one model with complete
mapping, binding, dependency/M26 and incident-join authority. The result reuses M34 publication,
finishes at `activation_ready` and leaves activation to the separate M23 approval/CAS path.

## Final evidence and retained boundaries

- Phase A profiles only aggregate key evidence and rejects self/cross-connection/many-to-many or
  unmitigated fanout before a proposal exists.
- Phase B consumes one immutable M33 source and exact active base/dependency/M26 authority, then
  preserves, freshly upserts or explicitly removes every incident join.
- Schema v15 persists bounded tenant history, exact replay, crash-safe requested-job binding,
  fenced claims and closed `add_join_v1`/`replace_model_v1` publication sources.
- HTTP derives actor/workspace from authentication, minimizes lists, masks cross-tenant existence
  and exposes no source/DataHub/activation credential.
- The publisher revalidates the complete authority closure; the replacement witness rejects
  model, mapping, physical-binding or incident-join tampering before handoff.
- Fresh PostgreSQL proves the complete replacement vertical through `activation_ready`, exact M23
  handoff and unchanged pointer; all 88 v15 tables are visible to the read-only backup role.
- Browser Phase A covers the safe journey and four hostile cases at desktop/mobile with zero
  source writes, SQL, rows or credentials.
- Final evidence passes 165 focal unit, 17 HTTP, 6 M34/M35 PostgreSQL and 7 acceptance tests plus
  Ruff, mypy over 357 source files and the full 3,881-test gate with 245 explicit deselections.

## Commercial status

M35 closes the bounded local PostgreSQL registry-v2 change lifecycle only. The Phase B DataHub
receipt is simulated and activation is deliberately not executed. Operated M29 controls, live
DataHub/IAM/secrets, blind bilingual M30 quality and independent security verification, M31 pilot,
large-scale customer evidence, legal/service controls and certification per future SQL dialect
remain mandatory. Commercial/production/release status is **NO-GO**.

---

## Previous milestone snapshot — M34

M34 remains accepted locally under D128. It supplies the isolated immutable publication/read-back
queue and activation-ready handoff reused by M35; it never activates a registry.

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
