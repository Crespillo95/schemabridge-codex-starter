# M22: Complete live DataHub context loop

- Status: complete — recommended for operator acceptance
- Timebox: 10 hours
- Recommended Codex: GPT-5.6 Sol — Extra High (xhigh)
- Dependencies: locally verified M20 identity boundary and M21 atomic registry contract

## Objective

Load the complete governed semantic registry from bounded, typed DataHub read-back so guided
requests, planning, execution, evaluation, and the UI no longer require a recorded planning
bundle when live mode is selected. The live adapter must reconstruct one atomic snapshot containing
all seven synthetic logical models, 31 mappings, five joins, transformations, decisions, evidence,
risks, versions, scope, and provenance without fallback.

The operator's continuing productionization instruction permits M22 work on the locally verified
M21 tree. It does not turn the dirty tree into release evidence or accept the pending M17/M18
external artifacts.

## Scope assumptions

1. M22 publishes and reads one explicitly configured immutable registry version. M23 owns a mutable
   active pointer, registry migrations, rollback, key rotation, and reconciliation.
2. The DataHub registry document is self-contained, decision-bound, approval-gated, audit-recorded,
   and linked to the exact governed physical assets. It contains no SQL, samples, credentials,
   tokens, prompts, results, or private data.
3. `recorded` and `live` are explicit deployment modes. A requested live registry never consults
   the manifest adapter after any missing, malformed, partial, unauthorized, or unavailable read.
4. MCP remains mutation-disabled. The bounded DataHub SDK writer is used only through an exact
   typed approval and the common append-only publication-audit ledger.

## Deliverables

- Add pure registry-publication approval/result contracts and a use case that validates the exact
  registry fingerprint, complete semantic-decision set, actor/time, DataHub target, and returned
  audit record before appending it.
- Add a bounded DataHub version-document publisher. It rejects immutable-content conflicts, writes
  only the exact approved snapshot, verifies post-write read-back, and has no current-marker write.
- Add a read-only `DataHubGovernedSemanticRegistry` adapter using a separate DataHub read
  credential. It loads only the deterministic configured URN and validates payload size, typed
  snapshot, scope, version, fingerprint, approval, audit, decision closure, and related assets.
- Add an explicit semantic-registry mode and version to typed configuration. Hosted demo remains
  recorded; staging/production must use a live registry with live catalog mode; local live use
  requires the existing development-only opt-in.
- Compose the live adapter through the existing `GovernedSemanticRegistryPort` and propagate it to
  guided intent, workflow, execution, evaluation, and UI without changing their inward contracts.
- Add one local CLI operator flow to prepare/show the exact publishable snapshot, require the
  fingerprint as confirmation, publish it through the approval/audit path, and verify fresh-process
  read-back.
- Extend live integration and acceptance coverage to plan and execute the north-star and commerce
  cases from DataHub-only context. Prove the manifest can be unavailable and is never read.
- Show registry mode, DataHub source, configured version, full fingerprint, and counts in the UI.

## Implementation sequence

1. Define pure publication contracts, target identity, and application port/use case.
2. Add fake and DataHub version-document adapters plus adversarial read-back tests.
3. Add typed configuration and bootstrap selection with fail-closed managed-profile rules.
4. Add CLI preparation/publication/read-back and common-ledger integration.
5. Publish the synthetic registry to local DataHub with an explicit synthetic approval.
6. Prove DataHub-only planning/execution through integration, acceptance, evaluation, and UI.
7. Run full quality, service, coverage, browser, release-audit, and diff gates.
8. Update architecture, security, runbooks, state, decisions, ADR, and handoff evidence.

## Acceptance criteria

- [x] A fresh live adapter reconstructs exactly 7 models, 31 mappings, and 5 contracts from the
      immutable DataHub version document with no file/manifest access.
- [x] Registry ID, version, catalog scope, canonical fingerprint, approval identity, every active
      semantic decision, publication audit, and related physical assets agree exactly.
- [x] Missing, oversized, truncated, malformed, extra-field, wrong-scope, wrong-version,
      wrong-fingerprint, stale-decision, bad-audit, wrong-related-asset, conflicting immutable
      target, permission, and outage cases fail with stable sanitized errors and no fallback.
- [x] Publishing requires an exact typed approval; no approval/mismatch performs zero mutation,
      successful publication is post-write verified and audit-appended, and replay is idempotent.
- [x] MCP exposes no mutation tools and the registry reader credential cannot perform governed
      document mutation.
- [x] Hosted demo remains recorded. Staging/production cannot boot with a recorded registry or a
      live registry combined with a recorded catalog. Local live mode requires explicit opt-in.
- [x] North-star and commerce requests compile, pass the AST guard, execute read-only with the
      existing 5000 ms/table/result bounds, and match exact ground truth using DataHub-only context.
- [x] Replacing, corrupting, or making the DataHub registry unavailable after confirmation yields
      `stale_registry`/typed context failure before preview and rejection I/O.
- [x] Internal-browser acceptance visibly reports `live:datahub`, the exact registry identity and
      7/31/5 counts, executes the governed north-star result, shows no warning/error console entries,
      and has no horizontal overflow at a narrow viewport.

## Required automated checks

```bash
pytest tests/unit/test_datahub_semantic_registry.py \
  tests/unit/test_semantic_registry_publication.py \
  tests/unit/test_auth_config.py tests/unit/test_governed_execution.py
make datahub-health
make datahub-catalog-check
make datahub-mcp-check
make test-integration
make test-acceptance
make evaluate
make check
make coverage
python scripts/release_audit.py
git diff --check
```

## Verified completion evidence

The final M22 run on 2026-07-23 produced:

- focused publication, live-reader, configuration, and governed-execution selection: `71 passed`;
- `make datahub-registry-check`: PASS for 7 models, 31 mappings, 5 joins, and 37 decisions;
- exact CLI publication replay: `already_current`, approval
  `registry-publication-v1-317d364ead8a8f4d946a91a699e37c60cfd5438dec5163342a06d56ad2c5859f`;
- `make test-integration`: `35 passed`;
- `make test-acceptance`: `15 passed`;
- `make evaluate`: PASS with synthetic-data digest
  `487388495115265d5fac5a17675e05c236fd4eb4c430613ef02050a5f6010654`;
- `make check`: `577 passed`;
- `make coverage`: `627 passed`, `80.66%`;
- development release audit: PASS over 402 files and 19 licenses, with the expected dirty-worktree
  release warning;
- `git diff --check`: PASS.

## Manual test for the operator

1. Publish the explicitly prepared synthetic registry version using its displayed fingerprint.
2. Start the UI in local-development live catalog/live-registry/live-reader mode.
3. Verify Overview and Semantic Models show `live:datahub`, version 1, the full fingerprint, and
   7/31/5 counts.
4. Complete the north-star workflow and verify exact `2, 1, 1` results, three rejection classes,
   `schemabridge_reader`, read-only execution, and 5000 ms timeout.
5. Inspect a clean browser tab and a narrow viewport for console errors, secret/PII leakage, and
   horizontal overflow.

Verified in Codex's internal browser at `http://127.0.0.1:8510`: context `live:datahub`, registry
`synthetic_enterprise` version 1, fingerprint
`ef480eb7370924ff4c94131a2c6c4063d85652cc83aaec9059538cdc2cf9c1b9`, and 7/31/5 counts were
visible. Workflow `m20-0c1928cea6a54f98b37fd013d47d8bbf` confirmed ambiguity
`distinct_or_relationship_count` at revision `r21` and returned
`2026-01-01=2`, `2026-01-02=1`, and `2026-01-03=1`. The rejection report showed
`non_integral_identifier` for `127.5`, `non_finite_identifier` for `NaN`, and `null_join_key` for
`NULL`; execution reported `read_only=True`, `truncated=False`, reader `schemabridge_reader`, and
timeout `5000ms`. The browser console result was `[]`, and at 390x844
`documentElement.scrollWidth == documentElement.clientWidth == 390`.

## Explicit non-goals

- An active/current registry pointer, mutable activation, migration, rollback, or reconciliation;
  M23 owns these controls.
- Authenticated HTTP APIs, workers, queues, quotas, or concurrency controls; M24/M25 own them.
- Registry-wide free-text matching or dynamic query controls; M27 owns that surface.
- Broad DataHub search, arbitrary document loading, MCP mutation, source writes, or raw LLM SQL.

## Handoff requirements

Update `tasks/PROJECT_STATE.md`, `tasks/CURRENT_TASK.md`, `tasks/DECISION_LOG.md`, the M22 ADR,
relevant architecture/security/deployment/runbook/UI documents, and examples. Return
`tasks/HANDOFF_TEMPLATE.md` with exact DataHub target/fingerprint, commands, browser evidence,
limitations, and one proposed commit message.
