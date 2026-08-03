# M30 Phase 1b: external control-policy validation

## Status and safety boundary

This slice prepares the external M30 control policy and proves that it is structurally bound to an authenticated campaign manifest. It does not authenticate control receipts, pass a control, authorize campaign I/O, or make SchemaBridge commercially available.

Every successful validation report is deliberately limited to:

- `policy_bound_to_authenticated_manifest=true`;
- `external_policy_trust_authenticated=false`;
- `receipt_authentication_enabled=false`;
- `external_controls_passed=0` of 24;
- `campaign_executable=false`;
- `release_decision=no_go`.

The CLI has no provider, source-database, target-database, hidden-corpus, DataHub, or deployment capability. Source databases remain read-only throughout the wider product, and this slice cannot reach them at all.

## What the policy freezes

The canonical external JSON policy binds the exact campaign, authenticated manifest, candidate repository/revision, policy validity window, authorization stages, 24 controls, prerequisite DAG, receipt kinds, producer identities, evidence subjects, and owner roles/quorum.

Each hosted workflow must be an immutable workflow in a repository distinct from the candidate repository. Candidate-repository comparison is case-insensitive. The policy cannot grant source write, raw-LLM-SQL execution, or blanket DataHub write authority.

Only `signed_campaign_manifest` has a pure contract-level adjudicator in this slice. That function is not connected to the CLI or an application port because a real external trust root, cryptographic receipt authenticator, durable anti-replay ledger, trusted clock, and raw-snapshot derivation are still missing. The other 23 controls are representable only as `admitted_unadjudicated`.

## Operator workflow

1. Complete Phase 1a and obtain the canonical campaign manifest plus its downloaded GitHub attestation bundle outside the repository.
2. Obtain the canonical control-policy JSON from the future independent policy authority. Do not author or amend it in the candidate repository.
3. Inspect the structural schema if needed:

   ```bash
   make m30-control-policy-schema
   ```

4. Validate the exact external files:

   ```bash
   make m30-control-policy-validate \
     M30_MANIFEST=/absolute/external/path/campaign-manifest.json \
     M30_ATTESTATION_BUNDLE=/absolute/external/path/manifest-bundle.json \
     M30_CONTROL_POLICY=/absolute/external/path/control-policy.json \
     M30_CONTROL_POLICY_OUTPUT=/absolute/external/path/policy-report
   ```

5. Inspect both deterministic artifacts written to the output directory. A successful command proves only the policy binding described above. It is not evidence that any of the 24 controls passed.

Input files must be regular, canonical JSON files outside the repository. Symlinks, non-canonical bytes, repository-contained evidence, unknown fields, altered hashes, missing assignments, policy/campaign mismatch, an invalid DAG, unapproved authority, or an expired validity window fail closed.

## Fixed control order and dependencies

The closed topological order is:

```text
signed_campaign_manifest
hosted_quality_gate
hosted_postgres_integration
hosted_supply_chain_subjects
published_artifact_attestation
candidate_clean_room_install
operated_target_cluster
operated_oidc_tenant_isolation
operated_secret_rotation
operated_datahub_iam
operated_siem_paging
independent_source_readonly
adversarial_browser_api
operated_m35_lifecycle
blind_bilingual_corpus
execution_ast_equivalence
browser_accessibility_matrix
scale_cost_soak_campaign
operated_recovery_drill
independent_pentest
operated_incident_drill
immutable_raw_result_bundle
release_risk_register
candidate_owner_signatures
```

The policy model freezes every direct prerequisite and rejects any missing node, extra node, reordering, cycle, or dependency that appears after its consumer. Authorization is staged so that later I/O can eventually be enabled only after the required earlier controls have been independently adjudicated. No capability is currently emitted.

## Remaining commercial blockers

Before receipts or any campaign capability can be connected, M30 still requires:

- an externally provisioned and authenticated trust bundle, with immutable GitHub numeric identities, workflow coordinates, signing keys, purpose, validity, and revocation;
- concrete cryptographic verification of producer and approver signatures plus trusted timestamps;
- canonical raw GitHub governance/run snapshots from which criteria are derived locally instead of trusting signed booleans;
- a durable compare-and-swap attempt ledger with exact predecessor chaining, replay protection, revocation checks, and trusted adjudication time;
- per-component `openat`/dirfd reads and same-dirfd atomic report writes with `O_NOFOLLOW`, owner/mode checks and race regressions, replacing the remaining checked-pathname P2;
- one reviewed deterministic adjudicator per remaining control, including frozen thresholds;
- real external workflows and receipts, independent owner approvals, operated target/provider/corpus evidence, and an exact candidate-head hosted campaign;
- M31 pilot, legal/service readiness, incident operations, and per-dialect certification beyond PostgreSQL.

Until all blockers are closed with authenticated external evidence, the only valid release result is `no_go`.
