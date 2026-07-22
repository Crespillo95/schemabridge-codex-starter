# M16 independent audit: DataHub integration and governance

Mode: bounded read-only review; the primary subagent made no edits. Two later read-only diagnostic
subagents reproduced clean-reset failures; the main agent integrated the fixes sequentially.
Snapshot: pre-fix M16 working tree plus clean-reset observations, 2026-07-21/22.

## Ranked findings

### DH-001 — High — Dataset URNs from another environment were accepted

- Evidence: catalog translation validated platform/database/name but not the URN environment.
- Reproduction: return an otherwise matching `DEV` URN while the adapter is configured for `PROD`.
- Risk: evidence from another environment could be presented as production governance context.
- Proposed regression: omit/reject every environment mismatch.
- Disposition: accepted and fixed at `src/schemabridge/adapters/datahub/catalog.py:414-424` with
  `test_search_omits_assets_from_another_datahub_environment`.

### DH-002 — High — Publication audit facts are not unified per target

- Evidence: exact approval/decision payload and per-target outcomes persist, but actor/time and an
  explicit old/new fingerprint summary are distributed across records rather than colocated for
  every target.
- Reproduction: inspect canonical, join, workflow, and recipe documents/results and attempt to
  answer actor/time/old/new/result from one persisted per-target record.
- Risk: governance review requires joining evidence and cannot prove the complete transition from
  a single target audit fact.
- Proposed regression: persist and read back one immutable per-target audit record with actor,
  timestamp, old/new fingerprints, operation, result, and approval/decision IDs.
- Disposition: accepted as `GOV-001` and explicitly blocks release; it was not silently downgraded.

### DH-003 — High — Release procedure did not prove governed state after restart

- Evidence: individual integration tests covered writes/read-back, but no release command reset,
  wrote all families, restarted DataHub, and reread them.
- Reproduction: follow the former release steps and note the missing post-restart checks.
- Risk: repaired-state tests could conceal ordering or persistence defects.
- Proposed regression: include catalog/MCP plus canonical/join/recipe read-back after restart.
- Disposition: accepted and fixed in `scripts/release_clean_room.sh:76-88`; the final clean run passed
  all three focused live read-backs.

### DH-004 — Medium — Stock policy permits personal-token generation

- Evidence: DataHub's stock local all-users policy exposes personal-token generation to the scoped
  identity even though SchemaBridge's custom policies are bounded.
- Reproduction: inspect the effective privilege set returned during writer verification.
- Risk: upstream ambient permission is broader than SchemaBridge needs.
- Proposed regression: remove/override the stock grant when supported and assert the exact set.
- Disposition: retained; SchemaBridge never invokes token mutation and MCP mutation tools are absent.

### DH-005 — Medium — Join immutable-document conflict detection is weaker than recipes

- Evidence: deterministic join versioning is idempotent but has less explicit read-before-write
  conflict handling than query recipes.
- Reproduction: precreate the version URN with different content and replay publication.
- Risk: an external collision can be reported less precisely.
- Proposed regression: require an exact fingerprint match or a typed immutable conflict.
- Disposition: retained in the unresolved-risk register.

### DH-006 — High — Health could select an unrelated GMS-like container

- Evidence: broad container matching could observe another local Compose project.
- Reproduction: run another container with a matching name and compare the selected component.
- Risk: false health success against the wrong service.
- Proposed regression: require exact Compose project, service, and component labels.
- Disposition: accepted and fixed in the DataHub health scripts; clean reset/restart passed.

### DH-007 — High — Fresh authorization was checked before the policy cache converged

- Evidence: pinned Core v1.6.0 uses a 120-second policy cache; fresh admin/writer verification ran
  within seconds and returned missing privileges although policies were active.
- Reproduction: `make datahub-reset`, then immediately initialize/provision identities.
- Risk: nondeterministic clean-machine failure and misleading permission diagnosis.
- Proposed regression: retry only missing privileges past 120 seconds; fail immediately on actor
  mismatch or unexpected grants.
- Disposition: accepted and fixed with 31 × 5-second bounded retries in
  `scripts/datahub.sh:84` and `scripts/provision_datahub_writer.py:38-39,232-274`; unit and final live
  clean-reset evidence passed.

### DH-008 — High — Fresh publication depended on canonical writeback running first

- Evidence: recipe and relationship documents assigned `io.schemabridge.decisionRef`, but only the
  canonical writer created that structured property; GMS returned HTTP 422 on a clean reset.
- Reproduction: publish recipe and relationships before canonical metadata on fresh DataHub.
- Risk: hidden test/order dependency and partial governed publication.
- Proposed regression: every approved write family independently ensure/verify the shared property.
- Disposition: accepted and fixed through
  `src/schemabridge/adapters/datahub/decision_property.py`; all three adapters call it and the clean
  integration order recipe → relationships → canonical passed.

### DH-009 — Low — Terminal writer verification can orphan an unsaved token

- Evidence: the one-month token is minted before final verification and local state is saved only
  after success.
- Reproduction: force all bounded verification attempts to fail after token creation.
- Risk: a still-valid token may require manual revocation even though no credential is committed.
- Proposed regression: add bounded revocation or return a safe revocation identifier.
- Disposition: retained as a documented residual risk; no token is printed or saved on failure.

## Conclusion

Clean reset, ingest, scoped read/write, MCP read-only behavior, and restart persistence passed. The
unified per-target audit fact remains a high release blocker; other retained risks are explicit.
