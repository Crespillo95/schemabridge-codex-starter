# Current task

- Current milestone: M28 — Governed connector routing, explicit dialects, and query cost controls
- Status: complete and accepted locally on 2026-07-28
- Recommended operator decision: retain M28 as the local synthetic baseline; M29 is eligible but
  not started
- Prompt: `prompts/M28_COMPILER_CONNECTORS_COST_CONTROLS.md`
- Plan: `plans/M28_COMPILER_CONNECTORS_COST_CONTROLS.md`
- Production/release GO: **NO**

## Accepted result

M28 binds every managed executable plan to one exact tenant connection, immutable connector-route
revision, PostgreSQL dialect, approved reader, immutable catalog/type evidence, and complete cost
budget. Secrets remain capability-private; the LLM still cannot produce executable SQL; a
deterministic compiler and independent AST guard own SQL; bounded `EXPLAIN ... ANALYZE FALSE`
preflight repeats before each read-only preview.

Schema v9, pristine and v8→v9 migration paths, the six-role capability matrix, two-tenant source
isolation, dynamic catalog/profile/execution routing, and the 10/75 plus 5,434/41,028 inventory
profiles pass. PostgreSQL is the sole executable M28 dialect; unsupported dialects and federation
fail closed.

## Final evidence

- Focused M28 unit groups: 427 passed.
- Focused connector/route/cost integration: 37 passed; focused acceptance with PostgreSQL:
  28 passed.
- Critical schema/tenant/managed-query cut: 17 passed.
- Full integration: 164 passed, one retained M27 fixture skip, six expected DataHub warnings in
  662.06 seconds.
- Full acceptance: 47 passed with one expected DataHub warning in 133.50 seconds.
- `make check`: Ruff format/lint, strict mypy over 276 source files, and 2,714 tests passed with
  207 deselected in 989.17 seconds.
- `make coverage`: 2,920 passed, one retained M27 fixture skip, seven expected DataHub warnings,
  81.76% coverage in 2,626.35 seconds.
- Deterministic evaluation: PASS over 11 tables/465 rows; `live_llm=not_run`.
- Runtime wheel, release scan, schema v9/six-role check, scale correctness, PostgreSQL scale plans,
  and whole-tree diff hygiene: PASS. The release scan's dirty-tree warning is retained.
- Codex internal browser: all nine scenarios passed at desktop 1280x720 and mobile 390x844 with
  clean consoles, no overflow/XSS/protected-data hits, distinct tenant readers/results, and exact
  cleanup.

## Publication reproducibility follow-up — 2026-07-29

The first draft-PR run exposed four clean-checkout assumptions that the accepted local environment
had masked: `make bootstrap` omitted the existing `datahub` extra; 18 retained signed v4–v10
campaign reports were ignored despite immutable-byte tests; Rich emitted CI-only ANSI sequences;
and the fresh DataHub service had no explicitly published semantic registry.

The next hosted run reached the live integration suite and exposed two fixture collisions. The
control container used the new non-secret local default while administrative tests still fell back
to the older password, and the registry seed plus replay test used the same deterministic local
principal with different approval timestamps. Every affected test and browser-runtime fallback now
uses the Compose file's explicit `local-only-not-a-secret` loopback placeholder while retaining its
test-only override, and only the approved registry seed receives a dedicated local subject. No
real credential is stored, no trust authentication is enabled, and the replay test retains an
independent approval identity and audit ledger.

The same hosted run also proved that `NO_COLOR=1` alone does not make Rich/Typer help bytes
portable across runner platforms. The three security-contract tests now remove ANSI styling with
Click before checking required and forbidden options; the CLI behavior and the assertions'
allow/deny lists are otherwise unchanged.

The publication candidate now installs the same reviewed DataHub/MCP dependencies in `bootstrap`,
tracks the 19 content-addressed synthetic campaign reports plus the two signed ledger attestations
cited by tests and governance docs, sets `NO_COLOR=1` for cleaner CI output, normalizes CLI-help
styling at the assertion boundary, and publishes then read-checks the exact approved synthetic
registry fingerprint before integration tests. No key, private data, unsigned runtime report,
source-database write, or additional production capability is included.

Focused reproduction passes 33 formerly affected tests both in the working repository and in an
index-only clean export. The development release audit passes 772 candidate files and 23 direct
licenses with only the expected dirty-tree warning.

The second follow-up passes 75 registry-publication unit tests, the three GitHub-Actions ANSI
regressions, 22 affected PostgreSQL integration tests, and all four live DataHub registry tests
from clean synthetic volumes. The complete service gates pass with 164 integration tests plus one
retained fixture skip and 47 acceptance tests. The final CI-shaped `make check` passes Ruff,
strict mypy over 276 source files, and 2,714 tests with 207 service tests deselected in 726.42
seconds.

The third hosted run passed both quality jobs and reached coverage only after DataHub, control
PostgreSQL, integration, and acceptance had passed. Coverage reached 81.29%, but a generic
job-wide `DATABASE_URL` crossed the component credential boundary and caused 31 deliberate
fail-closed configuration tests; coverage instrumentation also made the 64-read wall-clock scale
smoke exceed an unchanged latency budget once, while its zero-error and bounded-page checks
passed. The generic DSN is now scoped only to the evaluation step, all 19 control-admin fallbacks
match the explicit local Compose placeholder. Focused regressions pass.

The final local publication bytes pass Ruff, strict mypy over 276 source files, and 2,719
service-free tests with 207 deselected in 766.64 seconds. From clean synthetic demo, DataHub, and
control-plane state, the registry read-back, schema v9/six-role check, 164 integration tests plus
one retained skip, and all 47 acceptance tests pass. The complete corrected coverage run passes
2,925 tests with that one skip and seven expected DataHub warnings at 81.75% in 2,588.95 seconds.
The generic source/admin variables were explicitly absent from that process; the scale smoke and
all 31 formerly contaminated component-boundary tests passed.

The fourth hosted run proved the corrected credential scope: on the same commit, both quality jobs
passed, GitGuardian passed, and the push service job passed 2,925 tests with one retained skip at
81.75% coverage. Its duplicate PR service job passed DataHub, control-plane, integration,
acceptance, and every functional coverage check, but the 64-read smoke measured p95 292.712 ms
under active service/shared-runner contention and failed only its unchanged 250 ms budget. The
smoke is therefore marked `performance` and remains mandatory in the clean `make check` quality
job with unchanged 250/500 ms limits; release clean-room now also runs that gate before starting
project services. Coverage deselects only that marker while retaining the pure exact-limit
regression gate. The separately operated 5,000-read concurrency-16 PostgreSQL benchmark remains
authoritative. Replacement draft-PR checks remain the publication authority.

## Scope guard

M28 keeps inventory cardinality dynamic, while a single request remains constrained to one
connection, at most three tables, and two joins. Application preflight never uses
`EXPLAIN ANALYZE`; engineering timing diagnostics remain synthetic-only.

Local acceptance is not production approval. The draft PR is development publication evidence,
not a reviewed or signed release candidate; remote secret management, TLS/NetworkPolicy,
observability/SIEM, supply-chain controls, backup/recovery, production capacity/SLO evidence,
real-tenant evaluation, penetration/security review, clean signed release identity, and external
sign-off remain open.

## Next milestone

M29 is eligible but has not started. Its operated remote-secret, infrastructure, observability,
supply-chain, and recovery scope must be planned and executed as a separate milestone. M30
production evaluation/security verification and M31 pilot/GA remain sequentially blocked.
