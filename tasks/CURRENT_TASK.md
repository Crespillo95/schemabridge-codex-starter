# Current task

- Current milestone: M30 — Production evaluation and security verification
- Status: Phase 0 candidate-readiness implemented locally; campaign blocked on external inputs
- Plan: `plans/M30_PRODUCTION_EVALUATION_SECURITY.md`
- Machine contract: `plans/M30_CAMPAIGN_CONTRACT.yml`
- Handoff: `tasks/M30_HANDOFF.md`
- Production/release GO: **NO**

## Phase 0 objective

Freeze the exact PostgreSQL copy-first commercial boundary and provide an offline, deterministic,
fail-closed preflight before any blind campaign or security claim. The preflight binds Git HEAD,
tree/source/lock/build/workflow/migration/contract digests, schema v15 and candidate tag/branch
facts, then keeps every hosted, operated, independent and owner gate explicitly external.

## Implemented locally

- Strict YAML contract for the 1,000-case bilingual minimum: 200 simple, 300 advanced, 150
  ambiguous, 150 unsupported and 200 adversarial/security cases, split exactly 500 ES/500 EN.
- Exact zero-tolerance and quantitative thresholds, PostgreSQL typed-plan-v2 request/context/
  preview/timeout/`NULL`/fanout limits, closed SQL-family matrix and 24 external controls.
- Pure domain readiness models, application use case and Git/file adapters with deterministic JSON
  and Markdown reports.
- Repository gates for clean tree, `main`, exactly one annotated `v{package_version}` tag,
  committed contract, requirements/workflows, source-tree and migrations 1–15.
- External evidence classes that cannot pass from local fixtures or PR merge refs.
- CLI/Make workflow that performs zero network, database, DataHub or source writes and currently
  returns `blocked_prerequisites`, `campaign_executable=false`, `release_decision=no_go`.
- Focused unit/acceptance tests cover contract weakening, duplicate/extra YAML, dirty/non-main/
  untagged candidates, uncommitted contract drift, self-asserted evidence rejection and stable
  report bytes.

## Current evidence and boundary

The M35 commit `11c2f8527d3e935cb44292fb58f8c23b9611b8eb` is present on draft PR #1. Hosted
run `30804919220` passed supply-chain, GitGuardian and PostgreSQL integration. Its quality job
passed 3,880 tests and then failed one warm-cache synthetic p95 check because the isolated test
timed first-use construction; the current M30 bytes add an explicit unmeasured warmup without
changing the 250/500 ms limits. The hosted artifact is bound to PR merge ref
`792e1e7f1ec0c2eed75dfba08302304f17d9c9ff`, so it is useful PR evidence but not an exact M30
candidate subject.

Local final implementation gates pass: `make check` completed 3,931 tests with 246 explicit
deselections, the runtime wheel validated migrations 1–15 and all entrypoints, and independent
post-remediation review reported P0=0, P1=0 and P2=0. These remain local preparation facts, not
hosted or operated M30 acceptance.

Phase 0 is not M30 acceptance. There is no clean tagged `main` candidate, 1,000-case blind corpus,
target-environment evidence, independent pentest, operated browser/accessibility matrix, scale/
soak campaign or candidate-specific owner signatures. Repository and synthetic evidence must not
be relabeled to fill those gaps.

## Previous milestone snapshot — M35

M35 remains complete and accepted locally under D129. Its schema-v15 join/model-change lifecycle,
M34 publication handoff, PostgreSQL/HTTP/acceptance/browser/full gates and exact commercial limits
are recorded in `tasks/M35_HANDOFF.md`. Its local DataHub receipt remains simulated and the active
pointer is not changed.
