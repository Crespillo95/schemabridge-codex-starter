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
  SBOM/vulnerability/provenance policy, and protected-release OIDC contracts are present. Every
  Trivy action writes its cache under ignored `.local/trivy-cache`; static policy rejects any
  missing or different path as `trivy_cache_path_invalid`.
- Python auditing uses the frozen export directly with no resolver environment. The exact
  Python 3.13.14/Alpine 3.24 runtime builds a reproducible, SHA-bound `watchdog` wheel from its
  verified sdist, installs the complete wheelhouse offline through read-only BuildKit mounts, and
  retains neither build tooling nor the wheelhouse in the final image.
- Recovery policy, verified backup-pair retention/quarantine, fresh-target drill contracts, and
  forward-compatible image/restore rollback rules are present.
- Managed Streamlit is planning-only: execution and publication are explicitly `disabled`.
  Execution still requires the authenticated API/job/worker lane, for which no Streamlit client
  exists; publication still lacks a durable approval queue and dedicated publisher worker.
- A separate operator component keeps migration/backup/restore commands outside the web runtime
  and does not inherit developer `.env` capability.

## Local acceptance evidence

1. Focused M29 recovery/deployment/telemetry/logging/composition tests pass 210/210; the final
   supply/release/wheel cut passes 76/76.
2. Clean schema v11 and all seven control credentials pass; focused observer/provider-version
   PostgreSQL passes 18/18 and full integration passes 158 tests with 11 explicit external skips.
3. Acceptance passes 43 tests with four explicit DataHub skips; deterministic evaluation,
   installed wheel migrations 1–11/all ten entrypoints, frozen uv install, `pip check`, recovery,
   and scale contracts pass.
4. The operator-patched 61-resource manifest passes locally and the unpatched template fails
   closed. Target-cluster server-side validation remains `NOT_RUN_EXTERNAL`.
5. The Codex internal browser passes all six operations states at 1280×720 and 390×844 with clean
   console, escaped hostile text, no overflow or protected-data hits, and closed cleanup.
6. Final `make check` passes supply-chain/release audit, Ruff over 592 files, and mypy over 300
   source files before the local executor stops the still-passing pytest process at its 600-second
   limit. The exact 3154-test selection passes exhaustively in disjoint shards
   (3079 + 39 + 22 + 14), with 211 service tests deselected and no assertion failure. The retained
   full-coverage baseline over unchanged `src/schemabridge` passes 3325 tests with 14 explicit
   external skips and one performance deselection at 81.09%; the 2436-second command was not
   repeated after the final supply-chain-only patch.
7. The final checkout audit closes the hidden-artifact upload and Trivy-cache regressions with a
   seven-path allowlist, timeout/condition/retention validation, and an ignored exact cache path;
   59 supply-chain and 13 release-audit tests pass, including forced interrupted-coverage
   rejection, Docker-context key exclusion, complete build-input auditing, and exact final-stage
   command enforcement.
8. The final BuildKit image smoke passes as UID/GID 10001 with `pip check`, API/UI imports,
   `watchdog==6.0.0`, no `setuptools`, and no retained wheelhouse. Its current Trivy image scan
   reports zero HIGH/CRITICAL findings; Docker reports a local image size of 180,197,459 bytes,
   down from 310,636,145.

## Publication boundary

The exact branch commit and hosted status are intentionally external to this precommit task record
because a commit cannot embed its own identity. Publication evidence must be read from Git history
and draft PR #1. The candidate must pass the staged secret/artifact/history scan and strict
clean-revision audit before it can be treated as release input. Hosted PR checks remain independent
reproducibility evidence, not production authorization.

Hosted run `30493061776` failed safely after its image scans because Trivy created non-ignored
`.cache/trivy` before the exact-tree provenance guard. The reviewed correction confines all four
CI/release scanner caches to ignored `.local/trivy-cache` and adds fail-closed regressions; the
replacement run `30495413406` then generated provenance and all seven artifacts before failing
closed on two independent checks: default `pip-audit` omitted `packaging`, and the old Debian
runtime carried high/critical findings. The local correction uses `--disable-pip` and the reviewed
reproducible Alpine wheelhouse. Its replacement hosted result is deliberately not presumed inside
this commit. Run `30520807060` then passed the rebuilt image, vulnerability policy, and evidence
generation before exposing one compatibility boundary: pinned Trivy emitted CycloneDX 1.6 while
the verifier accepted only 1.5. The verifier now accepts the explicit reviewed 1.5/1.6 set without
weakening component, digest, source, lock, or provenance checks; its replacement result remains
external to this precommit record.

## Explicit NO-GO boundary

No external provider rotation/revocation, target-cluster admission/network enforcement,
production alert/SIEM delivery, immutable remote retention, operated external fresh-target
cutover/rollback, protected release attestation, production traffic/SLO, M30/M31, or independent
security/operator approval has been accepted. Static manifests, local metrics, unit tests, and
unsigned local artifacts cannot substitute for those operations.
