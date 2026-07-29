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

The publication candidate now installs the same reviewed DataHub/MCP dependencies in `bootstrap`,
tracks the 19 content-addressed synthetic campaign reports plus the two signed ledger attestations
cited by tests and governance docs, sets `NO_COLOR=1` for stable CI output, and publishes then
read-checks the exact approved synthetic registry fingerprint before integration tests. No key,
private data, unsigned runtime report, source-database write, or additional production capability
is included.

Focused reproduction passes 33 formerly affected tests both in the working repository and in an
index-only clean export. The development release audit passes 772 candidate files and 23 direct
licenses with only the expected dirty-tree warning. The current draft-PR checks are the
authoritative remote publication result.

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
