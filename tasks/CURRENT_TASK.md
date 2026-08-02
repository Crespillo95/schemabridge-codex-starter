# Current task

- Current milestone: M33 — Generic governed semantic onboarding
- Status: complete; accepted locally
- Prompt: `prompts/M33_GENERIC_SEMANTIC_ONBOARDING.md`
- Plan: `plans/M33_GENERIC_SEMANTIC_ONBOARDING.md`
- ADR: `docs/adr/0016-generic-semantic-onboarding.md`
- Handoff: `tasks/M33_HANDOFF.md`
- Production/release GO: **NO**

## Objective delivered

Allow an authenticated tenant to create a semantic onboarding draft from an exact retained catalog
generation, record explicit model/mapping decisions, and prepare one immutable audited
`ready_for_publication` proposal without editing repository fixtures or performing an external
write. M33 deliberately stops before publication and activation.

## Final evidence and retained boundaries

- Exact authenticated preflight, workspace/connection/generation/vector/locator/fingerprint/type
  binding, schema-v13 storage, CAS and append-only idempotency pass locally.
- Confidence/name similarity never approve; a steward decides every semantic fact and a distinct
  publisher prepares the immutable handoff.
- Identity rotation preserves only verified historical workspace/actor pairs; ambiguous lineage
  and cross-coordinate replay fail closed.
- 225 independently reviewed unit cases, 7 PostgreSQL integrations, 2 acceptance cases, Ruff,
  mypy and `git diff --check` pass; independent review reports P0=0/P1=0.
- Codex internal-browser desktop and 390×844 acceptance passes with no overflow or console error,
  zero SQL/external writes and no publication, activation or execution control.
- `make check` passes 3,682 tests with 233 deselections; the exact final repeat is recorded in the
  handoff.

## Commercial status

M33 closes generic semantic authoring locally only. M34 dedicated publication/readback plus M23
activation, M30 evaluation/security verification, and M31 operated pilot/GA remain mandatory.

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
