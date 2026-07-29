# Current task

- Current milestone: M29 — Operations, infrastructure, supply-chain, and recovery hardening
- Status: reproducible local baseline accepted; publication tracked in draft PR #1
- Prompt: `prompts/M29_OPERATIONS_SUPPLY_CHAIN_HARDENING.md`
- Plan: `plans/M29_OPERATIONS_SUPPLY_CHAIN_HARDENING.md`
- ADR: `docs/adr/0014-operated-runtime-secrets-observability-and-supply-chain.md`
- Production/release GO: **NO**

## Objective

Implement and operate the exact M29 gates for remote exact-version secrets, projected workload
identity, TLS/default-deny networking, structured redacted telemetry, bounded metrics/SLOs/alerts/
SIEM, frozen dependencies, pinned CI, SBOM/vulnerability/provenance evidence, signed backup
retention, fresh-target recovery, and rollback.

M28 remains the accepted local query/routing baseline. Its source-read-only, typed-intent,
deterministic SQL compiler/AST guard, semantic approval, route, tenant, fanout, cost, and dynamic
catalog boundaries must remain unchanged.

## Implemented locally

- One provider-neutral port now supports strict owner-only local evidence and HTTPS
  Vault/OpenBao-compatible exact-version remote resolution. Managed components require remote
  mode and reject global/local credential fallbacks.
- Provider secret versions are independent from public route revisions. Schema v11 stores four
  immutable version pins and deliberately leaves historical unversioned routes non-executable
  until a newly approved rotation.
- Schema v10 adds the read-only `schemabridge_observer` role and an aggregate-only queue view.
  API, execution, catalog, profile, reconciler, and observer processes have bounded metrics;
  structured logs, SLOs, alerts, dashboards, SIEM, and safe operations projections use closed
  schemas.
- The M29 Kubernetes base defines separate workload identities, projected tokens only where
  required, restricted containers, resource/topology controls, TLS ingress, default-deny
  networking, and isolated scrape paths. The production overlay retains blocking operator
  placeholders by design.
- `uv.lock`, hashed runtime/build exports, immutable workflow/image validation,
  SBOM/vulnerability/provenance policy, and protected-release OIDC contracts are present.
- Recovery policy, verified backup-pair retention/quarantine, fresh-target drill contracts, and
  forward-compatible image/restore rollback rules are present.
- Managed Streamlit is planning-only: execution and publication are explicitly `disabled`.
  Execution still requires the authenticated API/job/worker lane, for which no Streamlit client
  exists; publication still lacks a durable approval queue and dedicated publisher worker.
- A separate operator component keeps migration/backup/restore commands outside the web runtime
  and does not inherit developer `.env` capability.

## Local acceptance evidence

1. Focused M29 recovery/deployment/telemetry/logging/composition tests pass 210/210; the final
   supply/release/wheel cut passes 58/58.
2. Clean schema v11 and all seven control credentials pass; focused observer/provider-version
   PostgreSQL passes 18/18 and full integration passes 158 tests with 11 explicit external skips.
3. Acceptance passes 43 tests with four explicit DataHub skips; deterministic evaluation,
   installed wheel migrations 1–11/all ten entrypoints, frozen uv install, `pip check`, recovery,
   and scale contracts pass.
4. The operator-patched 61-resource manifest passes locally and the unpatched template fails
   closed. Target-cluster server-side validation remains `NOT_RUN_EXTERNAL`.
5. The Codex internal browser passes all six operations states at 1280×720 and 390×844 with clean
   console, escaped hostile text, no overflow or protected-data hits, and closed cleanup.
6. Final `make check` passes supply-chain/release audit, Ruff over 592 files, mypy over 300 source
   files, and 3136 tests with 211 deselected. Full coverage passes 3325 tests with 14 explicit
   external skips and one performance deselection at 81.09% against the 80% floor.
7. The final checkout audit closes the hidden-artifact upload regression with a seven-path
   allowlist and timeout/condition/retention validation; 41 supply-chain and 13 release-audit
   tests pass, including forced interrupted-coverage rejection.

## Publication boundary

The exact branch commit and hosted status are intentionally external to this precommit task record
because a commit cannot embed its own identity. Publication evidence must be read from Git history
and draft PR #1. The candidate must pass the staged secret/artifact/history scan and strict
clean-revision audit before it can be treated as release input. Hosted PR checks remain independent
reproducibility evidence, not production authorization.

## Explicit NO-GO boundary

No external provider rotation/revocation, target-cluster admission/network enforcement,
production alert/SIEM delivery, immutable remote retention, operated external fresh-target
cutover/rollback, protected release attestation, production traffic/SLO, M30/M31, or independent
security/operator approval has been accepted. Static manifests, local metrics, unit tests, and
unsigned local artifacts cannot substitute for those operations.
