# M29: Operations, infrastructure, supply-chain, and recovery hardening

- Status: final-byte local acceptance closed; initial branch commit published; corrective hosted
  supply-chain validation and external production/release operation remain pending
- Started: 2026-07-29
- Timebox: four sequential phases; no phase is accepted independently
- Recommended Codex: GPT-5.6 Sol — Extra High (xhigh)
- Dependency: accepted local M28 connector-routing and cost-control baseline

## Objective

Turn the M28 production-shaped runtime into an operable, fail-closed deployment candidate. M29
must make every workload identity, secret capability, network path, telemetry signal, build input,
release artifact, backup, and recovery action explicit and independently verifiable.

M29 retains the governed product boundary: source databases are read only, the LLM emits only
typed intent, deterministic code compiles and validates SQL, ambiguous semantics require human
approval, and DataHub mutation remains separately approved and audited.

Local acceptance is evidence that the repository's implementation and reproducible drills pass.
It is not a production or release GO. Provider/legal approval, real-tenant adversarial evaluation,
penetration testing, an operated pilot, and GA sign-off remain M30/M31 work. Conversely, a static
manifest alone is not M29 acceptance: the secret, identity, telemetry, supply-chain, and recovery
paths must each have an executable local or hosted proof, and any provider-only step must remain an
explicit NO-GO item until operated in the target environment.

## Product and architecture boundary

M29 adds no new query language, source dialect, automatic semantic approval, raw-SQL endpoint,
cross-connection join, source write, or LLM execution authority.

The production topology remains separated by capability:

| Workload | Private capability | Required identity | Forbidden capability |
|---|---|---|---|
| web/runtime | preflight only | `schemabridge-web` | execution, catalog, profile |
| API | control API only | `schemabridge-api` | every source/DataHub route |
| execution worker | execution route only | `schemabridge-worker` | preflight, catalog, profile |
| catalog indexer | catalog route only | `schemabridge-catalog` | source preview/profile |
| profile worker | aggregate profile route only | `schemabridge-profile` | preview/catalog |
| reconciler | control reconcile + immutable registry read | `schemabridge-reconciler` | source routes |
| migrator | reviewed schema mutation | `schemabridge-migrator` | runtime traffic |
| backup/restore | backup or fresh-target restore only | `schemabridge-backup` | runtime/source traffic |
| observer | sanitized operational reads/export only | `schemabridge-observer` | mutation and secrets |

Application code depends on a small secret-resolution port. The owner-only M28 resolver remains
available only for local tests and recorded demos. A managed/production composition must use an
authenticated remote resolver, exact immutable secret version, verified TLS, short-lived workload
identity, and capability-scoped path. It may not silently fall back to a directory, environment
credential, default service account, or global connector secret.

## Remote secrets, workload identity, and rotation

The remote secret protocol is provider-neutral at the application boundary and has a concrete
Vault/OpenBao-compatible HTTPS adapter for reproducible local evidence. It:

- authenticates with a projected, audience-bound workload JWT and a closed role;
- accepts only `https://`, a configured trust bundle, and hostname verification;
- reads an exact KV-v2 mount/path/version derived from the private binding;
- bounds token/response sizes and timeouts, rejects redirects and unknown JSON structure;
- keeps JWTs, client tokens, secret paths, bindings, DSNs, endpoints, and payloads out of repr,
  logs, exceptions, metrics, traces, state, and public responses;
- caches no secret beyond one operation and never writes it to disk;
- exposes stable sanitized failure codes and performs no source I/O after any auth/read failure.

The Kubernetes profile uses a distinct `ServiceAccount` and explicit projected token per
secret-reading workload. Automatic service-account token mounts remain disabled. Application pods
receive no Kubernetes API RBAC and no long-lived cloud key. Provider IAM policies and secret paths
must be narrower than the application's logical capability.

Rotation is versioned and ordered:

1. create a new external secret version and source credential;
2. verify the new reader independently;
3. create and approve the next immutable route revision;
4. canary, drain, and activate with compare-and-swap;
5. prove the former target is stale and cannot start new I/O;
6. revoke the old external credential/version;
7. record sanitized audit evidence and test the rollback boundary.

Changing secret material in place under the same executable target is forbidden.

## TLS, network, tenancy, and workload hardening

The managed Kubernetes profile includes:

- an explicit namespace, default-deny ingress and egress, resource quota, and limit range;
- one service account per workload, no wildcard role, no application `ClusterRole`, and no default
  service-account use;
- TLS ingress for the API/web path, HTTPS redirect/HSTS at the ingress, approved hostnames, and no
  clear-text external listener;
- verified TLS for control PostgreSQL, source PostgreSQL, DataHub, OIDC/JWKS, secret manager, and
  telemetry export;
- component-specific ingress/egress allowlists, including explicit DNS and telemetry paths;
- no worker/catalog/profile/reconciler ingress;
- non-root UID/GID, seccomp, dropped capabilities, read-only root filesystem, bounded writable
  memory volumes, probes, resource requests/limits, PDBs, topology spread, and graceful drain;
- immutable image digests and deployment preflight that rejects every placeholder.

Standard `NetworkPolicy` cannot authorize a dynamic external hostname or isolate all tenants that
share one pod. Therefore managed multi-tenant execution requires an approved tenant/capability
egress broker or isolated workload pools with separate service accounts and policies. A single pod
mounting or enumerating every tenant secret is not production acceptable. The local profile proves
the contracts without claiming a target-provider implementation.

## Observability, SLOs, alerts, and SIEM

All long-running processes emit one-line structured JSON with:

- UTC timestamp, severity, schema version, service, environment, event, outcome, duration, and
  bounded public correlation identifiers;
- allowlisted operational counts and stable error codes only;
- automatic redaction/rejection of credentials, secret paths/bindings, SQL, parameters, prompts,
  claims, source values, result rows, raw plans, and stack traces.

HTTP requests accept or generate one bounded request ID, return it, and place it in the matching
structured event. Background operations use public job/refresh/profile fingerprints only where
their existing public contract permits them.

The observer exposes bounded OpenMetrics derived from process events and sanitized control-plane
state. Metrics have a closed name/label registry with no tenant, actor, table, field, SQL, prompt,
secret, route binding, or arbitrary error text label. Health and metrics endpoints do not mutate,
poll source systems, or require source credentials.

The deployment contains versioned recording/alert rules and a provider-neutral SIEM export
contract for at least:

- API availability, latency, and authorization denials;
- execution, catalog, profile, and reconciliation queue depth/age;
- lease reclaim, retry, dead-letter, stale authorization, cancellation latency, and source
  timeout/error rates;
- route/secret/TLS/identity failures and denied cross-capability access;
- backup freshness, restore-drill age, audit-chain failure, and release-policy failure;
- capacity saturation, pod restart/readiness, database-pool pressure, and telemetry loss.

Each page-level alert has severity, threshold/window, owner, runbook link, deduplication key,
safe diagnostic fields, and explicit resolution condition. SLOs define availability, latency,
freshness, and correctness indicators plus windows and error budgets. Synthetic/local numbers are
labelled as evidence, never production targets achieved.

## Reproducible dependencies and software supply chain

M29 tracks a complete deterministic lock for all project extras. CI and release installation use
the frozen lock; production images install exact hashed runtime requirements and the local project
without resolving new dependencies. The final image uses the exact Python 3.13.14/Alpine 3.24
digest. Its sole sdist-only Linux dependency is built with pinned build tooling and the upstream
source timestamp, then SHA-bound and installed with the rest of the wheelhouse through read-only
BuildKit mounts and no runtime network access. The secure `setuptools` backend is isolated from
the DataHub-constrained application lock in its own exact hashed input; Python vulnerability
auditing covers the runtime export, project-wheel build export, and this isolated backend.

Every GitHub Action is pinned to a full immutable commit SHA with its reviewed release in a
comment. Docker base images remain digest-pinned. The supply-chain gate fails for:

- an unpinned action or image;
- a stale/mismatched lock or unhashed production requirement;
- a secret or forbidden artifact;
- a prohibited or unknown direct dependency license;
- a high/critical known vulnerability without an explicit, dated, owned exception;
- missing SBOM components, artifact digest, source revision, or provenance subject;
- a release artifact built from a dirty or non-exact revision.

CI builds the wheel and runtime image, generates CycloneDX SBOMs, scans dependencies and the image,
records immutable digests, and emits attestable provenance. Release promotion is manually
dispatched from an existing annotated SemVer tag through seven capability-separated jobs:
`audit → prepare → candidate → scan → attest → promote → release`. The protected `audit` job is
GET-only, checks out no repository code, executes no repository script, and uses no third-party
action. Its environment-only, repository-scoped audit token verifies the exact annotated tag,
current protected `main`, required push CI, ruleset, immutable-Release setting, and absence of
newer public Release/GHCR state before any build or mutation. An exact previously published
immutable Release is a bounded no-op only after its image, attestations, metadata, body, and
exactly ten public assets are reverified; all downstream jobs are skipped. A fresh path builds
once, publishes or resumes only an exact candidate, scans and attests the remote manifest,
promotes the same digest, and publishes the Release only as its terminal step. Five independent
approvals and all post-readback checks must complete within seven calendar days of `audit`; the
35-day artifact retention exists only as a further 28-day incident-analysis buffer. Signing uses
short-lived GitHub OIDC; no registry or signing key is committed. Pull-request CI does not claim a
production signature.

## Backup, retention, recovery, and rollback

M23's signed transaction-consistent control-plane backup and fresh-target restore remain the data
integrity primitive. M29 adds:

- explicit RPO/RTO policy, schedule, owner, encryption, immutable remote retention, and expiry;
- a fail-closed retention planner that never follows symlinks or deletes an unverified pair;
- remote transfer by a dedicated workload identity, never by an application pod;
- restore only to a distinct empty database, complete hash/schema/state/audit verification, and
  external explicit cutover;
- a reproducible drill that measures backup age, backup duration, restore duration, verification,
  RPO/RTO result, and rollback decision without exposing paths or credentials;
- release rollback that preserves forward-compatible schema, or restores a separately verified
  pre-migration backup; no automatic down-migration;
- incident/runbook steps for secret compromise, provider outage, telemetry loss, corrupt backup,
  failed deploy, source outage, control-plane outage, and cross-tenant suspicion.

Default policy for the production profile is hourly backup, RPO <= 60 minutes, RTO <= 4 hours,
35-day daily retention plus 12 monthly recovery points. A target operator may choose stricter
values, never weaker values without a recorded risk acceptance.

## Implementation status — 2026-07-30

The working tree contains the local M29 vertical slices plus the final audit remediations:
exact-version remote connector, DataHub-catalog, and semantic-registry secret resolution; control
schemas v10/v11/v12 and eight distinct control credentials; the aggregate-only observer; a
dedicated scheduled-backup identity and entrypoint; a 63-resource Kubernetes reference profile;
producer-backed active observability; frozen dependency and supply-chain inputs; verified-pair
retention and recovery policy tooling; and a fail-closed managed-web process boundary.

Schema v12 grants `schemabridge_backup` complete read-only control-schema access without
migrator/runtime/source authority. The hourly CronJob uses that identity, a versioned backup secret,
an external PVC, and only the `control-backup` egress capability. `pg_dump` and `pg_restore` receive
an allowlisted process environment containing only `PATH` and required `PG*` values. The runtime
image fetches exactly five versioned Alpine APKs for amd64/arm64 with reviewed SHA-256 values and
installs them from a read-only BuildKit mount with `apk --no-network`; it neither copies an
untracked filesystem closure nor resolves packages in the final stage.

Only eight metric families with a real composed producer are active. The active bundle contains six
alerts, three SLOs, and eight dashboard panels, and the Kubernetes PrometheusRule carries exactly
those active alert groups. SIEM and every uncomposed backup/release/integrity/capacity contract are
kept under `deploy/observability/inactive/`; they are validated design inputs, not delivery,
paging, or health evidence. Managed `schemabridge-web` preflights configuration, authentication,
and schema before launching Streamlit. Startup/readiness repeat those checks and require the exact
bounded loopback Streamlit health response. Staging/production Operations reports unavailable
without an operated data source and never substitutes synthetic healthy/degraded states.

Release promotion is now a seven-job replay-safe prepublication state machine:
`audit → prepare → candidate → scan → attest → promote → release`. The protected, GET-only
`audit` job runs without checkout, repository code, or third-party actions. It either proves an
exact historical immutable publication and skips all mutation-capable jobs, or admits a fresh
current-`main` build only after branch/ruleset, CI, immutable-Release, monotonic Release/GHCR, and
reference-absence checks pass. Candidate resumption is digest exact; scan credentials are isolated
and always cleaned; all ten public assets, including `release-body.md`, plus image and attestations
are reverified after publication. The five-approval publication window is bounded to seven days.
Branch/rule lookup, reference absence, authentication, transport, archive parsing, checksum,
attestation, and digest checks fail closed. Exclusive Release/GHCR write authority, protected
environment/reviewer policy, and target operation remain external prerequisites.

Current final-byte local evidence now closes the 316-test focused M29 cut; the 158-test
release/supply-chain cut and 888-file/23-license static audit; clean schema-v12/eight-credential
source/control separation; the focused backup/restore cut; full integration and acceptance;
deterministic 11-table/465-row evaluation with `live_llm=not_run`; the installed wheel through
migrations 1–12 and all entrypoints; local recovery policy and fresh-target evidence; scale
postflight; the final internal-browser matrix; and the local BuildKit image/SBOM/vulnerability
smoke. The image is local pre-commit evidence built from stable product bytes: its OCI revision
label still identifies the prior HEAD, so it is not an exact-commit or release artifact.

Exact closed results are 316 focused component tests in 17.74 seconds; 158 final
release/supply-chain tests; backup/restore 6 passed and 5 deselected in 5.96 seconds; integration
161 passed, 11 skipped, and 3327 deselected in 250.42 seconds; acceptance 43 passed, 4 skipped,
and 3452 deselected in 48.51 seconds; scale 25 passed in 3.16 seconds plus 8 passed and
4 deselected in 4.41 seconds; final `make check` 3339 passed and 214 deselected in 1503.89 seconds
with Ruff over 608 files and strict mypy over 308 files; current-byte coverage 3534 passed,
14 skipped, and 1 deselected at 81.17% in 4149.89 seconds; and recovery fingerprint
`24bdecb8bcb8faab8ba83d64eadf201c0b1d31142ebab773b012735f8f6f34ae`.
The local image is
`sha256:db2a42ce187243c809a9fcc1272246fb914fbeefc9186b7a83b651014d015b79`,
182236711 bytes, user 10001:10001, with `pg_dump`/`pg_restore` 16.14, `pip check`, `ldd`, all
entrypoint smoke, exact 107-component SBOM, zero-known-vulnerability `pip-audit`, and passing Trivy
HIGH/CRITICAL policy. Its OCI revision is prior HEAD
`9e8b69e1adce8e144b345d3b0d33482558804dc6`.

The final release topology is **PASS_LOCAL** at workflow SHA-256
`9979c54be6ba39d1d7b606e6d882aa10e9868bb9d003482b30a780ee104d1f26`:
Actionlint 1.7.12, ShellCheck 0.11.0, static policy, 158 adversarial tests, and independent review
all pass with zero local P0/P1/P2 findings. Current-byte coverage passes at 81.17%, superseding
D115's 81.09% historical baseline. Initial commit
`09c3a2e0f47a7fbadb5297fa6bc4f9aca0d21950`, its exact revision/history scans, and draft-PR
publication pass. Run `30560980711` failed closed on the pinned base image's distinct x86_64
virtual `.python-rundeps` timestamp and was then cancelled; D124 binds the exact arm64/x86_64 pair.
The corrective revision scan, push, and hosted rerun remain **PENDING**. D123 records local
acceptance. No
target-provider rotation/revocation, cluster-side admission, real alert/SIEM delivery, immutable
remote retention, external cutover/rollback, protected release attestation, production
traffic/SLO, or external security/operator review has been operated. Production and release
therefore remain **NO-GO**, and M30/M31 remain blocked.

## Acceptance criteria

### Plan and boundaries

- [x] Plan, prompt, ADR 0014, threat/identity/network/secret matrices, SLOs, and RPO/RTO are
      reviewed before production source changes.
- [x] M28 query, semantic, SQL, fanout, source-read-only, approval, and dynamic-catalog invariants
      remain unchanged.
- [x] No M30 evaluation threshold or M31 pilot/GA claim is pulled into M29.

### Secrets, identity, TLS, and network

- [x] A typed application port permits local and remote connector-secret adapters without concrete
      adapter coupling.
- [x] Managed/production config requires the remote mode and cannot fall back to local files,
      global connector credentials, environment bearer tokens, or default service accounts.
- [x] Remote auth/read uses verified TLS, projected audience-bound identity, an exact secret
      version, strict response bounds, sanitized failures, and zero disk persistence.
- [x] Wrong audience/role, expired token, redirect, non-HTTPS URL, bad CA/hostname, missing/wrong
      version, malformed/oversized payload, timeout, and provider outage cause zero source I/O.
- [x] Secret, JWT, client token, binding, path, DSN, endpoint, username/password, and provider
      response scans are clean across repr, logs, exceptions, metrics, state, responses, and UI.
- [ ] Rotation/revocation proves new revision activation, stale old target rejection, old external
      credential denial, audit evidence, and bounded rollback.
- [x] Kubernetes resources define distinct service accounts, explicit projected identities,
      no automatic tokens, no application API RBAC, and no wildcard permissions.
- [x] Namespace default deny, per-component ingress/egress, DNS/telemetry exceptions, TLS ingress,
      source/control/provider TLS, immutable images, PDB/topology spread, and resource bounds pass
      the closed 63-resource local render contract, including the backup CronJob and exact
      PrometheusRule.
- [ ] The same network, TLS, identity, image, admission, disruption, topology, and resource
      controls pass server-side validation and enforcement in the target cluster.
- [x] Local cross-capability and cross-tenant secret/source attempts fail under the
      eight-credential control boundary and exact-route test matrix.
- [ ] Target-provider IAM and workload isolation prove that no workload can enumerate all tenant
      credentials.

### Observability and operations

- [x] API and every worker process emit schema-versioned JSON events with bounded correlation and
      stable codes; hostile or sensitive input cannot become a log field.
- [x] Health/ready/metrics paths are bounded, credential-free, mutation-free, and source-I/O-free.
- [x] The active OpenMetrics surface is limited to eight closed, low-cardinality families with an
      explicit runtime or observer producer.
- [x] Six active producer-backed alerts validate, are mirrored exactly by the PrometheusRule, and
      link to exact active runbooks.
- [x] The provider-neutral SIEM and uncomposed signal contracts remain schema-validated under the
      inactive bundle and cannot be mistaken for active delivery or paging.
- [ ] Every currently inactive signal has a lifecycle-owned producer/exporter and operated
      delivery before it is moved into the active bundle.
- [ ] TLS-authenticated delivery, rejection, retry, loss detection, and resolution are operated
      against the production SIEM destination.
- [x] Three active SLOs and eight active dashboard panels use only producer-backed metric names and
      label local evidence honestly.
- [x] Internal-browser final-byte validation covers local/demo scenarios plus the managed
      Operations-unavailable state at desktop and mobile with safe diagnostics, clean console,
      escaped hostile text, and no horizontal overflow.

### Supply chain

- [x] The complete lock is current, frozen installation succeeds from a clean checkout, and
      runtime requirements are exact and hash-verified.
- [x] Every workflow action and container base is immutable-SHA/digest pinned; a regression test
      rejects tag references and mutable images.
- [x] Every Trivy action writes cache only under ignored `.local/trivy-cache`; static workflow
      policy rejects an omitted or different path as `trivy_cache_path_invalid`, with CI and
      release regressions.
- [x] `pip-audit --disable-pip` covers the complete frozen runtime and build inputs, including the
      isolated non-vulnerable `setuptools` backend, and the reviewed Alpine image builds
      `watchdog` reproducibly into the exact SHA-bound local requirement. Static policy rejects a
      mutable base, missing epoch/hash/BuildKit contract, any changed/additional final-stage
      command, networked runtime install, or retained wheelhouse layer.
- [x] The PostgreSQL client closure is an exact five-APK amd64/arm64 matrix with reviewed versions,
      URLs, and SHA-256 values; the final image installs it only from a read-only mount with
      `apk --no-network`, and static policy rejects extra APKs, mutable coordinates, raw copying,
      or networked final-stage resolution.
- [x] Fixture-backed wheel/runtime-image SBOM and evidence-contract validation covers exact
      direct/runtime dependencies and rejects source/artifact digest mismatches; it is not a final
      local image build claim.
- [ ] The final pushed image and wheel receive complete hosted CycloneDX subjects bound to the
      exact clean revision and registry manifest digest.
- [x] The exact hashed runtime/build dependency audit reports zero known vulnerabilities,
      direct-license inventory has no unknown/prohibited package, and local
      image-scan/exception schemas fail closed.
- [x] The final local BuildKit image passes non-root runtime smoke, `pip check`, API/UI imports,
      `pg_dump`/`pg_restore`, no-build-tool/no-wheelhouse/no-staged-APK checks, and a current Trivy
      scan with zero HIGH/CRITICAL findings. This will be local candidate evidence, not a registry
      or protected-release claim.
- [ ] The final registry image scan and any dated, owned vulnerability disposition pass in the
      protected hosted release.
- [x] Provenance policy binds repository, revision, builder, workflow, artifact, and SBOM digests;
      the protected workflow uses only short-lived OIDC and cannot sign a pull request as a
      production release.
- [ ] The protected environment actually signs, attests, and verifies those exact clean subjects.
- [x] CI artifact upload is a single immutable-action step with a seven-path non-secret allowlist,
      mandatory hidden-path opt-in, failure on no files, and bounded retention.

### Backup, recovery, and rollback

- [ ] Backup schedule, encryption, immutable retention, ownership, RPO/RTO, restore target, and
      cutover authority are explicit and placeholder-free in the operated profile.
- [x] Retention keeps required hourly/daily/monthly points, verifies signed manifest/archive
      pairs, rejects symlinks/tampering, and has a dry-run default.
- [x] Schema v12, `schemabridge_backup`, the sanitized scheduled entrypoint, the hourly CronJob,
      exact versioned inputs, subprocess environment allowlist, and read-only/write-denial
      contracts are explicit in the local implementation.
- [x] A complete fresh-target backup/restore drill passes exact checksum, schema, state,
      audit-chain, pointer/history/outbox/quarantine checks and records sanitized timing evidence.
- [x] Restore never targets the active/source database; failed verification cannot cut over.
- [x] Local rollback contracts permit only a forward-compatible image or separately verified
      restore and introduce no down-migration or source write.
- [ ] Application/image rollback, external cutover, and verified-restore rollback are rehearsed in
      the operated target environment.
- [x] Secret compromise, provider outage, telemetry loss, backup corruption, failed deployment,
      and cross-tenant suspicion runbooks contain executable detection, containment, recovery, and
      evidence steps.
- [ ] Those incident paths are drilled with target-provider, cluster, storage, and alert evidence.

### Final gates and handoff

- [x] Focused unit, security, manifest, observability, supply-chain, retention, recovery, and
      browser-runtime tests pass on the final bytes.
- [x] Real PostgreSQL integration/acceptance, deterministic evaluation, runtime wheel, release
      audit, frozen install, local SBOM/scan/provenance policy, recovery drill, `make check`, scale
      postflight, and `git diff --check` pass locally on the final bytes.
- [ ] Final registry-image SBOM, scan, protected provenance/attestation, and target-environment
      evidence pass against the exact clean release revision.
- [x] Internal-browser final validation runs after the last UI/operator-byte change and records
      exact desktop/mobile cleanup.
- [x] `PROJECT_STATE`, `DECISION_LOG`, `CURRENT_TASK`, architecture/security/test/deployment/
      runbook/browser docs, commands, failures, corrections, limitations, and handoff match the
      actual evidence.
- [x] The precommit Git secret/artifact/history scan is clean and the candidate contains no keys,
      local runtime artifacts, backups, tokens, logs, caches, or generated private evidence.
- [ ] The exact corrective M29 commit is pushed and passes the strict clean-revision scan plus
      hosted PR checks. The initial commit and scan passed; run `30560980711` exposed D124 and was
      cancelled after the true supply-chain failure.

## Implementation sequence

1. Freeze this plan/prompt/ADR and add failing contract tests.
2. Generalize the secret port, implement exact-version remote resolution, managed-mode refusal,
   rotation/revocation tests, and source TLS enforcement.
3. Add the Kubernetes identity/TLS/network/topology profile plus static and cluster validation.
4. Add structured redacted logging, bounded metrics/health, rules, dashboards, SIEM contract, SLOs,
   and incident runbooks.
5. Add frozen dependency/runtime locks, pinned workflows, SBOM/vulnerability/license/provenance
   gates, and immutable image build.
6. Add retention policy, remote-backup workload contract, recovery/rollback drill, and evidence.
7. Run focused then full service/supply-chain/recovery gates.
8. Run the internal-browser operations matrix on final bytes.
9. Consolidate docs/state/handoff, scan secrets, commit, and push.

M30 remains blocked until every criterion above is checked and has corresponding executed
evidence; a checked local criterion never satisfies a separate unchecked external criterion.
