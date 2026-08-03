# M32 milestone handoff

## Summary

- Milestone: M32 — Simple and advanced natural language to copyable PostgreSQL
- Status: complete and accepted locally for the bounded deterministic/synthetic capability
- Recommended operator decision: accept local automated scope; complete the retained manual
  browser and live-provider/holdout gates before any broader quality or production claim
- Proposed commit message: `feat: add governed copy-first advanced PostgreSQL`

## Implemented

- Added parallel immutable version-2 typed request and plan contracts without changing historical
  version-1 payloads or fingerprints.
- Added bounded Spanish mention extraction, complete approved logical-registry traversal, a
  3-model/12-field/2-join semantic closure, strict typed interpretation, and deterministic
  representability routing.
- Added exact simple COUNT/SUM, verbose-v1, short-ranking-v2, simple row, and advanced reference
  language journeys. Provider schemas still cannot emit SQL or physical identifiers.
- Added deterministic semantic resolution before preview signing so the human confirms selected
  datasets, mapping/join confidence/evidence/risks, fanout facts, and the resolved-plan
  fingerprint. Confirmation reloads context and revalidates/re-resolves the same signed request
  without rerunning retrieval or either language stage.
- Added fourteen closed window operations, boolean trees, conditional aggregates, buckets,
  `HAVING`, post-window filters/order, and compiler-owned CTE stages, bounded to four derived
  window outputs and eight window AST nodes.
- Added a deterministic PostgreSQL compiler extension, a scope-aware independent AST guard, a
  typed standalone literal renderer, and a second complete zero-binding guard.
- Added a copy-first CLI interactive review flow and Streamlit preview/confirmation/download flow.
  The primary artifact always reports `executed=false`; optional validation is separate and
  disabled by default.
- Added deterministic evaluation, PostgreSQL-only operator guidance, a benchmark capability
  matrix, and explicit no-SQL outcomes for unsupported or unapproved meaning.

## Files changed

- `src/schemabridge/domain/advanced_requests.py`,
  `src/schemabridge/domain/advanced_plans.py`, and
  `src/schemabridge/domain/advanced_query_studio.py`: closed v2 language, plan, preview, review,
  route, confirmation, and fingerprint contracts.
- `src/schemabridge/application/natural_sql.py`,
  `src/schemabridge/application/sql_export.py`, and
  `src/schemabridge/application/ports/advanced_query_studio.py`: prepare, confirm, re-resolve,
  generate-copy, renderer, and language/retrieval ports.
- `src/schemabridge/adapters/query_studio/advanced_fake_language.py`,
  `advanced_semantic_index.py`, and `advanced_security.py`: deterministic fixtures, governed
  retrieval, and signed preview tokens.
- `src/schemabridge/adapters/language/openai_advanced_query_studio.py` and
  `openai_boundary.py`: strict live provider schemas and admitted tenant-governed boundary.
- `src/schemabridge/adapters/sql/compiler.py`, `guard.py`, and `export.py`: deterministic advanced
  compilation, independent scope/topology validation, and standalone typed-literal rendering.
- `src/schemabridge/bootstrap.py`, `src/schemabridge/entrypoints/cli/main.py`,
  `src/schemabridge/entrypoints/streamlit/app.py`, and
  `src/schemabridge/entrypoints/streamlit/natural_sql.py`: live/fake/disabled composition and
  copy-first CLI/Streamlit delivery.
- Existing semantic resolution, registry, request-context, query-policy, and execution modules:
  v2-compatible typed unions and exact guard metadata without moving I/O inward.
- `tests/unit/test_advanced_*`, `tests/unit/test_natural_sql*`,
  `tests/unit/test_copyable_sql_contract.py`, `tests/unit/test_m32_benchmark_matrix.py`,
  `tests/unit/test_sql_guard_m32.py`, and OpenAI/bootstrap regressions: positive, route, boundary,
  artifact, and adversarial coverage.
- `tests/acceptance/test_m32_*.py` and
  `tests/integration/test_m32_natural_sql_postgres.py`: exact copy-first, Spanish, Streamlit, and
  optional read-only five-row journeys.
- `plans/M32_ADVANCED_COPYABLE_SQL.md`, `prompts/M32_ADVANCED_COPYABLE_SQL.md`,
  `docs/adr/0015-advanced-copyable-postgresql.md`, core architecture/security/pipeline/test/
  runbook/browser/evaluation docs, and `README.md`: accepted bounded contract, procedures,
  evidence, and limitations.
- `reports/m32-copyable-sql-deterministic-evaluation.md`: bounded raw deterministic evidence.
- `.gitignore`: explicitly tracks the bounded M32 deterministic report while retaining default
  ignore behavior for runtime reports.
- `tasks/CURRENT_TASK.md`, `tasks/PROJECT_STATE.md`, `tasks/DECISION_LOG.md`, and this handoff:
  final M32 state and D125.

No production dependency was added.

## Commands executed

| Command | Result | Notes |
|---|---|---|
| M32 22-module targeted pytest selection with `SCHEMABRIDGE_TEST_DATABASE_URL` | pass | 304 passed in 22.38 s; includes unit, acceptance, and one read-only integration |
| Focused language fake/retrieval/natural/bootstrap/CLI/acceptance selection | pass | 72 passed in 8.91 s |
| `pytest -q tests/acceptance/test_m32_streamlit_copyable_sql.py` | pass | 3 passed in 5.88 s |
| `pytest -q tests/integration/test_m32_natural_sql_postgres.py` with synthetic reader DSN | pass | 1 passed in 2.98 s; five exact rows |
| Focused `ruff format --check` and `ruff check` | pass | 12 M32 source/test files formatted and clean |
| Focused `mypy` | pass | 11 M32 source files, no issues |
| First `make check` attempt | fail | stopped at Ruff: two files required mechanical formatting; no functional test failed |
| `ruff format` for the two reported files, focused lint, and `git diff --check` | pass | 2 files reformatted; lint and whitespace clean |
| Final-byte `make check` | pass | repeated successfully after state/handoff updates; supply-chain/release audit, Ruff, strict mypy over 320 source files, 3,587 tests passed, 224 deselected |
| `git diff --check` | pass | no whitespace errors |
| Streamlit loopback startup and `/_stcore/health` | pass | port 8510 returned `ok`; process stopped and temporary directory removed |
| In-app browser discovery | not run | exact browser list was empty; no forbidden alternate browser surface was substituted |

## Automated test results

- Focused tests: 304/304 targeted M32 checks pass.
- `make check`: 3,587 passed, 224 deselected; Ruff and strict mypy pass.
- Integration tests: 1/1 optional PostgreSQL reference case passes through the synthetic read-only
  role and returns all five reviewed rows.
- Acceptance tests: exact reference/copy, four Spanish route controls, and 3/3 Streamlit journeys
  pass.
- Coverage: no new coverage gate was requested for M32; the repository's previously accepted
  current-byte M29 coverage record is not relabeled as M32 evidence.

The reference standalone SQL SHA-256 is
`ec589a1527d0d641f4f7f7eb7e052ca1ace5b092013b437da72542ce4145d4f3`.
The SQL and literal values are intentionally absent from this durable handoff.

## Operator manual test

1. Start the final Streamlit app in fake mode with a fresh ignored draft store and open Query
   Studio at desktop 1280×720 and mobile 390×844.
2. Prepare the simple Product row request, the verbose customer-count request, the short revenue
   ranking request, and the exact advanced Spanish reference in the M32 plan.
3. Before confirmation, verify there is no SQL or download; inspect the exact route, typed clauses,
   selected models/fields/datasets/joins, mapping and join confidence/evidence/risks, assumptions,
   fanout facts, and fingerprints.
4. Confirm the exact preview. Verify standalone PostgreSQL, no `%s` or `$n` placeholders, matching
   SHA-256, `executed=false`, visible copy/download, and optional validation disabled.
5. Exercise ambiguity, stale context, recursive/gaps-and-islands, `ROLLUP`, and hostile instruction/
   SQL text. Verify no SQL. Check the physical-only case in the separate M27 discovery surface as
   `needs_mapping_review`.
6. Verify focus order, no document overflow, a clean browser console, escaped hostile text, and no
   SQL/literal/provider/credential leakage outside the confirmed transient artifact.

Expected result:

```text
Supported requests expose an exact no-SQL preview and produce one standalone PostgreSQL statement
only after exact confirmation. Ambiguous, stale, unapproved, and unsupported requests produce no
SQL. No primary-flow query is executed.
```

The 2026-07-30 attempt started Streamlit and passed health, but browser discovery returned `[]`.
The manual viewport/console/interaction matrix therefore remains **not run**, not PASS.

Later, on 2026-08-03, Codex completed exactly the advanced Spanish desktop happy path, recorded in
`docs/14_BROWSER_ACCEPTANCE.md` and `tasks/M34_HANDOFF.md`. That partial observation does not change
this milestone's original result and does not close the remaining simple, v1/v2, blocked-state,
hostile-input, clipboard/download or 390×844 M32 matrix.

## Architecture and security review

- Dependency direction: domain remains pure; application uses typed ports; adapters own
  language/SQL/crypto/provider I/O; bootstrap is the composition root.
- Source database writes: none. The only integration uses the hardened read-only synthetic role.
- SQL/LLM validation: the model emits typed mentions/intent only; deterministic code validates,
  resolves, compiles, guards, literalizes, and guards again.
- DataHub mutation approval: unchanged; M32 adds no write path or approval bypass.
- Secrets/proprietary data: synthetic fixtures only; provider schemas, reports, logs, and durable
  state exclude SQL, parameters, literal values, credentials, raw source rows, and private
  reasoning.
- Fanout/semantic risks: mapping/join evidence and risks are previewed; automatic fanout rewrite
  blocks confirmation and requires the requested safe metric to be explicit.

## Decisions made

- Decision: accept M32 only as a bounded deterministic/synthetic PostgreSQL capability.
- Reason: copy-first behavior and advanced supported families are exact and independently guarded,
  while universal SQL/language, live quality, arbitrary destination, and production claims remain
  unproved.
- Logged in: `tasks/DECISION_LOG.md` as D125 and
  `docs/adr/0015-advanced-copyable-postgresql.md`.

## Known limitations or unverified items

- PostgreSQL only; the artifact targets the same governed database/context and current
  unquoted-canonical lowercase ASCII physical identifiers.
- At most three tables, two joins, four derived window outputs, and eight window AST nodes.
- Cross/self joins, arbitrary subqueries, set operations, recursion, gaps/islands, and `ROLLUP`
  remain explicit no-SQL outcomes.
- Deterministic fixtures are not live-provider/holdout generalization evidence and do not justify
  an “error-free expert for every query” claim.
- M32 consumes approved logical context only; catalog-only physical candidates remain in M27
  `needs_mapping_review`.
- Manual browser, live-provider quality, independent M30 security/evaluation, operated
  production/release controls, pilot, and GA are unverified.

## Blockers

- No blocker for the accepted local automated M32 scope.
- Operator browser evidence is blocked in this environment by the empty in-app browser list.

## Next milestone readiness

- Dependencies satisfied: M32's bounded product capability is ready for review and later M30
  adversarial/live-quality work.
- Recommended next prompt: run a frozen live-provider holdout and the complete manual browser
  matrix without widening the SQL language or production authority.
- Required operator prerequisites: connected in-app browser, approved synthetic provider campaign
  and budget/admission policy, same governed PostgreSQL target, and separate M30/M31 acceptance.
