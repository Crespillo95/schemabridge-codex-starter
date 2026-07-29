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

### Give each workload a distinct identity and explicit network

Every workload uses a separate Kubernetes ServiceAccount with automatic token mounts disabled.
Only secret-reading workloads receive an explicit projected token for the secret-manager
audience. Application identities receive no Kubernetes API RBAC and no static cloud key.

The namespace is default-deny. Ingress and egress are allowlisted per component, including DNS,
control PostgreSQL, source/tenant egress boundary, DataHub, identity provider, secret manager, and
telemetry. TLS and hostname verification are mandatory at every external trust boundary.

Because standard NetworkPolicy cannot authorize arbitrary tenant destinations and one shared pod
cannot isolate all mounted tenant secrets after compromise, production multi-tenancy must use
isolated workload pools or an approved capability-aware secret/egress broker. Logical M28 routing
alone is not accepted as infrastructure isolation.

### Treat telemetry as a governed output

Runtime events are schema-versioned JSON with closed fields and stable codes. HTTP correlation is
bounded and returned to the caller. Metrics use an allowlisted low-cardinality registry and
contain no user/tenant/schema/field/SQL/prompt/secret dimensions. Health and metrics perform no
source I/O or mutation.

Versioned alert and SLO definitions are built from those exact signals. SIEM export is authenticated,
bounded, and reports its own loss. Operator views expose safe state and runbook links, not raw logs
or stack traces.

### Build and recover exact bytes

Track a complete frozen dependency lock and hashed production requirements. Pin GitHub Actions and
container bases to immutable identities. CI builds once, generates CycloneDX SBOMs, scans
dependencies/images, binds artifact and SBOM digests into provenance, and uses short-lived OIDC
only on protected release events for signing/attestation.

Retain M23's signed transaction-consistent backup as the integrity primitive. Add scheduled
encrypted immutable remote retention, a conservative verified-pair retention planner, and a
fresh-target recovery drill with RPO/RTO evidence. Cutover remains explicit. Schema rollback uses
a forward-compatible binary or a separately verified restore; automatic down-migration is
forbidden.

## Consequences

- Production deployment requires secret-manager, identity, network, ingress, telemetry, artifact
  store, and backup-store configuration that cannot be inferred from application defaults.
- A provider outage fails closed before source I/O and becomes an alertable operational state.
- Secret rotation creates a new route revision and invalidates prior confirmations; this is
  intentional safety friction.
- Shared all-tenant secret mounts are not a production option.
- Telemetry is less ad hoc but safer, bounded, and reproducible.
- Frozen locks and scans increase build work and require explicit time-bounded vulnerability
  exceptions rather than silent upgrades.
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
- **Log free-form dictionaries and derive metrics later:** creates secret/high-cardinality leakage
  and unstable alert contracts.
- **Pin only direct dependencies or action tags:** still allows transitive/build/workflow bytes to
  change.
- **Generate an SBOM without binding the built artifact:** cannot prove what was scanned or
  deployed.
- **Back up in place or restore over the active database:** can destroy the evidence needed to
  diagnose and recover.
- **Automatic down-migration:** risks irreversible state loss and violates the reviewed migration
  model.
