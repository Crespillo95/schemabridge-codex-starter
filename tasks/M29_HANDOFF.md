# M29 milestone handoff

## Summary

- Milestone: M29 — Operations, infrastructure, supply-chain, and recovery hardening
- Status: local implementation and browser acceptance passed; external production/release
  operation remains blocked
- Recommended operator decision: accept the reproducible local M29 baseline only; do not authorize
  production deployment or release publication
- Proposed commit message: `fix: harden reproducible runtime supply chain`

```text
M29_FINAL_AUTOMATED_RESULT=PASS_CHECK_COMPONENTS_AND_SHARDED_3154_COVERAGE_BASELINE_81_09_NOT_RERUN
M29_FINAL_POSTGRES_RESULT=PASS_158_TESTS_11_EXTERNAL_SKIPS
M29_FINAL_RECOVERY_RESULT=PASS_LOCAL_FRESH_TARGET_EXTERNAL_CUTOVER_NOT_RUN
M29_FINAL_BROWSER_RESULT=PASS_DESKTOP_6_OF_6_MOBILE_6_OF_6
M29_LOCAL_ACCEPTANCE_DECISION=PASS_LOCAL_BASELINE_ONLY
M29_PRODUCTION_GO=NO
M29_RELEASE_GO=NO
```

The repository contains production-shaped local contracts, not proof of an operated production
environment. Provider rotation/revocation, target-cluster admission and network enforcement,
alert/SIEM delivery, immutable remote retention, fresh-target recovery/rollback, protected release
attestation, production traffic/SLOs, M30/M31, and independent security/operator approval remain
explicit NO-GO items.

## Implemented

- Added one provider-neutral connector-secret port with strict owner-only local evidence and
  Vault/OpenBao-compatible HTTPS remote adapters for PostgreSQL, DataHub catalog, and immutable
  semantic-registry reads.
- Bound every remote read to one exact positive external provider version, one closed capability
  role, a short-lived audience-bound projected token, verified TLS, bounded replies, no redirects,
  no persistent secret copy, and sanitized stable failures.
- Added additive control schema v10 for the read-only `schemabridge_observer` and its
  security-barrier aggregate queue view. Added schema v11 for four immutable provider-version pins
  and exact version-bearing connector route loaders.
- Preserved historical migration evidence: v11 does not infer provider versions from public
  `route_revision` and does not backfill v9/v10 routes. An unversioned route stays non-executable
  until a newly approved rotation.
- Added schema-versioned structured operational events, bounded process metrics exporters, an
  aggregate-only observer, closed OpenMetrics names/labels, SLOs, alert rules, dashboards, SIEM
  batching/loss contracts, and linked incident runbooks.
- Added a safe six-state operations projection and Streamlit renderer for healthy, degraded,
  secret-outage, queue-backlog, stale-backup, and failed-release states.
- Added an M29 Kubernetes reference base with distinct service accounts, explicit projected
  identities only for secret readers, disabled automatic token mounts, restricted non-root
  workloads, resources/topology/PDBs, TLS ingress, namespace default deny, capability-specific
  network paths, and six isolated metrics scrape services.
- Kept the production overlay deliberately non-deployable until an operator replaces every image,
  host, trust-root, external-secret, provider-role, and cluster-selector placeholder and passes
  target-cluster admission.
- Added `uv.lock`, exact hashed runtime/build requirement exports, immutable workflow/image
  validation, direct-license policy, vulnerability evidence checks, wheel/image SBOM binding,
  provenance contracts, and protected-release short-lived OIDC.
- Made Python auditing resolver-independent and moved the runtime to the exact Python
  3.13.14/Alpine 3.24 digest. The only sdist-only dependency (`watchdog`) is built with pinned
  vulnerability-fixed tooling isolated from DataHub's application constraint and the upstream
  timestamp into a reproducible amd64/arm64-identical wheel, bound to an exact local SHA-256 and
  installed offline from read-only BuildKit mounts. CI audits all runtime and build inputs.
- Added an explicit recovery floor, verified signed backup-pair retention planning, exact
  review-bound recoverable quarantine, fresh-target recovery evidence contracts, and safe
  forward-compatible image/verified-restore rollback decisions. Automatic down-migration remains
  forbidden.
- Added a separate `operator` component for migration/check/backup/restore commands. It does not
  load developer `.env` state and rejects unrelated web, OIDC, OpenAI, DataHub, API-auth, or
  connector-secret capabilities.
- Made managed Streamlit planning-only. Execution and publication must be `disabled`; the web
  runtime can load governed context, match fields, compile, independently guard, and preflight,
  but cannot open an execution connection or load DataHub writer material.

## Files changed

- `src/schemabridge/application/ports/connector_secrets.py`,
  `operational_snapshot.py`, `operational_telemetry.py`: provider-neutral secret and operational
  ports.
- `src/schemabridge/adapters/connectors/remote_secrets.py`,
  `adapters/catalog/remote_datahub_secrets.py`,
  `adapters/semantic_registry/remote_secrets.py`: exact-version HTTPS credential resolution.
- `migrations/control_plane/0010_operational_observer.sql`,
  `0011_connector_secret_versions.sql`, and
  `demo/control_plane/init/001_roles.sql`: observer least privilege and immutable provider-version
  storage/loaders.
- `src/schemabridge/adapters/observability/`,
  `application/operational_snapshot.py`, `entrypoints/observer/`, and long-running entrypoints:
  structured events, metrics, snapshot, SIEM, observer, and process wiring.
- `src/schemabridge/application/m29_operations.py` and
  `entrypoints/streamlit/m29_operations*.py`: safe six-state operations view.
- `deploy/observability/`: metric bundle, SLOs, alerts, dashboard, SIEM contract, and exact
  runbooks.
- `deploy/kubernetes/m29/`: production-shaped reference topology and fail-closed renderer
  validation.
- `requirements/`, `uv.lock`, `scripts/verify_supply_chain.py`,
  `.github/workflows/ci.yml`, `.github/workflows/release-evidence.yml`,
  `Dockerfile.runtime`, and `scripts/release_clean_room.sh`: frozen inputs and artifact policy.
- `src/schemabridge/adapters/backup/retention.py`,
  `application/recovery.py`, `scripts/m29_recovery.py`, and `deploy/recovery/`: retention,
  recovery, and rollback contracts.
- `src/schemabridge/config.py`, `bootstrap.py`, `entrypoints/cli/main.py`, `Makefile`, bootstrap
  scripts, and runtime manifests: component boundaries, operator environment, managed web
  refusal, and operational composition.
- M29 unit, integration, acceptance, migration, runtime, manifest, observability, supply-chain,
  retention, recovery, managed-composition, and browser-panel tests.
- M29 plan/prompt/ADR, architecture, security, test strategy, runbook, deployment, browser,
  project state, decision log, current task, and this handoff.

The working tree contains historical milestone changes as well. This list identifies M29
surfaces; it does not attribute every dirty-tree path to M29.

## Commands executed

The rows below distinguish reproducible local evidence from unavailable or deliberately blocked
external operation. A local pass is never a production or release claim.

| Command | Result | Notes |
|---|---|---|
| Required repository docs, M29 plan/state, ADR, and local milestone skill | pass | Read before documentation consolidation |
| Focused M29 unit/security/managed-composition selection | PASS | 210 M29 recovery/deployment/telemetry/logging/composition tests passed in 12.83 s; the final supply/release/wheel cut passed 76 tests |
| Final supply-chain and release-audit regressions | PASS | 59 supply-chain tests and 13 release-audit tests pass; CI/release uploads use one immutable action and a seven-path allowlist, forced `.coverage.*` artifacts fail closed, and Trivy cache, complete runtime/build audit resolution, Docker-context key exclusion, immutable Alpine base, isolated non-vulnerable build backend, reproducible wheel/hash, exact final-stage commands, offline BuildKit install, and no-retained-wheelhouse contracts are enforced |
| `make supply-chain-static` | PASS_LOCAL | Immutable-input policy passed; the final precommit release audit inspected 865 candidate files and 23 direct licenses with only the expected dirty-tree warning |
| `make m29-recovery-policy-check` | PASS | Schema `schemabridge.recovery-operator.v1`; policy fingerprint `24bdecb8bcb8faab8ba83d64eadf201c0b1d31142ebab773b012735f8f6f34ae` |
| `make control-plane-reset` / `make control-plane-migrate` / `make control-plane-check` | PASS | Clean schema v11; seven credentials current with no pending migration; source/control separation verified |
| Focused real PostgreSQL observer/provider-version integration | PASS | 18 passed in 8.20 s after a clean v11 migration |
| `make test-integration` | PASS_WITH_EXTERNAL_SKIPS | 158 passed, 11 skipped, 3171 deselected in 172.76 s; skips are exact missing DataHub credentials and one unavailable retained M27 browser fixture |
| `make test-acceptance` | PASS_WITH_EXTERNAL_SKIPS | 43 passed, 4 skipped in 54.73 s; skips require unavailable DataHub reader/writer credentials |
| `make evaluate` | PASS_DETERMINISTIC | Recorded fixtures, deterministic fake, and read-only PostgreSQL passed; live LLM attestation was not run |
| `make runtime-wheel-smoke` | PASS | Fresh installed wheel verified migrations 1–11, exact names, all 10 console entrypoints, and eight safe `--help` paths |
| Frozen clean install from `uv.lock` and hashed exports | PASS | uv 0.11.30 lock check resolved 173 packages, frozen sync checked 162 packages, `pip check`, doctor, and isolated runtime imports passed |
| Wheel/image SBOM, dependency/image scan, license, and provenance validation | PASS_LOCAL_PARTIAL_EXTERNAL | `pip-audit --disable-pip` covered all 74 applicable frozen runtime/build dependencies, including `packaging` and isolated `setuptools==83.0.0`, with zero known vulnerabilities. The final 180,197,459-byte BuildKit image passed non-root smoke, `pip check`, API/UI imports, no-build-tool/no-wheelhouse checks, and a current Trivy scan with zero HIGH/CRITICAL findings. Final registry SBOM/provenance publication remains protected hosted-release evidence |
| M29 retention planner and complete distinct fresh-target recovery/rollback drill | PASS_LOCAL_EXTERNAL_CUTOVER_NOT_RUN | Signed backup restored into a distinct fresh local database with schema v11/state verification; retention/tamper/rollback contracts passed; remote object-lock, external cutover, and operated rollback were not run |
| Operator-patched Kubernetes render and local validator | PASS_LOCAL | Closed 61-resource render passed; the unpatched overlay failed closed with `unresolved_placeholder` |
| `kubectl apply --server-side --dry-run=server` against target cluster | NOT_RUN_EXTERNAL | Required for production; missing cluster context remains an explicit blocker |
| `make check` | EXECUTOR_LIMIT_WITH_COMPONENTS_PASSING | Supply-chain and release audit, Ruff over 592 files, and mypy over 300 source files passed; the local executor sent SIGTERM at its 600-second limit while pytest was still passing at 68%. The current exact selection then passed in exhaustive disjoint shards: 3079 + 39 + 22 + 14 = 3154 tests, with 211 service tests deselected and no assertion failure |
| `make coverage` | RETAINED_BASELINE_NOT_RERUN | The complete baseline passed 3325 tests, with 14 skips and 1 deselection, at 81.09% in 2436.86 s. `src/schemabridge` is unchanged by the final supply-chain-only patch, but this expensive command was not rerun; thirteen skips require unavailable DataHub credentials and one requires the retained M27 browser corpus |
| `make test-scale-correctness` and retained PostgreSQL scale postflight | PASS | 33 passed; 10 and 5,434 asset profiles retained bounded 1/17/50 paging, while final unit/integration contracts cover 10/75 and 5,434/41,028 |
| Release audit / clean exact-revision checks | PRECOMMIT_PASS | Normal audit passed with only dirty-tree warning; strict clean-revision audit is required immediately after commit |
| Codex internal-browser desktop/mobile operations matrix | PASS | 6/6 desktop and 6/6 mobile states; zero console warnings/errors, overflow, injected scripts, protected-data hits, or dangerous operations actions |
| Final secret/artifact scan and `git diff --check` | STAGED_PASS | Release audit and diff hygiene pass. Gitleaks v8.30.1 reports zero findings across the staged diff, all nine reachable commits, and the isolated staged tree. Strict clean-commit audit remains required immediately after commit |
| Focused commit, push, and existing draft-PR checks | NOT_RUN_COMMIT_BOUND | A commit cannot embed its own identity. Exact push/check evidence is external to this precommit handoff and must be read from Git and draft PR #1 after publication |

### Corrections retained from development

- Schema v11 separates external provider versions from public route revisions and refuses
  historical unversioned routes instead of inventing a backfill.
- Managed web no longer composes live/fake synchronous execution or publication in staging and
  production; both must be explicitly disabled.
- Privileged control commands use the operator component and a cleaned environment rather than
  inheriting web or developer `.env` capability.
- Final corrections retained exact v11 upgrade expectations, the observer's negative ACLs, the
  v2 provider-version loader, structured lifecycle events, complete wheel entrypoints, hardened
  CI/Docker secret context, and explicit disabled managed-web mutation modes. Their focused
  PostgreSQL/unit cuts passed 28, 26, 21, 12, and 58 tests respectively before the final gates.
- The prepublication workflow audit found that `upload-artifact` excludes everything below a
  hidden directory unless explicitly enabled. Both workflows now opt in only for an exact
  seven-path evidence allowlist; the static gate enforces one reviewed upload, its timeout,
  condition, retention, and failure behavior. Interrupted `.coverage.*` fragments are ignored by
  Git/Docker and still rejected if forced commit-visible.
- Independent Gitleaks review classified the historical matches as synthetic fixtures or
  non-secret identifiers. The immutable history exceptions are bound individually to exact
  commit/path/rule/line fingerprints, current fixtures carry explicit line-local markers, and no
  path or detector rule is allowlisted.
- The first hosted M29 supply-chain run reached the image scans but failed closed before provenance
  because Trivy's implicit `.cache/trivy` made the exact checkout dirty. CI and release now
  confine every Trivy cache to ignored `.local/trivy-cache`; static policy and regressions reject
  omission or deviation as `trivy_cache_path_invalid`. The corrected hosted result remains
  external to this commit and must be read from draft PR #1.
- Hosted run `30495413406` proved that correction by generating provenance and uploading all seven
  evidence paths, then failed closed because default `pip-audit` omitted `packaging` and the old
  Debian runtime exposed high/critical findings. CI/release now use `--disable-pip`; final review
  also replaced vulnerable build-only `setuptools==81.0.0` with isolated, hash-bound 83.0.0. The
  reviewed Alpine wheelhouse closes all findings without vulnerability exceptions. The next
  hosted result remains external to this commit.

## Automated test results

- Focused tests: PASS — 210 M29 tests plus the 76-test supply/release/wheel cut.
- `make check`: executor-limited after supply-chain/release audit, Ruff over 592 files, and mypy over
  300 source files passed; pytest remained green at 68% when the 600-second limit sent SIGTERM.
  The exact selection passed exhaustively in disjoint shards: 3079 + 39 + 22 + 14 = 3154,
  with 211 service tests deselected and no assertion failure.
- Integration tests: PASS — 158 passed, 11 explicit external skips.
- Acceptance tests: PASS — 43 passed, 4 explicit DataHub skips.
- Deterministic evaluation: PASS; live LLM attestation not run.
- Schema/role checks: PASS — schema v11, seven distinct credentials, no pending migration.
- Runtime wheel/image: wheel and final local image PASS; BuildKit smoke, exact wheel hash, no
  retained build inputs, and zero-HIGH/CRITICAL Trivy scan passed. Hosted image evidence is pending.
- Supply-chain/SBOM/scans/provenance: local contracts, complete dependency audit, and image scan
  PASS; protected registry SBOM/provenance release evidence not run.
- Recovery drill: local distinct-target PASS; external cutover/remote retention not run.
- Coverage: retained baseline over unchanged measured product source — 3325 passed, 14 explicit
  external skips, one performance deselection, and 81.09% against the 80% floor; not rerun after
  the final supply-chain-only patch.
- Scale postflight: PASS for local correctness and retained exact cardinality contracts.
- Final diff/secret scan: staged diff/tree and complete reachable history PASS; strict clean-commit
  audit remains the publication boundary.

## Operator manual test

1. Start the final M29 Streamlit operations scenario using the same product renderer that will be
   committed.
2. In Codex's internal browser, inspect `Healthy`, `Degraded`, `Secret outage`,
   `Queue backlog`, `Stale backup`, and `Failed release` at desktop 1280x720.
3. Repeat all six states at mobile 390x844.
4. Verify every state has its safe status, bounded diagnostics, and recommended response; blocked
   states expose no source, secret, release, restore, or dangerous action.
5. Verify the hostile script-shaped caption remains escaped literal text, no script executes, the
   final fresh console has no warning/error, and document width never exceeds viewport width.
6. Search rendered text, browser responses, and console output for token/JWT, binding/path, DSN,
   endpoint, username/password, SQL/parameters, prompt/provider payload, source values/rows, raw
   plan, actor/tenant/table/field identity, stack trace, and backup path disclosure.
7. Stop the process and remove only the exact temporary browser state/listener. Verify no listener,
   temporary database/role, backup artifact, token, log, or private evidence remains.

Expected result:

```text
desktop_states=6/6
mobile_states=6/6
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
desktop_states=6/6
mobile_states=6/6
console_warnings_or_errors=0
horizontal_overflow=false
xss_flag=undefined
injected_scripts=0
protected_data_hits=0
dangerous_actions=0
cleanup_state=absent
cleanup_listener=closed
```

## Architecture and security review

- Dependency direction: the secret and telemetry contracts live in application ports; filesystem,
  HTTPS, PostgreSQL, logging, SIEM, Streamlit, and Kubernetes details remain in adapters,
  entrypoints, or deployment files. `bootstrap.py` remains the only composition root. The final
  whole-tree architecture scan and mypy gate over 300 source files pass.
- Source database writes: none are introduced. Connector operations remain transient read-only
  capabilities; recovery operates only the control plane and requires a distinct empty target.
  Local schema-v11, seven-role, source/control-separation, negative-ACL, and write-denial evidence
  passes. Target-provider and target-cluster enforcement remains external and unoperated.
- SQL/LLM validation: unchanged. The LLM can return only bounded typed interpretation;
  deterministic compilation and independent AST policy remain authoritative. Managed web has no
  execution capability; authenticated API/job/worker execution still revalidates governed
  evidence before read-only I/O.
- DataHub mutation approval: unchanged and still explicit/audited. Managed web receives no writer
  material, and no durable publisher worker exists yet.
- Secrets/proprietary data: repository examples and tests are synthetic. Secret/token/DSN/path
  values have no public model or telemetry field. Whole-tree/history/artifact and browser scans
  pass precommit; the staged candidate and exact clean revision are rescanned at publication.
- Fanout/semantic risks: M28 mapping, join, fanout, ambiguity, cost, one-connection,
  three-table/two-join, and source-read-only controls remain mandatory. The final
  integration/acceptance/evaluation/check/coverage matrix passes with only the recorded external
  skips.

## Decisions made

- D109: begin M29 with remote exact-version secrets, distinct workload identity/default-deny
  networking, governed telemetry, exact supply-chain evidence, and verified recovery.
- D110: separate external provider versions from route revisions; schema v11 never invents a
  legacy version.
- D111: keep managed web planning-only and explicitly disable synchronous execution/publication.
- D112: isolate privileged control commands under a clean, non-web operator component.
- D113: keep M29 implementation distinct from operated production/release evidence.
- D114: make candidate secret/artifact/architecture scanning part of every static gate and require
  the protected-environment sentinel before any release action.
- D115: accept the reproducible local M29 baseline while retaining production and release NO-GO.
- D116: baseline only individually reviewed historical secret-scan fingerprints and require
  line-local synthetic markers for current false positives.
- D117: constrain every Trivy cache to ignored `.local/trivy-cache` and reject any workflow
  deviation before provenance.
- D118: audit every exact runtime/build input and build the exact Alpine runtime from a
  reproducible, SHA-bound, offline-mounted wheelhouse.

All decisions are recorded in `tasks/DECISION_LOG.md`; the lasting architecture is recorded in
ADR 0014.

## Known limitations or unverified items

- No real Vault/OpenBao/cloud-provider IAM policy, exact-version rotation, old-credential
  revocation, provider audit, outage, or bounded rollback has been operated.
- The production Kubernetes overlay contains intentional blockers. There is no target-cluster
  server-side admission, CRD, CNI, ingress, DNS, TLS, workload-identity, egress-broker, tenancy, or
  network-enforcement evidence.
- No production alert route, page delivery/resolution, SIEM destination/mTLS, dashboard backend,
  telemetry-loss drill, or production SLO/error-budget evidence exists.
- The Streamlit CLI can emit bootstrap text before `app.py` runs and installs the structured
  handler. Application and rerun logs are closed JSON, but complete process-startup capture still
  requires a reviewed wrapper or operated collector evidence.
- Streamlit does not submit execution jobs to the authenticated API, and publication has no typed
  durable queue or dedicated publisher worker. Those actions remain disabled in managed web.
- No encrypted immutable remote backup/object-lock retention, external fresh-target
  cutover/restore, image rollback, or verified-restore rollback has been operated. The distinct
  fresh local target drill passes and is not represented as external evidence.
- No final wheel/image SBOM, vulnerability disposition, provenance attestation, short-lived OIDC
  signing, registry promotion, or clean exact-commit release evidence has been accepted.
- The GitHub `production-release` environment is not configured and `main` is not protected in the
  current repository. The workflow now fails before checkout unless its environment-only approval
  sentinel exists, but release publication remains NO-GO until reviewers and ref protections are
  operated.
- Windows/PowerShell and target-provider/cluster/operator paths remain unverified unless the final
  command table explicitly records otherwise.
- M30 production evaluation/security verification and M31 pilot/GA have not started.

## Blockers

- Publication acceptance still requires the corrected commit to pass replacement hosted PR
  checks; local 59-test supply-chain and image evidence is not a hosted pass.
- Production acceptance is blocked on real provider, cluster, telemetry, remote retention,
  recovery/rollback, production traffic/SLO, vulnerability disposition, and independent
  security/operator evidence.
- Release acceptance is blocked on a reviewed clean commit, successful hosted checks, immutable
  artifact/SBOM/provenance subjects, protected signing/attestation, exact deployment identity, and
  M30/M31 approval.

## Next milestone readiness

- Dependencies satisfied: the reproducible local M29 baseline is complete. Operated production
  and release dependencies are not satisfied.
- Recommended next prompt: prepare M30 only after its provider, target-environment, data-governance,
  security, and independent-review prerequisites exist; do not import a production claim from
  static or local M29 evidence.
- Required operator prerequisites: approved provider IAM and exact-version secrets; isolated
  workload/egress design; target Kubernetes admission context; TLS/PKI and identity configuration;
  metrics/alert/SIEM destinations; immutable encrypted backup store and distinct restore target;
  protected release environment; production evaluation data governance; and independent
  security/operator reviewers.
