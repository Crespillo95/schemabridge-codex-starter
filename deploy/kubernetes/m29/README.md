# M29 Kubernetes production contract

This directory is an unoperated, production-shaped deployment contract. It is not evidence that a
cluster, ingress controller, egress broker, secret manager, monitoring stack, or backup target has
been operated. The checked-in overlay is deliberately blocked by obvious `.invalid`,
`replace-with-*`, CA, and all-zero digest placeholders.

The base defines:

- one restricted production namespace with quota and default container limits;
- separate service accounts for web, API, execution, catalog, profile, reconciliation, migration,
  backup, and observation, all with automatic API-token mounts disabled;
- seven existing long-running commands only: Streamlit web, API, execution worker, catalog indexer,
  aggregate profile worker, semantic reconciler, and the bounded HTTP observer;
- an explicitly planning-only web runtime: execution and publication are disabled, its connector
  identity is preflight-only, and it receives no source-execution or DataHub-writer credential;
- explicit 600-second, `schemabridge-secret-manager` audience tokens for web preflight/registry,
  execution/registry, catalog, profile/registry, and reconciliation/registry; API and observer
  receive neither the projected token nor the remote secret ConfigMap;
- exact `secretKeyRef` allowlists for separately provisioned, immutable, version-named runtime
  Secrets; no referenced Secret object or value is defined in this repository;
- no Kubernetes `Secret`, application `Role`, `RoleBinding`, `ClusterRole`, or
  `ClusterRoleBinding`;
- namespace default-deny plus a closed ingress/egress policy for every component identity;
- digest-only images, non-root restricted pod/container settings, memory-backed writable
  directories, probes, graceful drain, requests/limits, topology spread, and disruption budgets;
- TLS-only ingress contracts for web and the API business listener;
- an executable, digest-pinned `schemabridge-observer` Deployment, PDB, Service, and ServiceMonitor
  contract on port `9464`; and
- stable-job ServiceMonitor contracts for the observer plus the internal metrics listeners of API,
  worker, catalog, profile, and reconciler. The API business listener on `8520` does not expose
  `/metrics`; every process exporter is separately bounded on `9464`.

Migration and backup are deliberately represented by identities and network policies, not fake
long-running binaries. Their reviewed one-shot workflows must be supplied and validated by the M29
recovery/release procedure. The observer uses only its read-only control-plane credential, emits
process-local structured logs, and exposes bounded metrics for Prometheus scraping. It receives no
connector-secret configuration, projected workload token, OIDC, LLM, DataHub, source, audit, or
secret-manager capability.

## Required operator values

Before validation, replace every placeholder with:

- the exact registry name and lowercase SHA-256 digest for the single reviewed frozen runtime image
  used by API, background processes, and web;
- one approved HTTPS Vault/OpenBao-compatible endpoint and exact KV-v2 mount;
- four distinct closed connector roles for preflight, execution, catalog, and profile, plus four
  distinct registry-reader roles for web, execution, profile, and reconciliation; each workload
  that has both capabilities must use different roles;
- one opaque registry-reader binding reference and its exact immutable provider version;
- the governed semantic registry ID/scope, approved OIDC issuer/provider, separate web/API
  audiences, approved tenant allowlist, and exact API/web hostnames;
- seven distinct immutable external Secret names following
  `schemabridge-external-<component>-v<positive-version>`;
- the approved trust bundle PEM;
- approved non-wildcard web/API hostnames; and
- the names of two independently managed TLS certificate Secrets.

The namespace Secret quota is exactly `12`: seven immutable component runtime Secrets plus two
managed ingress TLS Secrets form the nine-object steady-state baseline. The remaining three slots
permit one ordered runtime-secret replacement and both TLS certificate replacements to coexist
during rotation. This is capacity headroom only; it does not permit checked-in Secret objects,
in-place mutation, parallel bulk rotation, or sharing one Secret across component identities.

The manifest contains no provider secret path, DSN value, endpoint credential, username, password,
bearer, or OpenAI key. Connector material and the immutable DataHub registry-reader document are
resolved remotely during one operation. The manifest carries only the opaque registry binding
handle plus its exact provider version. Control-plane and signing values are read only from these
exact external references:

| Component | Required keys in its one versioned external Secret |
|---|---|
| web | `control-runtime-dsn`, `control-audit-signing-key`, `identity-migration-key`, `pseudonymization-key`, `query-studio-signing-key`, `streamlit-secrets.toml` |
| API | `control-api-dsn`, `pseudonymization-key`, `inventory-cursor-signing-key` |
| execution worker | `control-worker-dsn` |
| catalog | `control-catalog-dsn` |
| profile worker | `control-worker-dsn` |
| reconciler | `control-reconciler-dsn`, `control-audit-signing-key` |
| observer | `control-observer-dsn` |

The cluster operator must create those Kubernetes Secrets outside this repository through the
approved external-secret process, set `immutable: true`, restrict each external identity to its
single component, record the upstream immutable version and Kubernetes object UID, and patch the
Deployment to a new versioned name for every rotation. Mutating data under an existing name is
forbidden. The validator rejects a cross-component name, an unversioned name, an extra key, a
literal value, or any referenced Secret object checked into the bundle.

The web Secret's `streamlit-secrets.toml` key is projected read-only with mode `0440` at
`/opt/schemabridge/.streamlit/secrets.toml`; the pod `fsGroup` makes it readable only to the
non-root application group. It must contain the closed `[auth]` and provider shape documented in
the runbook. The file is never baked into an image or stored in this repository.

The web ConfigMap must retain `SCHEMABRIDGE_JUDGE_EXECUTION=disabled` and
`SCHEMABRIDGE_PUBLICATION_MODE=disabled`. Query Studio may plan, compile, validate, and preflight
governed requests, but this release does not wire Streamlit to the authenticated execution-job API
and does not implement a durable publication approval queue/publisher worker. The browser must
therefore show both actions as unavailable. Setting either mode to `live`, `recorded`, or `fake`
for the production web runtime is rejected.

Every control DSN must use `sslmode=verify-full` (or the release-approved equivalent accepted by
`Settings`) and identify
`sslrootcert=/var/run/secrets/schemabridge/trust/ca.crt`. All seven pods mount that public trust
bundle read-only. Remote-secret readers reuse the same mounted trust path for the HTTPS resolver.
Web, worker, profile, and reconciler resolve the exact registry-reader document through distinct
provider roles; only the source-reading workloads also receive a separate connector capability.
API and observer keep the trust mount without receiving secret-manager identity.
The observer DSN username must be exactly `schemabridge_observer`.

The observer ConfigMap contains only real `Settings` aliases: its production component/schema
identity, bind and bounded HTTP/metrics settings, log level, and the documented bounded control
pool settings. Its DSN is supplied exclusively by the observer's versioned external
`secretKeyRef`; no credential is present in ConfigMap data.

All six metrics targets — API, worker, catalog, profile, reconciler, and observer — admit
`/metrics` scraping only from one Prometheus pod identity,
`app.kubernetes.io/name=prometheus`, in the `observability` namespace and always on port `9464`.
This gives one operated Prometheus stack access to every signal used by the fail-closed alert
rules. No metrics listener is exposed through an Ingress, and the validator rejects an API scrape
on its public business port `8520`.

NetworkPolicy cannot authorize a dynamic external hostname. The bundle therefore permits outbound
TCP 443 only to pods in a namespace labelled `schemabridge.io/egress-plane=true`, with one exact
`schemabridge.io/egress-capability` label. The target cluster must operate and independently audit
that TLS egress plane. It must also label the ingress, monitoring, DNS, and telemetry workloads
exactly as selected by the policies, or traffic remains denied.

## Validation sequence

Render the production overlay after applying reviewed patches, then run:

```bash
kubectl kustomize deploy/kubernetes/m29/overlays/production > rendered-m29.yaml
python deploy/kubernetes/m29/validate_rendered.py rendered-m29.yaml
kubectl apply --server-side --dry-run=server -f rendered-m29.yaml
```

The local validator accepts only the closed 61-resource contract and emits stable codes without
echoing manifest content. It rejects unresolved placeholders, mutable images, default identities,
automatic or long-lived tokens, wrong audiences, connector identity on API/reconciler, missing or
cross-component external Secret references, embedded Secret values/resources, application
Kubernetes RBAC, unknown workloads/commands, unbounded or cross-capability network peers,
incomplete TLS, missing hardening, resource/PDB drift, invalid metrics
Service/ServiceMonitor/NetworkPolicy selectors, API scraping on `8520`, and observer secret access.
It also rejects a production web ConfigMap that omits or changes either disabled mutation mode.

Server-side dry-run is mandatory because only the target cluster can prove its Kubernetes version,
admission policy, Ingress implementation, `ServiceMonitor` CRD, and namespace selectors. Apply is
still not an operations acceptance: M29 also requires operated secret rotation/revocation,
telemetry/alerts, recovery drill, and rollback evidence.
