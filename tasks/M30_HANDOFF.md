# Milestone handoff

## Summary

- Milestone: M30 — Production evaluation and security verification, Phase 0 only
- Status: partial; candidate-readiness prepared locally, campaign blocked
- Recommended operator decision: accept the fail-closed preparation vertical; retain M30,
  commercial, production and release NO-GO
- Published implementation commit: `c7e72cc97e4226b2d953f5c1e8ef55178a1598f5`
- Published warmup commit: `3f57a2ea89220ff0c68ac58f0f8e668069e93f81`
- Final corrective commit message: `fix: isolate scale percentile gate`

## Implemented

- Added the exact PostgreSQL typed-plan-v2 copy-first machine contract: 500 Spanish plus 500
  English blind cases, reviewed SQL-family matrix, request/context/preview/timeout/`NULL`/fanout
  limits, release thresholds and 24 external evidence controls.
- Added pure candidate/readiness models and an application use case that cannot pass external
  controls from offline evidence.
- Added Git/file adapters for stable HEAD/tree/index/source/lock/build/workflow/migration/contract
  identities, exact `v{package_version}`/main/clean checks and deterministic JSON/Markdown. Git
  replace refs, inherited Git configuration, hidden/sparse index state and symlink/gitlink blobs
  fail closed.
- Added a CLI and `make m30-readiness`; the retention switch changes only exit status and never the
  NO-GO report.
- Added mutation, identity, self-asserted-evidence, deterministic-writer and acceptance tests.

## Files changed

- `plans/M30_CAMPAIGN_CONTRACT.yml`: exact machine-readable commercial/evaluation boundary.
- `src/schemabridge/domain/production_readiness.py`: immutable contract, candidate, gate and report
  invariants.
- `src/schemabridge/application/production_readiness.py` and
  `src/schemabridge/application/ports/production_evidence.py`: offline use case and ports.
- `src/schemabridge/adapters/evaluation/m30_readiness.py`: strict YAML, Git candidate and report
  adapters.
- `scripts/m30_readiness.py` and `Makefile`: operator command.
- `src/schemabridge/bootstrap.py`: composition-root builders used by the operator command.
- `scripts/benchmark_catalog_scale.py`, `Makefile` and `tests/unit/test_scale_harness.py`: explicit
  unmeasured preconditioning per executor worker plus an exact fresh-process, 1,000-read synthetic
  percentile gate, without relaxing its 250/500 ms budgets. Reports identify nearest-rank and
  disclose exact tail counts; correctness and coverage targets exclude the wall-clock node. The
  5,000-read PostgreSQL benchmark remains authoritative operated evidence.
- `tests/unit/test_m30_readiness.py`, `tests/acceptance/test_m30_readiness_acceptance.py` and
  `tests/m30_readiness_support.py`: focused and observable regressions.
- M30/commercial/runbook/test/state/decision documentation: accurate Phase 0/no-GO boundary.

## Commands executed

| Command | Result | Notes |
|---|---|---|
| `.venv/bin/ruff format --check src tests scripts` | pass | 725 files formatted |
| `.venv/bin/ruff check src tests scripts` | pass | Zero findings |
| `.venv/bin/mypy src` | pass | 361 source files |
| `.venv/bin/pytest -q tests/unit/test_m30_readiness.py tests/acceptance/test_m30_readiness_acceptance.py` | pass | 49 passed |
| `.venv/bin/pytest -q tests/unit/test_scale_harness.py tests/unit/test_release_audit.py tests/unit/test_evaluation.py` | pass | 39 passed; 250/500 ms budgets unchanged |
| `.venv/bin/python scripts/release_audit.py --json` | pass | 1,030 candidate files and 23 dependency licenses; expected dirty-tree warning only |
| `make m30-readiness` | pass with explicit NO-GO | Pre-commit run: four repository failures and 24 external controls missing; zero network/database/DataHub/source actions |
| `make check` (initial attempt) | intentionally interrupted | 2,513 passed and 246 deselected before stopping to fix the independent-review P1 |
| `make check` | pass | 3,931 passed, 246 deselected in 1,096.17 s; supply-chain, release audit, Ruff and mypy passed |
| `make check` (post-hosted per-worker remediation) | pass | 3,931 passed, 246 deselected in 1,235.07 s; unchanged 250/500 ms scale budgets passed |
| `make test-performance` (D131 focused bytes) | pass | Exact fresh 1,000-read node passed in 2.72 s |
| `.venv/bin/pytest -q -m 'not performance' tests/unit/test_scale_harness.py` | pass | 23 passed, 1 deselected in 3.63 s; validates isolation, stateless metadata, v2 rendering compatibility, tail diagnostics and percentile boundaries |
| Direct 1,000-read diagnostic | pass | p50/p95/p99/max 9.775/10.761/12.288/19.260 ms; zero observations over 250/500; all 1,000 observations accounted for |
| `.venv/bin/pytest -q tests/unit/test_scale_harness.py tests/unit/test_release_audit.py tests/unit/test_evaluation.py` | pass | 42 passed in 7.25 s |
| `make test-scale-correctness` | pass | 29 passed and 1 deselected, then 8 passed and 4 deselected; correctness report generated |
| `make check` (D131 final bytes) | pass | Exact performance node passed, then 3,933 functional tests passed with 247 deselected in 943.82 s; supply-chain, release audit, Ruff and mypy passed |
| `make check` (concurrent D131 attempt) | discarded, externally terminated | The isolated gate passed, but a second pre-existing `make check` contended with the functional suite; the tree then changed and the captured session received SIGTERM 15. This run is not final-byte evidence |
| `make runtime-wheel-smoke` | pass | Installed wheel validated migrations 1–15 and every runtime entrypoint |
| `git diff --check` | pass | Repeated on the final documentation bytes before commit |

## Hosted publication evidence

- Commits `c7e72cc97e4226b2d953f5c1e8ef55178a1598f5` and
  `3f57a2ea89220ff0c68ac58f0f8e668069e93f81` are published on
  `agent/ignore-node-modules` and draft PR #1. GitGuardian passed.
- Run `30816314566` is branch-associated with `3f57a2e` but checked out PR merge ref
  `04402da8327f08ccc29799a6df4b4f2e9645dc90`; it is not an exact tagged-main candidate result.
  Supply-chain passed and PostgreSQL integration passed in 1 h 37 min 59 s.
- Quality passed 3,930 other tests with zero load errors, then the 64-read smoke failed only p95:
  p50/p95/p99/max was 26.420/330.102/355.132/355.132 ms against unchanged 250/500 ms limits. The
  correlated tail remained after all four executor workers were preconditioned, refuting the
  earlier cold-worker explanation without proving GC, GIL, scheduler or product causality.
- At 64 nearest-rank samples, a tail compatible with the synchronized four-worker launch can occupy
  6.25%: p95 is the fourth-largest value and p99 is the maximum. D131 runs only the exact
  performance node in a fresh process and raises the synthetic sample to 1,000 reads. At least 51
  values over 250 ms fail p95 and at least 11 over 500 ms fail p99. Four correlated transients are
  only 0.4%; no value is discarded and reports expose both tail counts. The commit-bound hosted
  rerun for these bytes must be judged from GitHub rather than inferred from local evidence.

## Automated test results

- Focused D131 tests: PASS — the exact 1,000-read node and 23 non-performance scale-harness tests
  passed; a direct diagnostic accounted for all 1,000 timed observations and the tracked v2 report
  renders without requiring newly added fields.
- D131 final-byte `make check`: PASS — the isolated performance node passed, followed by 3,933
  functional tests with 247 explicit deselections; all static/type gates passed.
- Integration tests: not required for the offline preflight; it deliberately performs no service
  call.
- Acceptance tests: PASS — one clean synthetic candidate remains NO-GO with every external gate
  missing.
- Coverage: not run; no coverage claim.

## Operator manual test

1. Run `make m30-readiness` from the repository root.
2. Inspect `.local/m30/readiness.json` and `.local/m30/readiness.md`.
3. Confirm the current candidate facts and that no hosted/operated/third-party/owner control is
   marked passed.

Expected result before the external prerequisites exist:

```text
blocked_prerequisites
campaign_executable=false
release_decision=no_go
network/database/DataHub/source actions=0
```

## Architecture and security review

- Dependency direction: pure domain invariants; application depends on ports; filesystem/Git and
  presentation remain adapters; `bootstrap.py` is the composition root used by the script.
- Source database writes: zero; no database connection exists in this vertical.
- SQL/LLM validation: neither SQL nor model output is produced or accepted.
- DataHub mutation approval: no DataHub port or credential is composed.
- Secrets/proprietary data: reports contain only public codes, paths, versions and digests; no raw
  evidence body or credential.
- Fanout/semantic risks: the existing one-connection/three-table/two-join and SQL-family contract is
  frozen; the preflight cannot expand it.
- Independent final re-review after remediation: P0=0, P1=0 and P2=0. It specifically reproduced
  fail-closed deep YAML, exact 15-source identity, descendant-safe Git timeout, real unmeasured
  per-worker preconditioning, gitlink rejection and the JSON bundle commit-marker boundary.

## Decisions made

- Decision: an offline preflight may pass repository controls only and can never return release GO.
- Reason: prevent local fixtures, self-asserted JSON or ephemeral PR merge subjects from becoming
  false operated/commercial evidence.
- Decision: a declared percentile gate must use a meaningful sample and its own fresh process while
  retaining the exact latency budgets.
- Reason: with 64 concurrency-four observations, a correlated tail compatible with synchronized
  launch can control p95/p99; the two hosted results refute cold-worker causality but do not prove
  a runtime or product cause.
- Logged in: D130 and D131.

## Known limitations or unverified items

- No exact clean tagged `main` candidate has been frozen.
- Hosted quality/PostgreSQL/supply-chain evidence must be regenerated for the final candidate
  subject. The current supply-chain artifact is bound to a PR merge ref, and the quality result
  predates the D131 fresh-process/sample correction.
- The 1,000-case blind corpus, target environment, independent pentest, browser/accessibility,
  scale/soak, legal/service review and owner signatures do not exist as accepted evidence.
- Phase 0 does not yet ingest or cryptographically verify external receipts; it deliberately keeps
  every external gate missing instead of accepting a local assertion.

## Blockers

- Operated M29 prerequisites and independent/owner inputs listed in the M30 plan.
- Merge/branch protection/release environment and exact candidate artifacts.

## Next milestone readiness

- Dependency state: M35 is satisfied locally; operated M29 and all independent/owner M30 inputs
  remain unsatisfied.
- Recommended next prompt: provision the target/assessor inputs, freeze a clean tagged candidate,
  then implement direct verification of exact-subject hosted and signed external receipts before
  running the corpus.
- Required operator prerequisites: protected GitHub/release controls, target environment, corpus
  owner/answer key, independent security assessor and named product/security/operations/semantic
  approvers.
