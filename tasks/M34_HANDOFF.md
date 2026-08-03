# M34 milestone handoff

## Summary

- Milestone: M34 — Governed registry publication and activation handoff
- Status: complete; accepted locally
- Recommended operator decision: accept the bounded local milestone; retain production/release
  **NO-GO**
- Proposed commit message: `feat: add governed registry publication`

## Implemented

- Registry format v2 carries exact workspace, connection, catalog generation/vector, locator,
  observed DataHub dataset URN, physical type and metadata fingerprints for every active mapping.
- A pure additive assembler creates a first zero-join registry or appends one non-colliding M33
  model to a strict v2 base; missing authority, v1 base, drift and collisions fail closed.
- Schema v14 persists a unique target reservation, bounded job/event payloads, database-time
  leases, capability digests, fencing, heartbeat, retry/dead-letter and cooperative cancellation.
- Authenticated tenant-bound HTTP endpoints submit, inspect, authorize and cancel jobs without a
  DataHub credential. Public schemas omit capabilities, DSNs, tokens and source values.
- The isolated publisher revalidates authority, requires a fresh approval of the complete
  candidate, observes before writing, writes only an absent target, and accepts success only after
  exact typed read-back.
- M34 DataHub identity requires the exact actor, exactly `manageDocuments`, and zero target-edit
  grants. Historical local M22 residual privileges are not accepted for registry-v2 publication.
- The explicit pre-write database heartbeat is the cooperative-cancellation boundary. Once crossed,
  exact read-back wins over a cancellation label.
- The v2 `activation_ready` handoff is loaded through a bounded PostgreSQL function. M23 repeats
  catalog authority validation inside the workspace-locked pointer CAS; publication itself cannot
  activate.
- A separate publisher Deployment/ServiceAccount/PDB/Service/ServiceMonitor/NetworkPolicy and
  exact-version writer binding are present. Readiness resolves no writer secret; the publication
  queue is included in aggregate observer metrics.
- One internal-browser M34 journey and one advanced M32 Spanish-to-copyable-SQL journey were
  completed manually with empty browser logs and no automatic execution/activation.

## Files changed

- `src/schemabridge/domain/registry_publication*.py`, `semantic_registry.py`,
  `physical_types.py`: v2 authority, candidate, approval, receipt and durable lifecycle contracts.
- `src/schemabridge/application/registry_publication*.py` and ports: tenant use cases, worker,
  fencing, cancellation and activation-ready handoff.
- `src/schemabridge/adapters/control_plane/postgres_registry_publication.py`,
  `postgres_registry_control.py`, `migrations/control_plane/0014_registry_publication.sql`: durable
  queue, exact M23 authority and least-privilege grants.
- `src/schemabridge/adapters/semantic_registry/datahub.py` and remote-secret adapters: observed-URN
  v2 document, document-only identity, immutable write/read-back and exact-version secret handling.
- `src/schemabridge/entrypoints/http/`, `entrypoints/registry_publisher/`, `bootstrap.py`,
  `config.py`: authenticated API and isolated process composition.
- `deploy/kubernetes/m29/` and `Makefile`: publisher workload, network/secret/metrics contract,
  probes and clean-environment targets.
- `tests/unit/test_registry_publication*`, `test_datahub_semantic_registry_v2.py`,
  `tests/integration/test_registry_publication_postgres.py`, live DataHub v2 extension and
  `tests/acceptance/test_m34_registry_publication.py`: required M34 matrix.
- `scripts/m34_registry_publication_scenario_app.py`, product/security/runbook/browser docs, ADR,
  plan and task state: reproducible evidence and release truth.

## Commands executed

| Command | Result | Notes |
|---|---|---|
| `make runtime-wheel-smoke` | pass | Clean wheel validates migrations 1–14 and every runtime entrypoint |
| `make install` | pass after approved retry | First sandboxed run could not write the external uv cache; approved retry refreshed the editable entrypoint |
| `make control-plane-reset && make control-plane-migrate && make control-plane-check` | pass | Recreated only the synthetic control volume; all nine credentials report schema v14 |
| `make registry-publisher-probe` | pass after fixes | Initial stale editable install lacked the binary; next run exposed and fixed unopened-pool/metric-contract readiness bugs |
| Required M34 unit/integration/acceptance matrix | pass with external skips | 118 passed, 5 live-DataHub skips in 3.02 s |
| Fresh PostgreSQL publication/M23/recipe/observer matrix | pass | 17 passed in 9.67 s |
| Deployment/runtime/metrics matrix | pass | 215 passed in 14.48 s |
| `make test-integration` | pass with external skips | 171 passed, 12 explicit skips in 162.54 s after correcting one stale v13 expectation |
| `make test-acceptance` | pass with external skips | 56 passed, 4 explicit external-DataHub skips in 38.14 s |
| `./.venv/bin/mypy src` | pass | 343 source files |
| First `make check` | fail, corrected | Supply chain/Ruff passed; mypy exposed an omitted explicit compatibility re-export |
| First hosted CI on `7b8e6b2` | fail, corrected | The legacy v1 `registry-publish` path rejected the stock M22 target grant after the v2 privilege split; the v1 contract was restored without weakening v2 |
| Legacy/v2 DataHub privilege regression | pass | 56 tests prove the historical v1 grant is tolerated while v2 remains document-only |
| Final `UV_CACHE_DIR=.local/uv-cache make check` | pass | 3,779 passed, 238 deselected; repeated after handoff completion on the exact commit candidate |
| `git diff --check` | pass | No whitespace errors on the exact commit candidate |

## Automated test results

- Focused tests: 118 passed, 5 skipped; includes exact document-only writer rejection and all
  required M34 unit/integration/acceptance files.
- `make check`: pass on the exact commit candidate: supply-chain static policy, release audit,
  Ruff format/lint, mypy over 343 source files, and 3,779 unit tests with 238 explicit
  deselections. A repository-local uv cache avoids relying on sandbox-external cache permissions.
- Integration tests: full integration gate passes 171 with 12 explicit external/retained-fixture
  skips. The 17 fresh M34/M23/recipe/observer cases pass; all 5 exact live DataHub-v2 cases skip
  because reader/writer credentials are absent.
- Acceptance tests: full gate passes 56 with 4 external-DataHub skips; both M34 journeys pass.
- Deployment/runtime/operational tests: 215 pass.
- Runtime wheel: pass; installed artifact exposes the publisher executable and migrations 1–14.
- Coverage: not rerun for M34; coverage is not an M34 acceptance criterion.

## Operator manual test

1. Started `scripts/m34_registry_publication_scenario_app.py` on loopback with a synthetic token.
2. Reserved the exact M33 proposal and observed `queued`, zero writes/versions and no active
   pointer.
3. Ran preparation, inspected the complete candidate and authorized the closed confirmation.
4. Ran publication and inspected the terminal receipt.
5. Separately opened Query Studio, entered the M32 advanced Spanish reference request, confirmed
   its typed interpretation and inspected the standalone SQL.

Expected and observed result:

```text
M34: queued → leased → awaiting_approval → approved → leased → activation_ready
DataHub writes: 1; immutable versions: 1; active pointer: not_configured
related asset: urn:li:dataset:(urn:li:dataPlatform:postgres,opaque-9f82,PROD)
browser errors/warnings: []

M32: PostgreSQL; plan v2; 106 lines; aggregated + windowed CTEs; 2 joins;
COUNT DISTINCT + HAVING + ROW_NUMBER + percentage + running sum + top-3 filter;
two AST validations; placeholders: none; executed: false; browser errors/warnings: []
```

The full-page Streamlit screenshot rendered blank in the browser tool, so DOM snapshots and
browser logs—not screenshot quality—are the retained manual evidence. Browser tabs and both local
servers were closed cleanly.

## Architecture and security review

- Dependency direction: domain remains pure; application depends on ports; PostgreSQL/DataHub and
  process concerns stay in adapters/entrypoints; `bootstrap.py` remains the composition root.
- Source database writes: none. The only external mutation is the approval-bound immutable DataHub
  document through the publisher.
- SQL/LLM validation: M34 invokes neither an LLM nor source SQL. M32 still compiles deterministically
  from typed intent and validates the final SQL AST twice before returning a copy artifact.
- DataHub mutation approval: M33 approval is insufficient; a fresh M34 publisher approves the
  complete exact candidate. Publication never activates.
- Secrets/proprietary data: synthetic data only; API/web/normal worker cannot resolve the writer
  secret; publisher cannot resolve source, LLM, OIDC or active-pointer capabilities.
- Fanout/semantic risks: M34 preserves approved mappings/joins but does not infer new joins. Query
  compilation retains the three-table/two-join and explicit fanout-mitigation bounds.
- Independent review: initial review found first-target handling, missing live v2 coverage,
  pre-write cancellation and release-truth gaps; all local P0/P1 findings were corrected. Final
  review retains only the explicit external DataHub evidence gap.

## Decisions made

- Decision: add registry format v2 and never derive DataHub authority from a physical name.
- Decision: separate API authorization, publisher mutation and M23 activation into different
  credentials and durable transitions.
- Decision: require an exact document-only M34 writer, even though historical local M22 policy
  tolerates broader stock DataHub grants.
- Decision: accept M34 only as bounded local evidence and keep global production/release NO-GO.
- Reason: immutable authority, least privilege, replay safety and honest commercial claims require
  these boundaries.
- Logged in: `docs/adr/0017-governed-registry-publication.md` and D128.

## Known limitations or unverified items

- The exact live DataHub-v2 test did not run locally. Its 5 skips are explicit; first-target IAM,
  write/read-back and document-only policy still need evidence on the operated instance.
- Only PostgreSQL output for the same governed context is supported. MySQL, SQL Server, Oracle,
  Snowflake, BigQuery and federated/cross-destination output are unsupported.
- Registry authoring is initial/additive one-model only. New joins, replacement/removal and batch or
  incremental import are not implemented.
- The historical M26 live acceptance that activates format-v1 replacement registries is now an
  explicit superseded skip. M34 keeps v1 readable but never activation-ready; re-enabling that
  lifecycle requires a typed v2 replacement contract and the dedicated document-only publisher.
- A catalog may contain thousands of assets, but one query remains limited to three tables and two
  joins; M34 adds no high-volume queue/catalog or multi-tenant load proof.
- Live-provider holdout quality, independent security testing, external IAM/TLS/secret rotation,
  alert/SIEM delivery, SLOs, legal/privacy controls and an operated pilot remain open.

## Blockers

- No blocker to committing and publishing the bounded local M34 implementation.
- Production/commercial release remains blocked on the unverified items above, M30 and M31.

## Next milestone readiness

- Dependencies satisfied: local M29, M32, M33 and M34 implementation gates.
- Recommended next prompt: execute M30 production evaluation and independent security
  certification only after exact live DataHub-v2 and hosted M34 evidence exists.
- Required operator prerequisites: document-only DataHub publisher identity, real secret manager and
  rotation/revocation, authorized holdout corpus, target-cluster admission/NetworkPolicy, external
  observability/recovery, legal/privacy owner and an operated pilot plan.
