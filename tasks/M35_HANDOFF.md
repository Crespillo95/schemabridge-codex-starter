# Milestone handoff

## Summary

- Milestone: M35 — Governed registry-v2 change lifecycle
- Status: complete; accepted locally under D129
- Recommended operator decision: accept the bounded local PostgreSQL implementation; retain
  commercial/production/release NO-GO
- Proposed commit message: `feat: complete governed registry v2 change lifecycle`

## Implemented

- Phase A authors one tenant-bound join over exact active same-connection mappings, obtains only
  aggregate read-only evidence, requires a steward decision and assembles the next immutable v2.
- Phase B consumes one immutable M33 replacement proposal, exact active base/catalog/dependency
  authority and optional complete blocking M26 remediation state. Every incident join is preserved
  exactly, freshly profiled/upserted, or explicitly removed.
- Strict authenticated HTTP covers profile persistence/finalization, model-change create/list/
  inspect/decision/preparation, exact idempotency, tenant masking and minimized list responses.
- Control-plane schema v15 persists bounded append-only history, crash-safe requested-job binding,
  exact replay, fenced claims/heartbeats, role separation and generic publication sources.
- M34 publication accepts closed `add_join_v1` and `replace_model_v1` kinds. Phase B re-reads the
  outer proposal, exact M33 source, decisions, active base, dependency/M26 state and fresh profile
  witnesses; a PostgreSQL witness binds the complete replacement effects before handoff.
- The active pointer remains unchanged through publication. M23 activation and M26 post-activation
  inspection remain separate approved operations.
- Commercial support, user workflow, go-live checklist and the M30/M31 path are documented. The
  current product is a bounded PostgreSQL copy-first candidate, not a multi-dialect GA product.

## Files changed

- `migrations/control_plane/0015_registry_v2_changes.sql`: additive M35 persistence, roles,
  authority functions, generic publication source/trigger and replacement activation witness.
- `src/schemabridge/domain/registry_changes.py` and
  `src/schemabridge/domain/registry_model_changes.py`: pure Phase A/B delta and assembler contracts.
- `src/schemabridge/application/registry_changes.py` and
  `src/schemabridge/application/registry_model_changes.py`: authenticated use cases and ports.
- `src/schemabridge/adapters/storage/postgres_registry_changes.py` and
  `src/schemabridge/adapters/storage/postgres_registry_model_changes.py`: durable tenant stores,
  exact replay and profile queue/readers.
- `src/schemabridge/adapters/semantic_onboarding/publication_authority.py`: exact join/model
  publication authority closure.
- `src/schemabridge/adapters/control_plane/postgres_registry_publication.py`: typed multi-kind
  proposal reader.
- `src/schemabridge/entrypoints/http/app.py`, `schemas.py`, and `bootstrap.py`: Phase A/B HTTP and
  isolated publisher composition.
- `scripts/m35_registry_join_change_scenario_app.py`: synthetic manual browser instrumentation.
- `docs/19_COMMERCIAL_USAGE.md`, M30/M31 plans and architecture/security/runbook/test docs: exact
  support matrix, adoption path and retained release gates.
- M35 unit, PostgreSQL integration and acceptance tests: positive, concurrency, drift, tampering,
  RBAC, replay, backup and historical M33/M34 regression coverage.

## Commands executed

| Command | Result | Notes |
|---|---|---|
| M35 combined focal unit command | pass | 165 passed |
| Phase B HTTP focal matrix | pass | 17 passed; all seven routes and command replay boundaries |
| M35 acceptance plus semantic/socket regressions | pass | 7 passed, 1 explicit historical format-v1 skip |
| Phase B/M34 PostgreSQL publication regression | pass | 6 passed |
| Expanded Phase B PostgreSQL/domain/migration matrix | pass | 65 passed |
| Isolated schema-v15 backup identity test | pass | 1 passed; 88/88 tables visible read-only |
| `make runtime-wheel-smoke` | pass | Built and installed the wheel in isolation; migrations 1-15 and every runtime entrypoint were present |
| Broad integration command against the shared local control DB | expected fail-closed | Shared DB remained v14 while code requires v15; it was not mutated. It also exposed a sub-millisecond host/PostgreSQL skew race in fixtures, corrected with an explicit fresh one-second margin |
| Repeated isolated M34/M35 PostgreSQL regression | pass | 6 passed after the clock-independent fixture correction |
| Full affected PostgreSQL upgrade-fixture regression | fail then corrected; pass | Final v15 audit found stale expected migration lists; all 64 affected tests pass after adding migration 15, with no database mutation outside their ephemeral fixtures |
| Independent final security review | pass after one correction | Found and fixed an invalid source-owner equality in the last publisher authority; final P0=0, P1=0 |
| Final pre-staging scope/secret/artifact review | pass | 98 candidate paths; P0=0, P1=0, P2=0; no secrets, non-synthetic data, binaries, dumps, caches, symlinks or out-of-scope files |
| First `make check` | fail then corrected | 21 new files required official Ruff formatting |
| Second `make check` | fail then corrected | 3,880 passed; wheel closed list omitted migration 15 |
| Final pre-GitHub `make check` | pass | supply chain, release audit, Ruff, mypy and 3,881 tests; 245 deselected in 1,098.63 seconds |
| `git diff --check` | pass | no whitespace errors |

## Automated test results

- Focused tests: PASS — 165 combined unit; 17 HTTP boundary; expanded Phase B focal 65.
- `make check`: PASS — mypy over 357 source files; 3,881 passed and 245 explicitly deselected.
- Integration tests: PASS — 6 fresh M34/M35 publication tests, isolated v15 backup, and 64
  affected upgrade/compatibility tests after the final v15 expectation audit.
- Acceptance tests: PASS — 7 passed, 1 documented historical format-v1 skip.
- Coverage: not rerun for M35; coverage is not claimed by this handoff.

## Operator manual test

1. Start `scripts/m35_registry_join_change_scenario_app.py` as documented in
   `docs/19_COMMERCIAL_USAGE.md`.
2. Persist the profile request, produce aggregate evidence, finalize and approve as steward.
3. Prepare as a distinct publisher, reserve/assemble/authorize/publish and inspect read-back.
4. Repeat self-join, cross-connection, stale-target and many-to-many cases; inspect mobile width.

Expected result:

```text
queued → leased → awaiting_approval → approved → leased → activation_ready
active pointer unchanged
external target writes = 1 (synthetic target only)
source writes = 0; SQL = 0; rows/DSNs/credentials exposed = 0
hostile cases external mutations = 0
390×844 horizontal overflow = 0
```

The Codex internal-browser run on 2026-08-03 observed exactly that result. This instrumentation is
in memory and does not prove live DataHub, source, IAM or secret-manager operation.

## Architecture and security review

- Dependency direction: domain remains pure; application uses typed ports; PostgreSQL/HTTP are
  adapters/entrypoints and bootstrap is the composition root.
- Source database writes: none; profiling is aggregate-only, allowlisted and read-only.
- SQL/LLM validation: M35 emits neither SQL nor LLM output. M32 remains the only copy-SQL path and
  retains deterministic compilation plus two AST guards.
- DataHub mutation approval: API/web have no writer secret. The isolated M34 publisher still needs
  exact post-assembly authorization/read-back and cannot activate.
- Secrets/proprietary data: persisted responses exclude SQL, parameters, rows, samples, DSNs and
  credentials; public fixtures are synthetic.
- Fanout/semantic risks: many-to-many is non-executable; one-to-many requires explicit distinct
  mitigation/risk; names/confidence never approve equivalence.
- Independent final review: P0=0 and P1=0 after the exact publisher-authority regression was
  corrected and covered with different M33-source/change owners.

## Decisions made

- Decision: use immutable deltas over an exact active v2 and reuse M34/M23 rather than edit or
  auto-activate an existing document.
- Decision: one M33 replacement source authorizes at most one outer Phase B draft; rejection or
  staleness requires a new immutable source instead of silent rebinding.
- Reason: preserve one exact provenance chain, separation of duties, recovery and fail-closed
  authority across authoring, publication, activation and evidence revalidation.
- Logged in: D129 and ADR 0018.

## Known limitations or unverified items

- Phase B publication/read-back uses a simulated DataHub receipt; no live external writer/IAM was
  available and no external write is claimed.
- PostgreSQL is the only SQL output dialect and copied SQL targets the same governed context.
- Each query is still bounded to one connection, three tables and two joins. Arbitrary subqueries,
  set operations, recursion, gaps/islands, unsafe `ROLLUP`, self/CROSS joins and federation remain
  typed unsupported outcomes.
- M35 is single-model/single-join authoring, not bulk/incremental remediation for thousands of
  mappings. Catalog scale does not prove service/query scale for a real customer workload.
- Operated secret manager/IAM/NetworkPolicy/SIEM/backup/RPO/RTO, independent penetration test,
  blind live-provider quality, legal/support/SLO/billing and a real pilot are unverified.
- Managed nodes require operated clock synchronization and skew alerting. The code fails closed on
  not-yet-current approvals/leases; local integration fixtures now avoid assuming zero clock skew.

## Blockers

- No local implementation blocker remains for M35.
- Commercial/production/release GO remains blocked by operated M29 prerequisites, M30, M31 and the
  external/legal/service gates in `docs/19_COMMERCIAL_USAGE.md`.

## Next milestone readiness

- Dependencies satisfied: M35 supplies the bounded registry lifecycle needed by M30.
- Recommended next prompt: execute M30 only after the exact candidate and operated M29 environment
  inputs exist; do not invent provider/pentest/operations evidence locally.
- Required operator prerequisites: real DataHub/IAM/secret manager, approved bilingual blind
  corpus and answer key, target capacity profile, independent security assessor, legal/privacy/
  support owners and a signed release-candidate freeze.
