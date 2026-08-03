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
timed first-use construction; its checkout/artifact subject was PR merge ref
`792e1e7f1ec0c2eed75dfba08302304f17d9c9ff`. Follow-up run `30811612188`, whose branch head is
`c7e72cc97e4226b2d953f5c1e8ef55178a1598f5` but whose checkout subject is PR merge ref
`01933509e22759886349451dc1e3a66751453ef2`, proved that one main-thread warmup was
insufficient: its four-latency outlier pattern left p95 at 393.774 ms while p99 remained within
budget at 417.752 ms and 3,930 other tests passed, consistent with one cold first read per worker.
The current bytes therefore warm every executor worker, in the same pool, before measurement
without changing the 250/500 ms limits. Both hosted runs are useful PR evidence, but neither merge
subject is an exact clean tagged-main M30 candidate.

Local final implementation gates pass: the post-hosted correction's `make check` completed 3,931
tests with 246 explicit deselections in 1,235.97 seconds, the runtime wheel validated migrations
1–15 and all entrypoints, and independent post-remediation review reported P0=0, P1=0 and P2=0.
The correction requires its own commit-bound hosted run, whose result must be read from draft PR
#1. These remain preparation facts, not operated M30 acceptance.

Phase 0 is not M30 acceptance. There is no clean tagged `main` candidate, 1,000-case blind corpus,
target-environment evidence, independent pentest, operated browser/accessibility matrix, scale/
soak campaign or candidate-specific owner signatures. Repository and synthetic evidence must not
be relabeled to fill those gaps.

## Previous milestone snapshot — M35

M35 remains complete and accepted locally under D129. Its schema-v15 join/model-change lifecycle,
M34 publication handoff, PostgreSQL/HTTP/acceptance/browser/full gates and exact commercial limits
are recorded in `tasks/M35_HANDOFF.md`. Its local DataHub receipt remains simulated and the active
pointer is not changed.
