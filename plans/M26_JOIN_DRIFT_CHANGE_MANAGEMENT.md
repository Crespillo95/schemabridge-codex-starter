# M26: Join drift and semantic change management

- Status: complete; accepted locally on 2026-07-24
- Timebox: 36 hours
- Recommended Codex: GPT-5.6 Sol — Extra High (xhigh)
- Dependencies: accepted M25 dynamic catalog inventory and accepted M23 registry control plane

Implementation note (2026-07-24): the current candidate includes schema v5, exact multiconnection
bindings, workspace-and-connection-qualified aggregate profile jobs, scoped scan fan-out, paginated dependency
reconciliation with lease continuation, scope-qualified DataHub recipe identities, and the
dependency-aware pre-I/O gate. The complete required command, browser, and operator-acceptance
evidence is recorded in `tasks/M26_HANDOFF.md`. Final-audit corrections add separate locator-first
initial-candidate and approved-evidence SQL,
bounded/batched dependency replacement, conservative stale-artifact projection, exact live-index
gating, resulting-head baseline revision, and workspace-plus-connection profile claims. Acceptance
is based on the complete recorded matrix, not implementation or focused regression alone.

## Objective

Detect when the physical catalog evidence behind an approved mapping or join has changed, show the
exact governed and downstream blast radius, and fail closed before SQL compilation or source I/O
until the change is either safely revalidated or remediated through a new approved semantic
registry version.

M26 compares only explicitly bound, governed resources. It never performs a catalog-wide
all-pairs match and never treats an equal name or description as semantic equivalence. A registry
physical reference such as `schema.table.field` is not enough to identify an M25 inventory item:
the durable semantic evidence binding also identifies workspace, catalog scope, connection,
asset, field path, and observed generation. Initial bindings and every compatible revalidation
require exact human approval.

Catalog scale remains dynamic and independent from query scale. A tenant may expose 10, 5,434, or
more tables, but drift inspection is bounded by the active registry's maximum 2,000 mappings and
500 join contracts. One executable query remains limited to one governed connection, three
tables, two joins, the deterministic compiler, AST guard, allowlist, fanout policy, result limit,
statement timeout, and read-only source transaction.

## Scope assumptions

1. PostgreSQL control schema v5 is additive. Migrations 0001–0004 remain byte-for-byte unchanged.
2. The active M23 pointer and strict immutable DataHub registry version remain authoritative for
   mappings and joins. M26 never edits them in place.
3. M25 inventory identity is authoritative for metadata observation only. It is not an M28
   execution-connector or cross-connection federation contract.
4. An exact physical field may be proposed as a binding candidate only inside the pointer's
   workspace/catalog scope. An operator must approve the exact connection/asset/field identity.
5. Missing, disabled, removed, stale, out-of-scope, or ambiguous bindings fail closed. A homonymous
   field in another connection cannot silently replace a bound resource.
6. Reports store metadata fingerprints, normalized types, null/key flags, bounded reason codes, and
   opaque references. They store no source rows, raw values, samples, SQL, parameters, prompts,
   tokens, DSNs, raw claims, or OpenAI material.
7. Definition, tag, term, and asset-metadata changes are review-required but may retain the same
   registry only after exact revalidation. Removal, ambiguity, physical-type, key, or nullability
   changes are blocking and require a corrected, strictly approved registry version and a new
   binding/revalidation cycle.
8. Runtime gating is dependency-aware: unrelated changes among thousands of catalog assets do not
   block a query whose exact mapping/join evidence is unchanged.
9. A blast-radius result is never labelled complete unless every managed workflow and query recipe
   dependency is indexed. Incomplete dependency coverage is visible and blocking for approval.
10. Join drift is not inferred from catalog metadata alone. Aggregate-only relationship profiles
    re-run the exact approved normalization plans with the existing read-only reader, timeout, and
    allowlist; no keys or source rows leave the source boundary.
11. M26 makes no LLM call. Short-description matching, free-text field selection, and dynamic Query
    Studio remain M27 scope.
12. A tenant may have governed bindings across many connections, but each profile job and
    executable plan must identify one common connection. M26 uses an explicit
    worker-per-workspace/connection deployment binding; M28 owns dynamic connector routing and
    federation.
13. Query-recipe current/version identities are scoped by semantic-registry scope fingerprint.
    Intent-only legacy markers cannot satisfy active-scope dependency coverage.
14. One dependency replacement is capped at 10,000 artifacts and 100,000 mapping/join edges.
    Exceeding a cap is incomplete coverage, never a truncated complete blast radius.

## Pure domain contract

Add `domain/semantic_change.py` with immutable, fingerprinted values for:

- catalog generation vectors and exact governed resource bindings;
- observed field evidence without raw data or unbounded metadata text;
- versioned aggregate-only join evidence baselines;
- closed change kinds and severities;
- immutable drift reports, finding summaries, impact summaries, and completeness;
- exact baseline/revalidation/rejection proposals, approvals, and decisions;
- current, review-required, blocked, revalidated, rejected, and superseded states.

Closed change kinds include:

- baseline or explicit binding missing;
- binding ambiguous or bound connection/asset/field removed;
- physical type changed;
- join-key status changed;
- nullability changed;
- field definition fingerprint changed;
- tags/glossary fingerprint changed;
- asset metadata fingerprint changed;
- active registry pointer/version changed;
- approved cardinality/fanout evidence changed, foreign-key evidence disappeared, normalized
  overlap changed materially, or null/invalid/multiplicity safety moved outside policy.

Every finding carries old/new evidence fingerprints, exact affected mapping decision, affected join
contract identities, bounded risks, severity, and a stable fingerprint. Reports bind scope,
active-pointer generation/fingerprint, registry version/fingerprint, observed catalog-generation
vector, all finding fingerprints, impact-set fingerprint/counts, dependency-index watermark, and
inspection time.

## Application contract

### Inspection

`InspectSemanticChange`:

1. loads the exact active pointer and strict registry version;
2. loads a prior approved evidence baseline, if one exists;
3. resolves or verifies only exact governed resource bindings under the same catalog scope;
4. observes current active-generation evidence for those bindings;
5. profiles each affected approved join with aggregate counts only under the exact allowlist;
6. classifies deterministic changes and their mapping/join impact;
7. derives paginated workflow/recipe impacts from the managed dependency index;
8. records one immutable idempotent report and its findings/impacts;
9. returns a safe typed report without changing registry, DataHub, or source data.

The first inspection creates a baseline-review report; it does not silently trust current catalog
state. Candidate lookup is evidence for a human binding decision, not semantic approval.

### Revalidation and remediation

`PrepareSemanticChangeDecision` creates an exact proposal over one current immutable report.

- `establish_baseline` is permitted only when every governed resource has one explicit binding and
  the dependency index is complete.
- `revalidate_compatible_change` is permitted only when no blocking finding exists.
- `reject_change` records rejection and leaves affected context blocked.
- blocking findings cannot be waived against the same registry. The operator must publish a
  corrected strict registry version through existing governed reviews, activate it through M23
  compare-and-swap, then inspect and approve its new evidence.

`CommitSemanticChangeDecision` validates the closed confirmation, actor, time, report/pointer/
catalog/dependency fingerprints, expected baseline revision, and compare-and-swap head. It
atomically appends the decision/audit record and advances the evidence baseline only for an
eligible approval. The accepted baseline and bindings store the resulting head revision. Replay is
exact and idempotent; stale approval performs zero writes.

### Execution and reuse gate

`AssertSemanticContextCurrent` receives the exact resolved plan dependencies and runs before the
compiler, preview, rejected-source inspection, or recipe reuse:

- the active pointer must still match the resolved plan;
- every used mapping/join must have a current approved explicit evidence binding;
- current catalog evidence for those exact resources must match the approved baseline;
- a pending blocking/review finding for any used resource rejects with one sanitized stale-context
  code;
- an unrelated changed resource does not block the plan;
- unavailable drift state or catalog evidence fails closed.

Recipe assessment gains a semantic-evidence staleness reason. Existing stale-recipe migration still
requires a newly completed workflow and explicit publication approval.

## Persistence and process boundaries

Add migration `0005_semantic_change_management.sql` with:

- immutable reports, findings, impacts, decisions, and audit events;
- a compare-and-swap scope head and versioned exact resource bindings;
- immutable aggregate-only join-profile baselines and current observations;
- an append-only generation-change ledger that survives inactive-generation pruning;
- a workflow/recipe semantic dependency index with completeness watermark;
- a durable scan-request queue created when a catalog generation or registry pointer changes;
- compound scope/status/keyset indexes and bounded retention fields;
- immutable-row/identity guards and exact shape constraints;
- no cascade that can erase accepted decisions or audit history.

Least privilege keeps the six existing roles; no seventh broad service role is added:

- `schemabridge_reconciler` reads exact active registry/catalog evidence, claims scans, and commits
  reports/decisions through bounded capabilities;
- `schemabridge_runtime` and `schemabridge_worker` can read only the current gate projection needed
  for exact plan dependencies; the worker may run only exact aggregate relationship profiles
  through the existing read-only source reader;
- `schemabridge_api` can read tenant-scoped sanitized report/finding/impact projections only;
- `schemabridge_catalog` can cause only an automatic scan request through the reviewed generation
  promotion trigger and cannot read semantic reports or decisions;
- `schemabridge_migrator` owns DDL and explicit repair/retention operations;
- `PUBLIC` receives no table, sequence, or function privilege.

Add a separately operated semantic-change reconciler/CLI. It does not auto-migrate, execute source
queries, write DataHub, call an LLM, or receive catalog route credentials.

## Authenticated and operator surfaces

Authenticated HTTP is read-only for M26:

- `GET /v1/semantic-changes/reports`
- `GET /v1/semantic-changes/reports/{report_id}`
- `GET /v1/semantic-changes/reports/{report_id}/findings`
- `GET /v1/semantic-changes/reports/{report_id}/impacts`

All lists use signed tenant/report/filter-bound keyset cursors and at most 50 items. Protected,
cross-tenant, stale, or unknown report identities use one indistinguishable unavailable boundary.
Responses expose no raw catalog definition, SQL, source values, credentials, or audit key.

Mutation remains an operator CLI flow:

1. inspect/prepare and print the exact report/proposal fingerprint;
2. separately approve with the closed confirmation and configured trusted actor;
3. commit through the reconciler credential;
4. inspect current head and immutable audit verification.

The Streamlit query UI receives no reconciler credential and no activation/reconciliation button.
A bounded read-only browser-acceptance panel may render the same authenticated HTTP projections;
it must be labelled acceptance instrumentation, not Query Studio.

## Deliverables

- Pure semantic-change domain, ports, inspection/decision/gate use cases, and typed failures.
- Schema-v5 migration, PostgreSQL evidence/report/impact/decision/scan stores, six-role matrix, and
  audit-chain verification.
- Exact M25 catalog evidence reader that uses bound identities and active generations without
  scanning all tenant assets.
- Workflow and recipe dependency indexing plus completeness reconciliation.
- Pre-compiler/pre-source execution and recipe-reuse gate.
- Read-only authenticated HTTP list/detail routes with bounded cursors.
- Operator CLI/reconciler process with schema-only startup, leases/fencing, idempotency, and
  graceful shutdown.
- Heterogeneous deterministic baseline and change fixtures: unchanged, description/tag-only,
  type/key/nullability drift, removal, homonym, ambiguity, and unrelated 5,433-asset changes.
- Real PostgreSQL/DataHub integration, process, package, audit, and internal-browser acceptance.
- ADR, architecture, security, domain, query, testing, deployment, runbook, UI, browser, state,
  decision-log, current-task, work-queue, and milestone handoff updates.

## Implementation sequence

1. Specify immutable domain/fingerprint/policy contracts and adversarial unit tests.
2. Add schema v5, grants, immutable guards, scan queue, and PostgreSQL integration tests.
3. Implement exact catalog evidence capture, first-baseline inspection, and deterministic drift.
4. Implement impact indexing/pagination and prove completeness/incompleteness behavior.
5. Implement exact decision prepare/approve/commit, CAS replay, rejection, and audit verification.
6. Inject dependency-aware gating before compiler/source I/O and into recipe reuse/migration.
7. Add read-only HTTP and operator CLI/reconciler composition.
8. Bind profile jobs and workers to workspace plus `connection_id`, fan out catalog scans to
   matching active registries, and prove dependency lease continuation across every bounded page.
9. Run real generation-change, registry-remediation, service/process, package, browser, and full
   quality/coverage gates.
10. Consolidate exact evidence and limitations before accepting M26.
11. Rewrite initial-candidate and approved-evidence reads locator-first and prove bounded
    rows/plan/timeout at 5,434+ assets.
12. Make every managed stale/legacy workflow/recipe explicit or incomplete, and prove realistic
    non-empty dependency fanout against PostgreSQL.
13. Bind profile claim/reclaim and pre-source validation to workspace plus connection.

## Acceptance criteria

- [x] The first managed inspection cannot silently establish trust; it produces a review-required
      baseline with exact proposed bindings and requires explicit approval.
- [x] Every approved binding identifies workspace, catalog scope, connection, asset, field path,
      active generation, mapping decision/version, and evidence fingerprint.
- [x] Missing, disabled, removed, stale, out-of-scope, or ambiguous bindings fail closed without
      choosing by name similarity.
- [x] An unchanged catalog generation and an equivalent later generation produce no false drift.
- [x] Definition/tag/term/asset-metadata changes are review-required and may be revalidated only by
      an exact current report approval.
- [x] Type, join-key, nullability, removal, ambiguity, or registry-contract changes are blocking
      and cannot be waived against the same registry version.
- [x] An approved one-to-one/one-to-many relationship becoming many-to-many, losing declared-FK
      evidence, gaining invalid/null keys, or exceeding its multiplicity/overlap policy is blocking;
      only aggregate counts/fingerprints are retained.
- [x] A corrected strict registry version plus exact new evidence can be activated through M23 and
      establishes a new approved baseline without rewriting history.
- [x] Drift inspection reads only governed resources and bounded candidate counts; changing an
      unrelated asset among 5,434 does not inspect/materialize the full catalog or block an
      unaffected plan.
- [x] Blast radius deterministically traverses mapping → join → workflow/recipe, deduplicates
      impacts, exposes bounded pages, and binds counts plus a set fingerprint to the report.
- [x] Missing/incomplete dependency indexing is visible and prevents a false “complete” report or
      approval.
- [x] A stale report, pointer, catalog evidence, dependency watermark, baseline revision, actor,
      time, confirmation, or approval fingerprint causes zero state/audit mutation.
- [x] Exact decision replay is idempotent; a concurrent compare-and-swap has one winner.
- [x] Rejection preserves the blocked state and immutable evidence; approval never edits source
      data, DataHub documents, or the active registry in place.
- [x] Affected planning/execution/retry/recipe reuse fails before compiler, AST guard, preview,
      rejected-source, or source-database I/O with a sanitized stale-context code.
- [x] An unaffected plan continues through the unchanged compiler/guard/read-only execution path.
- [x] Catalog and registry activation automatically enqueue a durable idempotent scan request;
      crashes, lease expiry, stale fencing, and duplicate delivery cannot lose or double-apply a
      report.
- [x] One catalog promotion fans out to every matching active registry scope; a 5,434-artifact
      dependency traversal renews its lease between pages and lease loss cannot advance coverage.
- [x] Initial-candidate and approved-evidence reads are locator-first; reviewed PostgreSQL plans
      and rows-read facts prove unrelated rows in a 5,434+ asset connection are not materialized,
      and initial ambiguity returns at most two exact witnesses per mapping.
- [x] A stale workflow, stale scoped recipe, or legacy intent-only recipe cannot be silently
      omitted while dependency coverage is `complete`.
- [x] Real PostgreSQL reconciliation of 5,434 artifacts with non-empty dependency fanout remains
      within the recorded heap/RSS, transaction-time, and statement-timeout budget.
- [x] Every aggregate profile job carries one exact `connection_id`; a worker cannot claim another
      connection, and joins whose endpoints bind to different connections fail before source I/O.
- [x] Profile claim/reclaim and pre-source validation are workspace- and connection-qualified; two
      workspaces reusing one connection ID cannot cross-claim or open the wrong source.
- [x] Active DataHub query-recipe current/version IDs are scope-qualified; identical intent in two
      scopes cannot collide or enter the wrong blast radius.
- [x] HTTP report/finding/impact pages are tenant-scoped, at most 50 items, cursor-bound, and return
      indistinguishable denial for unknown/cross-tenant identities.
- [x] API/UI/worker/indexer environments contain no reconciler signing key, catalog route secret,
      source write credential, or OpenAI key.
- [x] Schema v4→v5 and pristine migration pass; all six role boundaries and forbidden grants are
      proven, and runtime processes refuse schema mismatch without auto-migration.
- [x] Real PostgreSQL/DataHub acceptance proves baseline approval, compatible change revalidation,
      blocking key/type/removal drift, corrected-registry recovery, and complete blast radius.
- [x] Internal-browser desktop and 390x844 acceptance shows current/review/blocked/remediated states,
      paginated findings/impacts, role-denied mutation absence, stale-review denial, clean console,
      no horizontal overflow, and zero protected-data hits.
- [x] Focused, integration, acceptance, evaluation, package, release-audit, `make check`, coverage,
      and `git diff --check` gates pass and exact results are recorded.

## Required automated checks

```bash
pytest tests/unit/test_semantic_change.py \
  tests/unit/test_semantic_change_use_cases.py \
  tests/unit/test_semantic_change_gate.py \
  tests/unit/test_http_semantic_change_api.py \
  tests/unit/test_semantic_change_cli.py \
  tests/unit/test_semantic_change_operator.py \
  tests/unit/test_semantic_change_reconciler.py \
  tests/unit/test_semantic_change_scan_runner.py \
  tests/unit/test_semantic_change_scans.py \
  tests/unit/test_semantic_change_schema_migration.py \
  tests/unit/test_semantic_dependency_reconciler.py \
  tests/unit/test_semantic_profile_bootstrap.py \
  tests/unit/test_semantic_profile_jobs.py \
  tests/unit/test_semantic_profile_process.py \
  tests/unit/test_semantic_profile_worker.py \
  tests/unit/test_semantic_reconciler_bootstrap.py \
  tests/unit/test_semantic_reconciler_process.py \
  tests/unit/test_m26_semantic_change_browser_panel.py \
  tests/unit/test_m26_browser_acceptance_runtime.py
make control-plane-reset
make control-plane-migrate
make control-plane-check
pytest -m integration tests/integration/test_semantic_change_postgres.py
pytest -m integration tests/integration/test_semantic_change_scans_postgres.py \
  tests/integration/test_semantic_profile_queue_postgres.py
pytest -m acceptance tests/acceptance/test_semantic_change_acceptance.py
make test-api-integration
make test-worker-integration
make test-integration
make test-acceptance
make evaluate
make runtime-wheel-smoke
python scripts/release_audit.py
make check
make coverage
git diff --check
```

## Manual internal-browser test

1. Migrate a clean local control plane to v5 and start signed OIDC, API, semantic reconciler, and
   the read-only acceptance panel under distinct roles.
2. As the small tenant, inspect the initial baseline report and exact proposed bindings, approve it
   through the separate CLI, and refresh the browser to current.
3. As the 5,434-table tenant, page first/middle/final findings/impacts and prove only governed
   resources were observed.
4. Promote a generation with an unrelated asset change and verify the governed request stays
   current; then change a governed definition and observe review-required state.
5. Attempt stale/cross-tenant report/cursor/approval reuse, then perform an exact compatible
   revalidation and observe one immutable decision.
6. Promote type/key/nullability/removal drift and verify blocking state with compiler/source
   counters still zero.
7. Publish and activate a corrected strict registry version, inspect/approve its new evidence, and
   verify the request becomes eligible again while the old report remains immutable.
8. Repeat at 390x844; inspect browser/API/process text, control state, and logs for protected data,
   verify no warning/error console entries, and verify no horizontal overflow.

## Explicit non-goals

- Free-text/description matching, LLM candidate ranking, dynamic guided field selection, or
  arbitrary natural-language multi-domain Query Studio; M27 owns them.
- Cross-connection joins, connector credential routing, dialect expansion, federation, EXPLAIN
  cost budgets, or source-specific type coercion; M28 owns them.
- Production observability/SIEM, HA/failover, remote secret manager operation, SBOM/provenance, or
  SLO claims; M29–M31 own them.
- Automatically approving semantic equivalence, silently replacing a removed field, editing an
  immutable registry, or executing LLM-produced SQL.

## Handoff requirements

Update `tasks/PROJECT_STATE.md`, `tasks/CURRENT_TASK.md`, `tasks/DECISION_LOG.md`, the M26 ADR,
architecture/security/domain/query/test/deployment/runbook/UI/browser docs, service/deployment
examples, and exact browser record. Return `tasks/HANDOFF_TEMPLATE.md` with migration checksum,
six-role matrix, report/binding/decision fingerprints, scan lifecycle, blast-radius counts,
pre-I/O gate evidence, real service commands/results, warnings, omitted drills, known limitations,
and one proposed commit message.
