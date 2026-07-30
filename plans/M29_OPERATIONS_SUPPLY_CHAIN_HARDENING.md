# M29: Operations, infrastructure, supply-chain, and recovery hardening

- Status: reproducible local baseline accepted; external production/release operation not accepted
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

CI builds the wheel and runtime image once, generates CycloneDX SBOMs, scans dependencies and the
image, records immutable digests, and emits attestable provenance. Publication/signing uses
short-lived GitHub OIDC and environment approval; no registry or signing key is committed.
Pull-request CI verifies the same deterministic bytes but does not claim a production signature.

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

## Implementation status — 2026-07-29

The working tree now contains the local M29 vertical slices: exact-version remote connector,
DataHub-catalog, and semantic-registry secret resolution; control schemas v10/v11 and the
aggregate-only observer; structured telemetry, bounded metrics, alert/SLO/dashboard/SIEM
contracts; a distinct-identity/default-deny Kubernetes reference profile; frozen dependency and
hashed runtime/build inputs with supply-chain validation; verified-pair retention and recovery
policy tooling; a six-state operations view; and managed-web refusal of synchronous execution and
publication.

The reproducible local gates now pass: schema v11 and the seven-role boundary, focused/full
PostgreSQL cuts, deterministic acceptance/evaluation, installed wheel, frozen dependency and
supply-chain policy, local distinct-target recovery, scale contracts, and the final internal
browser matrix. The monolithic quality command passed supply-chain/release audit, Ruff, and mypy
before the local command executor stopped it at its 600-second limit with pytest still passing at
68%. Its exact 3154-test selection then passed in exhaustive disjoint shards
(3079 + 39 + 22 + 14), with 211 service tests deselected; no assertion failed. The retained full
coverage baseline over unchanged `src/schemabridge` is 3325 tests with 14 explicit external skips
at 81.09%; that 2436-second command was not repeated after this supply-chain-only patch. Missing
DataHub credentials and the retained M27 browser fixture remain explicit external skips. No
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
      the closed 61-resource local render and static validator.
- [ ] The same network, TLS, identity, image, admission, disruption, topology, and resource
      controls pass server-side validation and enforcement in the target cluster.
- [x] Local cross-capability and cross-tenant secret/source attempts fail under the seven-role and
      exact-route test matrix.
- [ ] Target-provider IAM and workload isolation prove that no workload can enumerate all tenant
      credentials.

### Observability and operations

- [x] API and every worker process emit schema-versioned JSON events with bounded correlation and
      stable codes; hostile or sensitive input cannot become a log field.
- [x] Health/ready/metrics paths are bounded, credential-free, mutation-free, and source-I/O-free.
- [x] Closed OpenMetrics names/labels cover the required availability, queue, lease, retry,
      dead-letter, timeout, capacity, secret, backup, and release signals without high cardinality.
- [x] Versioned alert rules validate and every page-level alert links to an exact runbook.
- [x] The provider-neutral SIEM adapter is buffered, bounded, detects loss, and cannot log or retry
      a sensitive payload.
- [ ] TLS-authenticated delivery, rejection, retry, loss detection, and resolution are operated
      against the production SIEM destination.
- [x] SLO/error-budget definitions and dashboard panels use the same metric/rule names and label
      local evidence honestly.
- [x] Internal-browser desktop and mobile operations views show healthy, degraded, secret outage,
      queue backlog, stale backup, and failed release states with safe diagnostics, clean console,
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
- [x] Fixture-backed wheel/runtime-image SBOM and evidence-contract validation covers exact
      direct/runtime dependencies and rejects source/artifact digest mismatches; it is not a final
      local image build claim.
- [ ] The final pushed image and wheel receive complete hosted CycloneDX subjects bound to the
      exact clean revision and registry manifest digest.
- [x] The exact hashed runtime/build dependency audit reports zero known vulnerabilities,
      direct-license inventory has no unknown/prohibited package, and local
      image-scan/exception schemas fail closed.
- [x] The final local BuildKit image passes non-root runtime smoke, `pip check`, API/UI imports,
      no-build-tool/no-wheelhouse checks, and a current Trivy scan with zero HIGH/CRITICAL
      findings. This is local candidate evidence, not a registry or protected-release claim.
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
      browser-runtime tests pass.
- [x] Real PostgreSQL integration/acceptance, deterministic evaluation, runtime wheel, release
      audit, frozen install, local SBOM/scan/provenance policy, recovery drill, all `make check`
      components via exhaustive sharding, scale postflight, and `git diff --check` pass locally.
      The retained 81.09% coverage baseline targets unchanged `src/schemabridge`; the full
      2436-second coverage command was not repeated after the final supply-chain-only patch.
- [ ] Final registry-image SBOM, scan, protected provenance/attestation, and target-environment
      evidence pass against the exact clean release revision.
- [x] Internal-browser final validation runs after the last UI/operator-byte change and records
      exact desktop/mobile cleanup.
- [x] `PROJECT_STATE`, `DECISION_LOG`, `CURRENT_TASK`, architecture/security/test/deployment/
      runbook/browser docs, commands, failures, corrections, limitations, and handoff match the
      actual evidence.
- [x] The precommit Git secret/artifact/history scan is clean and the candidate contains no keys,
      local runtime artifacts, backups, tokens, logs, caches, or generated private evidence.
- [ ] The exact focused M29 commit is pushed and passes the strict clean-revision scan plus hosted
      PR checks.

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
