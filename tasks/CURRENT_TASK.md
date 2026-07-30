# Current task

- Current milestone: M29 — Operations, infrastructure, supply-chain, and recovery hardening
- Status: final-byte local acceptance complete; initial branch commit published; hosted
  supply-chain correction awaiting exact follow-up commit and rerun
- Prompt: `prompts/M29_OPERATIONS_SUPPLY_CHAIN_HARDENING.md`
- Plan: `plans/M29_OPERATIONS_SUPPLY_CHAIN_HARDENING.md`
- ADR: `docs/adr/0014-operated-runtime-secrets-observability-and-supply-chain.md`
- Production/release GO: **NO**

## Objective

Finish and verify the exact M29 gates for remote exact-version secrets, projected workload
identity, TLS/default-deny networking, truthful producer-backed observability, frozen dependencies,
prepublication release evidence, dedicated scheduled backup, fresh-target recovery, and rollback.

M28 remains the accepted local query/routing baseline. Its source-read-only, typed-intent,
deterministic SQL compiler/AST guard, semantic approval, route, tenant, fanout, cost, and dynamic
catalog boundaries remain unchanged.

## Implemented locally

- One provider-neutral port supports strict owner-only local evidence and HTTPS
  Vault/OpenBao-compatible exact-version remote resolution. Managed components require remote mode
  and reject global/local credential fallback.
- Schema v10 adds the aggregate-only `schemabridge_observer`; schema v11 stores four immutable
  provider-version pins without inventing legacy values; schema v12 adds the dedicated
  `schemabridge_backup` identity. The current control boundary has eight distinct credentials:
  runtime, API, worker, catalog, reconciler, migrator, observer, and backup.
- `schemabridge-backup` creates one signed control-plane backup with the read-only backup
  credential. Its subprocess receives only allowlisted `PATH` and required `PG*` values, never the
  ambient OpenAI, audit, web, OIDC, DataHub, or connector-secret environment.
- The 63-resource Kubernetes base contains seven long-running Deployments, an hourly
  non-overlapping backup CronJob, the exact active PrometheusRule, nine ServiceAccounts, restricted
  containers, resource/topology controls, TLS ingress, default-deny networking, and isolated
  capability paths. The production overlay retains blocking operator placeholders by design.
- Exactly eight metric families with composed producers are active. The active bundle contains six
  alerts, three SLOs, and eight dashboard panels. SIEM and every remaining uncomposed signal,
  rule, SLO, panel, and runbook live under `deploy/observability/inactive/` as validated design
  contracts, not monitoring, paging, or delivery evidence.
- Managed `schemabridge-web` preflights typed configuration, authentication, and exact schema before
  launching Streamlit. Startup/readiness repeat preflight and require the fixed bounded loopback
  health response. Staging/production Operations reports unavailable without an operated data
  source and never falls back to synthetic status.
- Managed Streamlit remains planning-only: execution and publication are explicitly `disabled`.
  Execution still requires the authenticated API/job/worker lane, for which no Streamlit client
  exists; publication still lacks a durable approval queue and dedicated publisher worker.
- `uv.lock`, hashed runtime/build exports, immutable workflow/image validation,
  SBOM/vulnerability/provenance policy, and protected-release OIDC contracts are present. Every
  Trivy action uses ignored `.local/trivy-cache`.
- The runtime keeps the exact Python 3.13.14/Alpine 3.24 base and reproducible SHA-bound `watchdog`
  wheel. A separate fetch stage downloads exactly five versioned Alpine APKs for amd64/arm64 and
  verifies each reviewed SHA-256; the final stage installs them from a read-only BuildKit mount
  with `apk --no-network`. Raw filesystem copying, extra APKs, mutable coordinates, and online
  final-stage resolution fail static policy. Runtime evidence accepts the base image's virtual
  `.python-rundeps` through one exact platform map only: `20260616.002547/noarch` for `aarch64`
  and `20260616.002554/noarch` for `x86_64`.
- Release promotion is manually dispatched from an existing annotated SemVer tag through seven
  jobs: `audit → prepare → candidate → scan → attest → promote → release`. The protected GET-only
  audit runs without checkout, repository code, or third-party actions; it either verifies an
  exact historical immutable publication and skips every downstream job, or admits one fresh
  current-`main` build after branch/ruleset, CI, immutable-Release, monotonic publication, and
  reference-absence checks. Candidate resumption, scan, attestations, same-digest promotion, and
  the exact ten-asset Release are independently reverified. Five approvals and post-readback are
  bounded to seven days; the 35-day retention is only an incident-analysis buffer.
- Recovery policy, verified backup-pair retention/quarantine, fresh-target drill contracts, and
  forward-compatible image/verified-restore rollback rules remain present. Automatic
  down-migration and automatic cutover remain forbidden.

## Final-byte validation status

The final-byte focused, service, package, recovery, scale, browser, coverage, and local-image cuts
below are current evidence. Current-byte coverage is 81.17% and supersedes D115's 81.09%
historical baseline. D123 records local acceptance; D124 records the exact platform-specific
virtual-package evidence correction while retaining release and production NO-GO.
The local image was built from stable product bytes before commit and its OCI revision label still
binds the prior HEAD
`9e8b69e1adce8e144b345d3b0d33482558804dc6`; it is not an exact-commit or release artifact.

| Gate | Status |
|---|---|
| Focused unit/security/manifest/observability/backup/web tests | **PASS** — 316 passed in 17.74 s |
| Release/supply-chain focused and static gates | **PASS** — 158 passed; static audit 888 candidate files/23 licenses with only the expected dirty-tree warning |
| Clean schema v12 and eight-credential PostgreSQL boundary | **PASS** — clean reset/migrate/check; source/control separation verified |
| Focused backup/restore PostgreSQL cut | **PASS** — 6 passed, 5 deselected in 5.96 s |
| Full integration suite | **PASS_WITH_EXTERNAL_SKIPS** — 161 passed, 11 skipped, 3327 deselected in 250.42 s |
| Full acceptance suite | **PASS_WITH_EXTERNAL_SKIPS** — 43 passed, 4 skipped, 3452 deselected in 48.51 s |
| Deterministic evaluation | **PASS** — 11 tables/465 rows; `live_llm=not_run` |
| Installed-wheel smoke | **PASS** — migrations 1–12 and all current entrypoints |
| Operator-patched 63-resource local contract | **PASS_LOCAL** — covered by the focused manifest/render validation; target admission remains external |
| Final local BuildKit image/SBOM/vulnerability smoke | **PASS_LOCAL_PRECOMMIT** — image `sha256:db2a42ce187243c809a9fcc1272246fb914fbeefc9186b7a83b651014d015b79`, 182236711 bytes |
| Scale correctness/postflight | **PASS** — 25 passed in 3.16 s; PostgreSQL cut 8 passed, 4 deselected in 4.41 s |
| Recovery policy | **PASS_LOCAL** — fingerprint `24bdecb8bcb8faab8ba83d64eadf201c0b1d31142ebab773b012735f8f6f34ae` |
| Final release-topology redesign/review | **PASS_LOCAL** — 7 jobs; workflow SHA-256 `9979c54be6ba39d1d7b606e6d882aa10e9868bb9d003482b30a780ee104d1f26`; Actionlint 1.7.12, ShellCheck 0.11.0, static/adversarial review; P0/P1/P2 = 0 |
| `make check` | **PASS** — 3339 passed, 214 deselected in 1503.89 s; Ruff 608 files; strict mypy 308 files |
| Current-byte coverage | **PASS_WITH_EXTERNAL_SKIPS** — 3534 passed, 14 skipped, 1 deselected in 4149.89 s; 81.17%, above the required 80% |
| Documentation postflight `git diff --check` | **PASS** |
| Internal-browser desktop/mobile final-byte validation and cleanup | **PASS_LOCAL** — exact matrix retained in `tasks/M29_HANDOFF.md` |
| Initial published revision secret scan | **PASS_INITIAL_REVISION** — commit `09c3a2e0f47a7fbadb5297fa6bc4f9aca0d21950` plus all 12 then-existing commits; zero leaks |
| Corrective candidate/exact-revision secret scan | **PENDING_COMMIT_BOUND** |
| Commit, push, and hosted PR checks | **FOLLOW_UP_REQUIRED** — initial commit is on draft PR #1; run `30560980711` failed closed only in `supply-chain`; remaining jobs were then cancelled |

The local image runs as 10001:10001 and passes `pg_dump`/`pg_restore` 16.14, `pip check`, `ldd`,
all entrypoint smoke, an exact 107-component SBOM, a zero-known-vulnerability `pip-audit`, and the
Trivy HIGH/CRITICAL policy. Recovery policy fingerprint is
`24bdecb8bcb8faab8ba83d64eadf201c0b1d31142ebab773b012735f8f6f34ae`.
These are local pre-commit results, not hosted registry, provenance, release, or production
evidence.

## Publication boundary

Initial branch commit `09c3a2e0f47a7fbadb5297fa6bc4f9aca0d21950` is published on draft PR
#1. Hosted run `30560980711` built, scanned, and generated evidence, then failed closed because the
x86_64 base-image leaf exposes `.python-rundeps=20260616.002554/noarch` while the initial verifier
encoded the arm64 identity `20260616.002547/noarch`. The remaining `quality` and
`postgres-integration` jobs were cancelled after that true supply-chain failure; they are neither
passes nor code failures. D124's exact two-platform mapping is the corrective candidate.

The final exact commit and hosted status must be read from Git history and draft PR #1 after
publication because a commit cannot embed its own identity. The staged candidate and exact clean
revision must pass the secret/artifact/history and strict release scans; hosted PR checks are
independent reproducibility evidence, not production authorization.

The protected release workflow is also not sufficient by itself. The external repository
inspection found no `production-release` environment, unprotected `main`, zero rulesets, and
Immutable Releases disabled. Before any dispatch, an operator must create and prove the exact
environment, independent reviewers with self-review/bypass disabled, eligible tag restrictions,
the environment-only sentinel/audit credential, exclusive GitHub Release and GHCR write authority
for this protected workflow, protected `main`, and Immutable Releases. YAML cannot substitute for
those controls.

## Explicit NO-GO boundary

No external provider rotation/revocation, target-cluster admission/network enforcement, production
alert/SIEM delivery, inactive-signal producer operation, encrypted immutable remote retention,
external fresh-target cutover/rollback, exclusive registry-writer enforcement, protected release
attestation, production traffic/SLO, M30/M31, or independent security/operator approval has been
accepted. Static manifests, local metrics, unit tests, and unsigned local artifacts cannot
substitute for those operations.
