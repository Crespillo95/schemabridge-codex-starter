# M29 milestone handoff

## Summary

- Milestone: M29 — Operations, infrastructure, supply-chain, and recovery hardening
- Status: complete locally; branch publication pending
- Recommended operator decision: accept the final-byte local M29 baseline; do not authorize
  production deployment or release publication
- Proposed commit message: `fix: close M29 production-readiness audit gaps`

```text
M29_FINAL_AUTOMATED_RESULT=PASS_LOCAL
M29_FINAL_POSTGRES_RESULT=PASS_SCHEMA_V12_EIGHT_CREDENTIALS_SOURCE_CONTROL_SEPARATED
M29_FINAL_RECOVERY_RESULT=PASS_LOCAL_FRESH_TARGET_EXTERNAL_CUTOVER_NOT_RUN
M29_FINAL_IMAGE_RESULT=PASS_LOCAL_PRECOMMIT_SHA256_DB2A42CE187243C809A9FCC1272246FB914FBEEFC9186B7A83B651014D015B79
M29_FINAL_BROWSER_RESULT=PASS_LOCAL_DEVELOPMENT_6X2_PRODUCTION_UNAVAILABLE_2X
M29_LOCAL_ACCEPTANCE_DECISION=ACCEPT
M29_PRODUCTION_GO=NO
M29_RELEASE_GO=NO
```

The current focused, schema/service, package, recovery, scale, browser, deterministic-evaluation,
wheel, coverage, and local-image evidence is recorded below. Current-byte coverage is 81.17% and
supersedes D115's 81.09% historical baseline. The local image was built from stable product bytes
before commit; its OCI revision label identifies prior HEAD
`9e8b69e1adce8e144b345d3b0d33482558804dc6`, so it is not an exact-commit or release artifact.

The repository contains production-shaped local contracts, not proof of an operated production
environment. Provider rotation/revocation, target-cluster admission and network enforcement,
active alert/SIEM delivery, inactive-signal producers, encrypted immutable remote retention,
fresh-target cutover/rollback, protected release attestation, exclusive GHCR write authority,
GitHub immutable Releases, production traffic/SLOs, M30/M31, and independent security/operator
approval remain explicit NO-GO items.

## Implemented

- Added one provider-neutral connector-secret port with strict owner-only local evidence and
  Vault/OpenBao-compatible HTTPS remote adapters for PostgreSQL, DataHub catalog, and immutable
  semantic-registry reads.
- Bound every remote read to one exact positive external provider version, one closed capability
  role, a short-lived audience-bound projected token, verified TLS, bounded replies, no redirects,
  no persistent secret copy, and sanitized stable failures.
- Preserved additive control history: v10 adds the aggregate-only observer; v11 adds four immutable
  provider-version pins without inventing legacy values; v12 adds the dedicated
  `schemabridge_backup` identity. The control boundary now has eight distinct credentials:
  runtime, API, worker, catalog, reconciler, migrator, observer, and backup.
- Added `schemabridge-backup`, a sanitized non-interactive entrypoint that uses only the read-only
  backup DSN and audit key. `pg_dump`/`pg_restore` receive an allowlisted process environment of
  `PATH` plus required `PG*` values and cannot inherit ambient OpenAI, OIDC, web, DataHub, audit, or
  connector-secret variables.
- Added an hourly, non-overlapping backup CronJob with bounded deadlines, the dedicated backup
  ServiceAccount, an exact versioned Secret reference, external PVC, and only the
  `control-backup` egress capability. Restore and cutover remain separate explicit operations.
- Closed active observability around actual producers: eight metric families, six alerts, three
  SLOs, and eight dashboard panels. The Kubernetes PrometheusRule mirrors the active alert group
  exactly. SIEM and uncomposed backup/release/integrity/capacity signals remain validated under
  `deploy/observability/inactive/` and cannot be represented as active monitoring or delivery.
- Added the fail-closed `schemabridge-web` process wrapper. It checks typed configuration,
  authentication, and exact schema before launching Streamlit; startup/readiness repeat preflight
  and require the exact bounded loopback 200/`ok` health response.
- Kept managed Streamlit planning-only. Execution and publication remain `disabled`; staging and
  production Operations reports unavailable without an operated source and never falls back to
  synthetic healthy/degraded scenarios. Synthetic six-state scenarios remain limited to explicit
  development/hosted-demo profiles.
- Closed the M29 Kubernetes base at 63 resources: seven long-running Deployments, the backup
  CronJob, the active PrometheusRule, nine ServiceAccounts, restricted containers,
  resources/topology/PDBs, TLS ingress, namespace default deny, capability-specific network paths,
  and six isolated metrics targets.
- Kept the production overlay deliberately non-deployable until an operator replaces every image,
  host, trust root, external Secret, provider role, registry scope, PVC/storage class, and cluster
  selector placeholder and passes target-cluster admission.
- Kept `uv.lock`, exact hashed runtime/build inputs, immutable workflow/image validation,
  direct-license policy, vulnerability evidence checks, SBOM/artifact binding, provenance
  contracts, protected-release OIDC, and the reproducible SHA-bound `watchdog` wheel.
- Replaced the unsafe PostgreSQL client filesystem copy with an exact Alpine APK closure. A
  separate stage fetches exactly `libpq`, `lz4-libs`, `postgresql-common`,
  `postgresql16-client`, and `zstd-libs` for amd64/arm64, verifies every reviewed SHA-256, and the
  final stage installs only those mounted files using `apk --no-network`. Static policy rejects
  changed versions/URLs/hashes, an extra APK, writable mounts, raw copying, and online final-stage
  resolution.
- Changed release publication into seven capability-separated jobs:
  `audit → prepare → candidate → scan → attest → promote → release`. The protected GET-only audit
  runs without checkout, repository code, or third-party actions; it either reverifies an exact
  historical immutable publication and skips every downstream job, or admits one fresh
  current-`main` build after branch/ruleset, CI, immutable-Release, monotonic publication, and
  reference-absence checks. Candidate resumption is digest exact; scan credentials are isolated
  and always cleaned; the remote manifest, attestations, Release body/metadata, and exactly ten
  public assets are reverified. Five approvals and all post-readback checks must finish within
  seven calendar days; 35-day retention is only a further 28-day incident-analysis buffer.
- Retained verified backup-pair retention/quarantine, distinct-target recovery contracts,
  forward-compatible image/verified-restore rollback, and the prohibition on automatic
  down-migration or cutover.

## Files changed

- `migrations/control_plane/0012_backup_identity.sql`,
  `demo/control_plane/init/001_roles.sql`: eighth read-only control identity and grants.
- `src/schemabridge/entrypoints/backup/`, `src/schemabridge/config.py`,
  `src/schemabridge/bootstrap.py`, `src/schemabridge/adapters/control_plane/postgres_operations.py`:
  scheduled backup composition and subprocess capability isolation.
- `src/schemabridge/entrypoints/streamlit/main.py`,
  `src/schemabridge/entrypoints/streamlit/m29_operations_page.py`,
  `src/schemabridge/adapters/web/`: fail-closed web launch/readiness and truthful Operations
  routing.
- `src/schemabridge/adapters/observability/`, `deploy/observability/`:
  producer registry, active bundle, and inactive contracts.
- `deploy/kubernetes/m29/base/backup-cronjob.yaml`,
  `deploy/kubernetes/m29/base/prometheus-rules.yaml`, the M29 base and validator:
  63-resource topology.
- `Dockerfile.runtime`, `scripts/verify_supply_chain.py`, supply-chain tests:
  exact five-APK multi-architecture fetch/hash/offline-install contract.
- `.github/workflows/release-evidence.yml`, release/supply-chain tests:
  prepublication release state machine.
- M29 architecture, security, test, deployment, runbook, plan, state, decision, task, and handoff
  documents.

The working tree contains historical milestone changes as well. This list identifies the final M29
audit-remediation surfaces; it does not attribute every dirty-tree path to M29.

## Commands executed

Final results must be inserted only after each command finishes on the final bytes. Counts from
overlapping commands must not be summed.

| Command | Result | Notes |
|---|---|---|
| Required repository docs, M29 plan/state, ADR, and local milestone skill | PASS | Read before implementation/document consolidation |
| Focused M29 unit/security/managed-composition tests | PASS | 316 passed in 17.74 s across backup, web, observability, Kubernetes, release-policy, and APK-policy regressions |
| Final release/supply-chain adversarial tests | PASS | 154 passed |
| `make supply-chain-static` | PASS_LOCAL | 894 candidate files and 23 direct licenses; only the expected dirty-tree warning |
| `make control-plane-reset && make control-plane-migrate && make control-plane-check` | PASS | Clean schema v12; all eight distinct credentials present; source/control separation verified |
| Focused real PostgreSQL backup/restore integration | PASS | 6 passed, 5 deselected in 5.96 s; includes backup write denial and distinct fresh-target restore behavior |
| `make test-integration` | PASS_WITH_EXTERNAL_SKIPS | 161 passed, 11 skipped, 3327 deselected in 250.42 s; skips require unavailable DataHub credentials plus the retained M27 small fixture |
| `make test-acceptance` | PASS_WITH_EXTERNAL_SKIPS | 43 passed, 4 skipped, 3452 deselected in 48.51 s; skips require unavailable DataHub credentials |
| `make evaluate` | PASS_DETERMINISTIC | 11 tables and 465 rows; `live_llm=not_run` |
| `make runtime-wheel-smoke` | PASS | Fresh installed wheel verified packaged migrations 1–12 and all current entrypoints |
| Operator-patched Kubernetes render and local validator | PASS_LOCAL_CONTRACT | Exact 63-resource manifest/render behavior is covered by the focused final-byte validation; target-cluster server-side validation remains external |
| `kubectl apply --server-side --dry-run=server` against target cluster | NOT_RUN_EXTERNAL | Required for production |
| Final local BuildKit image smoke, SBOM, and vulnerability policy | PASS_LOCAL_PRECOMMIT | Image `sha256:db2a42ce187243c809a9fcc1272246fb914fbeefc9186b7a83b651014d015b79`, 182236711 bytes; user 10001:10001; `pg_dump`/`pg_restore` 16.14; `pip check`, `ldd`, and all entrypoint smoke passed; exact 107-component SBOM; `pip-audit` found no known vulnerabilities and Trivy HIGH/CRITICAL policy passed |
| Image revision/release identity review | LOCAL_ONLY_OLD_HEAD_LABEL | Built from stable product bytes before commit, but OCI revision is prior HEAD `9e8b69e1adce8e144b345d3b0d33482558804dc6`; this is not an exact-commit or release image |
| `make m29-recovery-policy-check` | PASS_LOCAL | Fingerprint `24bdecb8bcb8faab8ba83d64eadf201c0b1d31142ebab773b012735f8f6f34ae` |
| `make check` | PASS | 3335 passed, 214 deselected in 1234.82 s; Ruff format/lint over 608 files and strict mypy over 308 files passed |
| `make coverage` | PASS_WITH_EXTERNAL_SKIPS | 3534 passed, 14 skipped, 1 deselected in 4149.89 s; 81.17%, above the required 80%; skips are unavailable optional DataHub credentials and the retained M27 small browser fixture |
| Scale correctness and PostgreSQL postflight | PASS | 25 passed in 3.16 s; PostgreSQL cut 8 passed, 4 deselected in 4.41 s; correctness policy passed |
| Internal-browser desktop/mobile final-byte matrix | PASS_LOCAL | Development 6/6 desktop and 6/6 mobile; production unavailable at 1280x720 and 390x844; final clean reload had zero console warnings/errors, overflow, XSS, scripts, protected-data hits, or dangerous actions; listener 8511 closed and temporary state absent |
| Final release-topology redesign/review | PASS_LOCAL | Seven jobs; workflow SHA-256 `9979c54be6ba39d1d7b606e6d882aa10e9868bb9d003482b30a780ee104d1f26`; Actionlint 1.7.12 and ShellCheck 0.11.0 passed (the sole Actionlint schema exclusion is GitHub's newer `concurrency.queue`); static/adversarial review found P0/P1/P2 = 0 |
| Worktree/staged/history secret and artifact scans | PASS_PRECOMMIT | Gitleaks 8.30.1 with full redaction; 888-file worktree/index snapshots and all 11 existing commits; zero leaks; ignored runtime/cache paths were not force-added |
| Exact-revision secret scan | PENDING_COMMIT_BOUND | Runs after the focused commit exists |
| `git diff --check` | PASS | Documentation postflight completed without whitespace errors |
| Focused commit, push, and draft-PR checks | NOT_RUN_COMMIT_BOUND | Read exact evidence from Git/PR after publication |

## Automated test results

- Focused tests: **PASS** — 316 component tests in 17.74 seconds; the final
  release/supply-chain cut passed 154 tests; focused backup/restore
  PostgreSQL passed 6 with 5 deselected in 5.96 seconds.
- `make check`: **PASS** — 3335 passed, 214 deselected in 1234.82 seconds; Ruff covered 608
  files and strict mypy covered 308 files.
- Integration tests: **PASS_WITH_EXTERNAL_SKIPS** — 161 passed, 11 skipped, 3327 deselected in
  250.42 seconds. Skips are exact unavailable DataHub credentials plus the retained M27 small
  fixture.
- Acceptance tests: **PASS_WITH_EXTERNAL_SKIPS** — 43 passed, 4 skipped, 3452 deselected in
  48.51 seconds; all skips require unavailable DataHub credentials.
- Deterministic evaluation: **PASS** over 11 tables/465 rows; `live_llm=not_run` and no
  live-provider result is inferred.
- Schema/role checks: **PASS** — clean schema v12, eight distinct credentials, and verified
  source/control separation.
- Runtime wheel: **PASS** for migrations 1–12 and all current installed entrypoints.
- Final local image: **PASS_LOCAL_PRECOMMIT** — exact digest
  `sha256:db2a42ce187243c809a9fcc1272246fb914fbeefc9186b7a83b651014d015b79`,
  182236711 bytes, user 10001:10001, PostgreSQL client 16.14, `pip check`, `ldd`, and all
  entrypoint smoke passed. It was built from stable pre-commit product bytes and carries prior
  HEAD `9e8b69e1adce8e144b345d3b0d33482558804dc6` in its OCI revision label; it is not release
  evidence.
- Supply-chain/SBOM/scans/provenance: **PASS_LOCAL** — 154 release/supply-chain tests; static audit over 894
  candidates/23 licenses with the expected dirty warning; exact 107-component image SBOM;
  `pip-audit` reports no known vulnerabilities and Trivy HIGH/CRITICAL policy passes. Protected
  registry provenance/attestation remains **NOT_RUN_EXTERNAL**.
- Recovery drill/policy: **PASS_LOCAL** with fresh-target behavior and exact policy fingerprint
  `24bdecb8bcb8faab8ba83d64eadf201c0b1d31142ebab773b012735f8f6f34ae`; external
  cutover/remote retention is **NOT_RUN_EXTERNAL**.
- Coverage: **PASS_WITH_EXTERNAL_SKIPS** — 3534 passed, 14 skipped, 1 deselected in 4149.89
  seconds; 81.17% exceeds the required 80%. Skips are unavailable optional DataHub credentials and
  the retained M27 small browser fixture.
- Scale postflight: **PASS** — 25 tests in 3.16 seconds plus the PostgreSQL cut with 8 passed,
  4 deselected in 4.41 seconds; correctness policy passed.
- Final browser: **PASS_LOCAL** — development 6/6 desktop plus 6/6 mobile; production unavailable
  at 1280x720 and 390x844; final clean-reload console and safety/cleanup checks passed.
- Release topology: **PASS_LOCAL** — workflow SHA-256
  `9979c54be6ba39d1d7b606e6d882aa10e9868bb9d003482b30a780ee104d1f26`; seven-job state machine;
  Actionlint/ShellCheck/static/adversarial gates pass with no local P0/P1/P2 finding.
- Secret/artifact hygiene: **PASS_PRECOMMIT** — Gitleaks 8.30.1 found zero leaks in the exact
  worktree/index snapshots or all 11 existing commits; ignored runtime/caches were not staged.
- Diff hygiene: **PASS**. Exact-revision secret scan, commit, push, and hosted checks are
  **PENDING/NOT_RUN**.

## Operator manual test

1. Start the final M29 Streamlit UI through the product entrypoint.
2. In the internal browser, inspect the explicit development/hosted-demo `Healthy`, `Degraded`,
   `Secret outage`, `Queue backlog`, `Stale backup`, and `Failed release` scenarios at desktop and
   mobile.
3. Start the staging/production presentation with an approved test composition and verify
   Operations renders only the unavailable state when no operated operations source exists.
4. Confirm neither managed state infers workload, queue, secret, backup, release, or telemetry
   health and no dangerous action is available.
5. Verify hostile script-shaped text remains escaped, the fresh console has no warning/error, and
   document width does not exceed viewport width.
6. Search rendered text, browser responses, and console output for tokens/JWTs, bindings/paths,
   DSNs/endpoints, username/password, SQL/parameters, prompts/provider payloads, source values/rows,
   raw plans, actor/tenant/table/field identity, stack traces, and backup paths.
7. Stop the process and remove only the exact temporary browser state/listener. Verify no listener,
   temporary database/role, backup artifact, token, log, or private evidence remains.

Expected result:

```text
development_desktop_states=6/6
development_mobile_states=6/6
managed_operations_state=unavailable
console_warnings_or_errors=0
horizontal_overflow=false
xss_flag=undefined
injected_scripts=0
protected_data_hits=0
dangerous_actions=0
cleanup_state=absent
cleanup_listener=closed
```

Observed result:

```text
development_desktop_states=6/6
development_mobile_states=6/6
production_desktop_1280x720=unavailable
production_mobile_390x844=unavailable
final_clean_reload_console_warnings_or_errors=0
horizontal_overflow=false
xss_flag=undefined
injected_scripts=0
protected_data_hits=0
dangerous_actions=0
cleanup_state=absent
cleanup_listener_8511=closed
```

Console errors observed during the controlled server change occurred before the clean validation
boundary. After the final server transition, the browser was reloaded and the complete final cycle
recorded zero console warnings or errors.

## Architecture and security review

- Dependency direction: backup/web/telemetry contracts remain in application/domain boundaries;
  PostgreSQL, process, Streamlit, HTTP health, SIEM, and Kubernetes details remain in adapters,
  entrypoints, or deployment files. Focused architecture regressions and the repository-wide
  `make check` quality gate pass.
- Source database writes: none are introduced. Backup/restore operate only on the control plane;
  source connectors retain transient read-only capability. Schema v12 grants backup reads, not
  migration/runtime/source writes.
- SQL/LLM validation: unchanged. The LLM can return only bounded typed interpretation;
  deterministic compilation and independent AST policy remain authoritative. Managed web has no
  execution capability.
- DataHub mutation approval: unchanged and still explicit/audited. Managed web receives no writer
  material, and no durable publisher worker exists.
- Secrets/proprietary data: repository examples remain synthetic. The backup subprocess environment
  is an explicit allowlist. Browser, worktree, staged-candidate, and existing-history scans pass;
  the exact-revision scan remains commit-bound.
- Fanout/semantic risks: M28 mapping, join, fanout, ambiguity, cost, one-connection,
  three-table/two-join, and source-read-only controls remain mandatory.

## Decisions made

- D109–D119 retain their historical meaning.
- D120: require a manual fail-closed prepublication release state machine and keep Release/GHCR
  writer exclusivity plus immutable Releases as external prerequisites. Its earlier mechanical
  topology is superseded by D123 after final local acceptance.
- D121: introduce schema v12 and a dedicated scheduled-backup identity/entrypoint; isolate
  PostgreSQL subprocess capability and install an exact five-APK client closure offline.
- D122: activate only producer-backed observability and require managed web readiness plus an
  explicit Operations-unavailable state without an operated source.
- D123: accept the final-byte local M29 baseline, supersede the earlier release mechanics with the
  seven-job replay-safe state machine, and retain production/release NO-GO.

All decisions are recorded in `tasks/DECISION_LOG.md`.

## Known limitations or unverified items

- No real Vault/OpenBao/cloud-provider IAM rotation, old-credential revocation, provider audit,
  outage, or bounded rollback has been operated.
- The production Kubernetes overlay contains intentional blockers. There is no target-cluster
  server-side admission, CRD, CNI, ingress, DNS, TLS, workload-identity, egress-broker, tenancy, or
  network-enforcement evidence.
- Only eight metric families and their dependent six alerts/three SLOs/eight panels are active.
  SIEM and remaining signals have no composed lifecycle-owned producer/exporter or operated
  destination.
- Streamlit does not submit execution jobs to the authenticated API, and publication has no typed
  durable queue or dedicated publisher worker. Those actions remain disabled in managed web.
- No encrypted immutable remote backup/object-lock retention, external fresh-target cutover,
  image rollback, or verified-restore rollback has been operated.
- No final registry wheel/image SBOM, vulnerability disposition, provenance attestation,
  short-lived OIDC signing, registry promotion, or clean exact-commit release evidence has been
  accepted. The exact 107-component SBOM and local vulnerability-policy pass recorded above bind
  only the pre-commit local image whose OCI label names the prior HEAD.
- Repository inspection found no `production-release` environment, unprotected `main`, zero
  rulesets, and Immutable Releases disabled. Exclusive GitHub Release/GHCR writer authority is
  also not operated or proven.
- Windows/PowerShell and target-provider/cluster/operator paths remain unverified unless a final
  command row explicitly records otherwise.
- M30 production evaluation/security verification and M31 pilot/GA have not started.

## Blockers

- Publication acceptance requires a focused commit, exact clean-revision scan, push, and successful
  hosted PR checks.
- Production acceptance is blocked on real provider, cluster, telemetry, remote retention,
  recovery/rollback, production traffic/SLO, vulnerability disposition, and independent
  security/operator evidence.
- Release acceptance is additionally blocked on protected branch/environment/reviewer policy,
  exclusive GHCR writer authority, immutable Releases, protected signing/attestation, exact
  artifact/deployment identity, and M30/M31 approval.

## Next milestone readiness

- Dependencies satisfied: **YES** for M29 local acceptance; **NO** for operated
  production and release.
- Recommended next prompt: finish final secret scans; then stage/commit/push the exact bytes and
  observe hosted checks.
- Required operator prerequisites: approved provider IAM and exact-version secrets; isolated
  workload/egress design; target Kubernetes admission context; TLS/PKI and identity configuration;
  metrics/alert/SIEM destinations and missing producers; encrypted immutable backup store and
  distinct restore target; protected release environment with independent reviewer; exclusive
  GHCR writer policy; GitHub immutable Releases; production evaluation data governance; and
  independent security/operator reviewers.
