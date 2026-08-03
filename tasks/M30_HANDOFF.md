# Milestone handoff

## Summary

- Milestone: M30 — Production evaluation and security verification, Phase 0 only
- Status: partial; candidate-readiness prepared locally, campaign blocked
- Recommended operator decision: accept the fail-closed preparation vertical; retain M30,
  commercial, production and release NO-GO
- Proposed commit message: `feat: add fail-closed M30 candidate readiness`

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
- `scripts/benchmark_catalog_scale.py` and `tests/unit/test_scale_harness.py`: explicit unmeasured
  warmup for the warm-cache latency contract exposed by hosted CI, without relaxing its 250/500 ms
  budgets.
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
| `make runtime-wheel-smoke` | pass | Installed wheel validated migrations 1–15 and every runtime entrypoint |
| `git diff --check` | pass | Repeated on the final documentation bytes before commit |

## Automated test results

- Focused tests: PASS — 49 M30 tests and 39 scale/release/evaluation tests passed.
- `make check`: PASS — 3,931 passed and 246 explicitly deselected.
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
  warmup, gitlink rejection and the JSON bundle commit-marker boundary.

## Decisions made

- Decision: an offline preflight may pass repository controls only and can never return release GO.
- Reason: prevent local fixtures, self-asserted JSON or ephemeral PR merge subjects from becoming
  false operated/commercial evidence.
- Logged in: D130.

## Known limitations or unverified items

- No exact clean tagged `main` candidate has been frozen.
- Hosted quality/PostgreSQL/supply-chain evidence must be regenerated for the final candidate
  subject; the current supply-chain artifact is bound to a PR merge ref.
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
