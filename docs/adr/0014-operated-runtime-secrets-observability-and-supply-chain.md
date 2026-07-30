# ADR 0014: Operated runtime identity, secrets, observability, and supply chain

- Status: accepted for M29 implementation
- Date: 2026-07-29

## Context

M28 binds executable work to an exact tenant connection, route revision, PostgreSQL dialect,
reader, semantic evidence, and cost budget. Its private connector bindings are resolved from
strict owner-only local files. The checked-in Kubernetes manifest is deliberately a reference:
it mounts Kubernetes Secrets, uses no workload identity, defines no ingress TLS or NetworkPolicy,
exports no bounded metrics, pins no complete dependency lock, produces no SBOM/provenance, and
does not schedule or retain control-plane backups remotely.

Those omissions are not presentation polish. They allow a compromised workload to reach unrelated
networks, make secret rotation depend on pod restart and mutable material, hide queue/lease/backup
failure from operators, permit clean builds to resolve different bytes, and leave recovery as an
unrehearsed command.

## Decision

### Keep one provider-neutral secret port and require remote mode when managed

Application code receives a small connector-secret resolver protocol. Local owner-only files
remain test/demo evidence only. Managed and production environments must compose a remote
exact-version resolver authenticated by a short-lived, audience-bound projected workload JWT.

The first concrete remote contract is Vault/OpenBao-compatible HTTPS KV v2 because it can be
tested without adding a cloud-specific authority to the application. Target deployments may
substitute an adapter behind the same port, but must prove equivalent identity, version, TLS,
audit, rotation, revocation, and denial properties.

No connector secret is cached across operations or written to disk. The binding, provider path,
JWT, client token, secret payload, DSN, and endpoint remain private adapter values. Changing secret
material requires a new external version and route revision; in-place mutation under an approved
target is rejected operationally.

The external provider version and the public route revision are separate identities. Each private
capability binding stores an explicit immutable provider version, and the approved route
fingerprint binds that version with a one-way digest of the opaque reference. Resolvers read only
the stored provider version; they never derive it from `route_revision` or request `latest`.
Historical bindings without an explicit provider version remain non-executable after migration
and require a newly approved route rotation rather than an inferred backfill.

### Preserve the browser-authentication composition boundary

The browser-authentication configuration contract belongs to
`application/ports/browser_auth.py`. The Streamlit-specific private-TOML validator belongs to
`adapters/identity/streamlit_auth.py` and depends only on that inward port. `bootstrap.py` is the
only component that constructs the adapter and places it in the Streamlit runtime options; the
entrypoint supplies private `st.secrets` through the port but never imports the concrete adapter.
Bootstrap likewise imports no Streamlit entrypoint. This keeps secret validation outside the UI
without recreating a bootstrap↔entrypoint cycle.

### Give each workload a distinct identity and explicit network

Every workload uses a separate Kubernetes ServiceAccount with automatic token mounts disabled.
Only secret-reading workloads receive an explicit projected token for the secret-manager
audience. Application identities receive no Kubernetes API RBAC and no static cloud key.

The namespace is default-deny. Ingress and egress are allowlisted per component, including DNS,
control PostgreSQL, source/tenant egress boundary, DataHub, identity provider, and secret manager.
TLS and hostname verification are mandatory at every composed external trust boundary. An
unimplemented destination is not granted speculative network access: M29 has no OTLP exporter, so
the OpenTelemetry collector peer and TCP/4317 egress are absent and rejected by rendered-manifest
mutation tests.

Because standard NetworkPolicy cannot authorize arbitrary tenant destinations and one shared pod
cannot isolate all mounted tenant secrets after compromise, production multi-tenancy must use
isolated workload pools or an approved capability-aware secret/egress broker. Logical M28 routing
alone is not accepted as infrastructure isolation.

### Treat telemetry as a governed output

Runtime events are schema-versioned JSON with closed fields and stable codes. HTTP correlation is
bounded and returned to the caller. Metrics use an allowlisted low-cardinality registry and
contain no user/tenant/schema/field/SQL/prompt/secret dimensions. Health and metrics perform no
source I/O or mutation.

Versioned alert and SLO definitions are built from those exact signals. The inactive SIEM design
requires authenticated bounded delivery and explicit loss reporting, but no SIEM or OTLP exporter
is currently composed. The active bundle admits only metric families with real producers, validates
each of six alert expressions against its exact canonical PromQL token sequence, closes the three
active SLO indicator shapes and metric tuples, and delivers the same alert group as a
`PrometheusRule`. Mutation tests reject semantic operator/function/threshold/vector-matching drift,
trailing syntax, SLO field/outcome drift, uncomposed metrics, and speculative TCP/4317 egress.
Operator views expose safe state and runbook links, not raw logs or stack traces.

### Build canonical bytes once and recover them without regeneration

Track a complete frozen dependency lock and hashed production requirements. Pin GitHub Actions and
container bases to immutable identities. CI generates CycloneDX SBOMs, scans
dependencies/images, and binds artifact and SBOM digests into provenance. Manual promotion accepts
only an existing annotated non-prerelease canonical SemVer tag exactly equal to
`v$project_version`.

Release authority is split across exactly seven jobs:

1. protected GET-only `audit` runs without checkout/actions/repository code and either rejects
   drafts, conflicting target state, or newer stable Release/GHCR tags before build, or validates
   an exact immutable hosted/OCI-attested historical no-op;
2. read-only `prepare` validates exact source/default-branch HEAD, CI/tag-ruleset metadata, and
   public/basic external state, builds and gates locally, and seals
   image/evidence/hashes into a run-scoped prepared artifact; it receives neither the protected
   environment nor its audit token;
3. protected inline-only `candidate` receives package-write only and creates the stable semantic
   reference `candidate-${{ github.sha }}`; its same-job retry may adopt only the bounded manifest
   with the sealed config digest;
4. read-only `scan` supplies an isolated pull credential to pinned Trivy, erases it, binds the
   scanner/DB snapshot, and emits the canonical payload;
5. protected `attest` receives attestations/OIDC plus only the package authority needed for OCI
   attestation and creates exact file and image attestations; a rerun may add another exact
   source-bound bundle;
6. protected `promote` receives package-write only and creates or verifies the stable SemVer image
   reference at the exact candidate manifest/config digest; and
7. protected `release` receives contents-write only, plus read access to packages and
   attestations, and reconciles the canonical GitHub Release.

`audit`, `candidate`, `attest`, `promote`, and `release` each use `production-release`; five sequential
environment approvals are therefore an intentional consequence, not a single approval shared
across the state machine. GitHub issues `GITHUB_TOKEN` at job start, so a sentinel step cannot be
the token-issuance boundary. Instead, the four mutation-capable privileged jobs (`candidate`,
`attest`, `promote`, and `release`) contain no checkout, dependency setup, or repository-code
execution, and every such inline script begins exactly with `set -euo pipefail`; `set +e` and
`|| true` error bypasses are forbidden. Their first inline step verifies the exact predecessor
artifact ID/digest/hashes, source, peeled tag, current default-branch HEAD, rules, and
immutable-Releases setting. The sentinel follows that verifier and confirms environment approval
before the first external mutation. Protected `audit` is a separate GET-only preflight with no
upstream artifact or mutation sentinel; its exact historical no-op intentionally omits current
HEAD/rules checks.

Accept the operational consequence of exact default-branch equality: a new publication has an
externally recorded `main` freeze from before dispatch through the immediate post-publication
read-back. Its evidence binds the source SHA, change-window/ticket, administrator, and independent
reviewer; every environment approval reconfirms it. A read-only historical published no-op is
exempt. Movement of `main` fails closed and converts any candidate, attestation, stable tag, or
Release state already written into preserved incident evidence. Recovery must never use a rerun,
new dispatch, deletion, clobber, overwrite, retag, or history/tag rewrite to evade that failure;
it requires a separately approved administrator/security recovery plan.

The mutation window is limited to seven calendar days from protected `audit` start through the
successful post-publication read-back. A run may start only when all five approvals can complete
inside that bound; reaching it stops approvals and enters preserved-state incident recovery. The
35-day retention of the two run-scoped artifacts supplies only a bounded 28-day
investigation/recovery buffer and never authorizes a late approval, rerun, redispatch, or
publication.

Before extraction, each of the four mutation-capable privileged boundaries binds the archive to
the artifact API's byte size and digest. A fixed standard-library ZIP check enforces the exact
flat allowlist/count, CRC, safe
external attributes, per-file and total size, and compression-ratio limits; rejects absolute,
nested, dot-segment, backslash, NUL, duplicate, encrypted, symlink, and device entries; and permits
extraction only into a fresh directory whose outputs are all regular non-symlink files.
Every checksum manifest is then bounded to 4096 bytes and parsed as an exact unique
lowercase-SHA256/two-space/canonical-basename allowlist before `sha256sum --check`.

The release source guard fails closed. Protected `main` must require the exact `quality`,
`postgres-integration`, and `supply-chain` contexts, and the current remote default-branch HEAD
must equal `SOURCE_REVISION` at `prepare` and every mutation-capable privileged boundary; ancestry
is insufficient. The historical `audit` no-op intentionally does not depend on current `main`.
A denied request, network failure, malformed response, missing rule, or failed/stale exact-SHA
push run cannot be interpreted as approval. A candidate or SemVer image reference is absent only
after structured registry HTTP 404 plus an expected manifest/name-unknown code; authentication,
authorization, transport, and other errors fail the release.

`prepare` may inspect tag-ruleset metadata with its normal token but cannot assert bypass state.
Fresh `audit` and each mutation-capable protected job instead list
`GET /repos/{owner}/{repo}/rulesets?includes_parents=true&targets=tag` and fetches the selected
exact `GET /repos/{owner}/{repo}/rulesets/{id}?includes_parents=true`. Exactly one active ruleset
must target tags, include only `refs/tags/v*`, have no excludes, expose `bypass_actors: []`, and
enforce update, deletion, and non-fast-forward protection. GitHub requires ruleset write
visibility to return bypass actors and contents-write visibility to inspect drafts/assets, so the
protected environment holds `SCHEMABRIDGE_RELEASE_AUDIT_TOKEN` with repository Administration
write plus Contents write scopes. It is an environment-only, repository-scoped fine-grained PAT
whose expiry exceeds the maximum approval window plus a recorded buffer and whose
rotation/revocation is operated. A static GitHub App installation token is not accepted because
the workflow does not mint fresh credentials per job. The workflow uses the PAT only for explicit
read-only ruleset, Release/draft/asset, and immutable-Releases `GET` requests and never logs it. Immutable
Releases must be enabled before any publication mutation and are rechecked at privileged
boundaries.

Internal artifact names may include run identity, but every downstream failed-job rerun consumes
the actual canonical outputs of its declared upstream artifact producer. The public evidence and
candidate identity exclude run ID and run attempt. A rerun may create another attestation bundle
only for the same exact subject, source repository, revision, and tag. A newly dispatched complete
workflow requires externally clean candidate, SemVer image-tag, draft, and Release state and no
newer stable Release/GHCR tag; partial or conflicting state fails before rebuilding and is never
regenerated, deleted, clobbered, or overwritten.

The workflow selects `ubuntu-24.04`, but the hosted runner, kernel, BuildKit, Docker daemon, and
toolchain remain mutable. `SOURCE_DATE_EPOCH=1730470033` normalizes timestamps only; it does not
establish bit-for-bit reproducibility across a complete redispatch.
`DOCKER_BUILD_RECORD_UPLOAD=false` prevents the Docker action from adding an unreviewed build
record artifact. Attested release metadata records pip-audit version/service/source/time and the
pinned Trivy action/tool plus DB timestamps/schema and exact metadata/database hashes. This is
temporal scan identity, not a reproducibility claim.

The GitHub Release has one canonical body and exactly ten digest-checked public assets:
`direct-licenses.json`, `pip-audit.json`, `provenance.intoto.json`,
`release-assets.sha256`, `release-body.md`, `release-metadata.json`,
`runtime-image.cdx.json`, `schemabridge-0.1.0-py3-none-any.whl`, `trivy-image.json`, and
`wheel.cdx.json`. The UI body is byte-for-byte identical to `release-body.md`; that file is bound
by `release-assets.sha256`, release metadata, and an attestation. Immediately after publication,
exact-ID and latest-Release reads require exact source/tag/title/body/assets, `draft=false`,
`immutable=true`, current/latest identity, and no newer stable release. A later dispatch for that
exact already-published immutable tag terminates in GET-only `audit` after exact hosted/OCI
attestation and asset/body verification. This historical no-op intentionally does not require
current/latest, no newer version, or current `main`, and never edits, uploads, reconciles, or
invokes `--latest`. A
repository-global non-cancelling FIFO
concurrency group with `queue: max` serializes versions. GitHub currently supports that queue
contract; actionlint 1.7.12's older schema requires one narrowly scoped local lint suppression and
no broader validation exception.

Repository static policy can prove which checked-in workflow has package/content write permission,
but no available GitHub API proves the global absence of competing GHCR `PUT` or GitHub Release
contents writers among humans, PATs, Apps, repositories, and workflows. A current external
administrator audit and a custom deployment-protection rule bound to that evidence are mandatory
before dispatch. Their absence, disabled immutable Releases, or a missing/nonconforming tag
ruleset keeps production and release at NO-GO even when the local state machine passes. A
2026-07-30 read-only preflight observed immutable Releases disabled and zero tag rulesets in the
real repository; that was a time-bound NO-GO snapshot and must be re-audited after remediation,
not treated as an immutable repository fact.

Install backup client tooling as an auditable Alpine package closure, not as raw binaries copied
from a PostgreSQL image. The Docker build accepts only `TARGETARCH=amd64` mapped to Alpine
`x86_64` or `TARGETARCH=arm64` mapped to Alpine `aarch64`. For both branches it pins the complete
official Alpine 3.24 URL and architecture-specific SHA-256 of
`postgresql16-client=16.14-r0`, `libpq=18.4-r0`, `lz4-libs=1.10.0-r1`,
`zstd-libs=1.5.7-r2`, and `postgresql-common=1.3-r0`. A controlled stage downloads and verifies
those five unchanged signed APKs. The final Python/Alpine stage receives them only through a
read-only BuildKit mount and installs them using `apk add --no-cache --no-network`, with default
Alpine signature verification. No APK archive, resolver cache, downloader, or PostgreSQL image
stage remains in the runtime; package metadata remains available to CycloneDX and vulnerability
scanners.
The pinned Python base is multi-architecture and creates one virtual `.python-rundeps` identity
per platform leaf. Evidence therefore permits only the closed pair
`aarch64=20260616.002547/noarch` and `x86_64=20260616.002554/noarch`; no wildcard or range is
allowed. This exception is limited to that virtual metapackage and leaves the real APK inventory,
hash, architecture, dependency-graph, and five PostgreSQL download bindings exact.

Retain M23's signed transaction-consistent backup as the integrity primitive. Add scheduled
encrypted immutable remote retention, a conservative verified-pair retention planner, and a
fresh-target recovery drill with RPO/RTO evidence. Cutover remains explicit. Schema rollback uses
a forward-compatible binary or a separately verified restore; automatic down-migration is
forbidden.

Backup uses a dedicated pre-provisioned `schemabridge_backup` login, never the migrator. Schema v12
fails closed unless the observed catalog posture is exactly `NOSUPERUSER`, `NOCREATEDB`,
`NOCREATEROLE`, `NOREPLICATION`, `NOBYPASSRLS`, `NOINHERIT`, with no membership that can inherit
or `SET ROLE`, global `default_transaction_read_only=on`, no role-and-database-specific
read-only/timeout override, and a positive timeout no greater than 15 minutes. It grants only
complete control-schema reads and does not repair the role. Every backup repeats those checks inside a
`REPEATABLE READ READ ONLY` transaction, additionally binds the observed session/current role and
database, and stops before snapshot export or `pg_dump` on mismatch. The tool child inherits only
`PATH` and allowlisted `PG*` connection variables; failure evidence contains no DSN or credential.

## Consequences

- Production deployment requires secret-manager, identity, network, ingress, telemetry, artifact
  store, and backup-store configuration that cannot be inferred from application defaults.
- A provider outage fails closed before source I/O and becomes a bounded typed operational state;
  paging remains inactive until its metric producer and alert lifecycle are composed.
- Secret rotation creates a new route revision and invalidates prior confirmations; this is
  intentional safety friction.
- Shared all-tenant secret mounts are not a production option.
- Telemetry is less ad hoc but safer, bounded, and reproducible.
- OTLP/4317 remains unavailable until a real bounded exporter and its authenticated delivery/loss
  lifecycle are composed and evidenced.
- Frozen locks and scans increase build work and require explicit time-bounded vulnerability
  exceptions rather than silent upgrades.
- PostgreSQL client upgrades require a reviewed two-architecture URL/hash matrix update and SBOM
  expectation change; an opportunistic repository resolution is intentionally unavailable.
- The pre-publication workflow cannot compensate for an additional GHCR or GitHub Release
  contents writer; a custom deployment-protection rule and current external administrator audit
  are required because repository APIs cannot prove that global absence. Immutable Releases must
  also be enabled and re-read before mutation.
- A fresh full redispatch is not a recovery mechanism for partial publication. Only failed-job
  reruns with the exact canonical predecessor artifacts may resume; otherwise operators stop and
  preserve the conflicting state for incident review.
- Backup existence is insufficient; only a verified fresh-target drill counts as recovery
  evidence.
- M29 local acceptance remains distinct from provider/legal approval, penetration testing,
  real-tenant evaluation, operated pilot, signed release promotion, and GA approval in M30/M31.

## Rejected alternatives

- **Keep local files in production:** provides no remote audit, workload identity, or reliable
  rotation/revocation.
- **Use one environment token or Kubernetes Secret per deployment:** collapses tenant/capability
  boundaries and creates long-lived static authority.
- **Mount every tenant secret into one worker:** logical checks cannot protect those credentials
  after process compromise.
- **Permit plaintext or `sslmode=prefer`:** makes trust depend on network placement and downgrade
  behavior.
- **Open egress and rely on application routing:** does not contain SSRF, dependency, or process
  compromise.
- **Pre-authorize OTLP/4317 for a future collector:** grants unused attack surface and falsely
  implies delivery before an exporter or loss lifecycle exists.
- **Log free-form dictionaries and derive metrics later:** creates secret/high-cardinality leakage
  and unstable alert contracts.
- **Validate only the backup username in the DSN:** does not prove the PostgreSQL-observed session,
  role attributes, memberships, database, read-only transaction, or effective timeout.
- **Parse Streamlit secrets directly in the entrypoint:** couples private transport parsing to the
  UI and reintroduces the forbidden bootstrap↔entrypoint dependency cycle.
- **Pin only direct dependencies or action tags:** still allows transitive/build/workflow bytes to
  change.
- **Generate an SBOM without binding the built artifact:** cannot prove what was scanned or
  deployed.
- **Copy PostgreSQL client binaries and shared libraries from another image:** hides their Alpine
  package identities from the final APK database and can make SBOM/vulnerability results
  incomplete even when the files execute.
- **Back up in place or restore over the active database:** can destroy the evidence needed to
  diagnose and recover.
- **Automatic down-migration:** risks irreversible state loss and violates the reviewed migration
  model.
