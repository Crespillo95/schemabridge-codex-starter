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

M30 Phase 0 commit `c7e72cc97e4226b2d953f5c1e8ef55178a1598f5` and per-worker warmup commit
`3f57a2ea89220ff0c68ac58f0f8e668069e93f81` are published on draft PR #1. Run
`30816314566` is branch-associated with the latter but checked out PR merge ref
`04402da8327f08ccc29799a6df4b4f2e9645dc90`, not an exact clean tagged-main M30 candidate.
GitGuardian and supply-chain passed. Quality passed 3,930 other tests with zero load errors, then
the 64-read synthetic smoke reported p50/p95/p99/max
26.420/330.102/355.132/355.132 ms and failed only the unchanged 250 ms p95 budget;
`postgres-integration` passed in 1 h 37 min 59 s. The run is therefore red only because of that
quality failure.

That second correlated tail remained after all four executor workers were preconditioned, refuting
the earlier cold-worker explanation without proving GC, GIL, scheduler or product causality. With
nearest-rank percentiles over 64 reads, a tail compatible with the synchronized four-worker launch
can occupy 6.25% of the sample: p95 is the fourth-largest value and p99 is the maximum. D131
therefore runs only the exact performance node in a fresh service-free Pytest process and increases
the synthetic sample to 1,000 concurrency-four reads while retaining per-worker preconditioning and
the unchanged 250/500 ms limits. At least 51 observations over 250 ms then fail p95 and at least
11 over 500 ms fail p99. Four correlated transients are only 0.4%; no observation is discarded and
sanitized reports expose both tail counts.
Coverage and correctness targets explicitly exclude this wall-clock node. The 5,000-read
concurrency-16 PostgreSQL benchmark remains the authoritative operated scale gate.

The published `3f57a2e` bytes passed a full local `make check` with 3,931 tests and 246 explicit
deselections in 1,235.07 seconds. On the current D131 bytes, the exact isolated 1,000-read node
passes in 2.72 seconds; a direct run records p50/p95/p99/max
9.775/10.761/12.288/19.260 ms with all 1,000 observations accounted for and zero over both budgets.
The 23 non-performance harness tests include tracked-format-v2 rendering compatibility. Combined,
scale/release/evaluation tests pass 42, and `make test-scale-correctness` passes 29 tests with 1
deselection followed by 8 tests with 4 deselections. Final `make check` passes the isolated node
plus 3,933 functional tests with 247 explicit deselections; supply-chain, release audit, Ruff and
mypy also pass. Only the commit-bound hosted rerun remains pending at this snapshot. The runtime
wheel previously validated migrations 1–15 and all entrypoints, and independent post-remediation review
reported P0=0, P1=0 and P2=0. These are preparation facts, not operated M30 acceptance.

Phase 0 is not M30 acceptance. There is no clean tagged `main` candidate, 1,000-case blind corpus,
target-environment evidence, independent pentest, operated browser/accessibility matrix, scale/
soak campaign or candidate-specific owner signatures. Repository and synthetic evidence must not
be relabeled to fill those gaps.

## Previous milestone snapshot — M35

M35 remains complete and accepted locally under D129. Its schema-v15 join/model-change lifecycle,
M34 publication handoff, PostgreSQL/HTTP/acceptance/browser/full gates and exact commercial limits
are recorded in `tasks/M35_HANDOFF.md`. Its local DataHub receipt remains simulated and the active
pointer is not changed.
