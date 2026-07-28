# API and M28 routed-worker reference runtime

`m24-runtime.yaml` is a production-shaped reference, not a claim that a cluster has been operated.
Build `Dockerfile.runtime`, replace the image placeholder with an immutable registry digest, and
replace every identity/tenant placeholder—including the all-zero reconciler actor—before applying
it. The filename is retained for compatibility; the manifest now includes the additive M25 catalog
and M26–M28 semantic-change and connector-routing processes. It intentionally defines no Kubernetes
`Secret` object.

Provision these ten independent secret inputs before applying the manifest:

- `schemabridge-api-secrets`: `control-api-dsn`, `pseudonymization-key`;
- `schemabridge-worker-secrets`: `control-worker-dsn` only;
- `schemabridge-worker-datahub-reader`: `reader.env`, containing only the read-only DataHub
  immutable-registry endpoint/token configuration;
- `schemabridge-worker-connector-secrets`: execution-only PostgreSQL connector documents;
- `schemabridge-catalog-secrets`: `control-catalog-dsn` only;
- `schemabridge-catalog-connector-secrets`: catalog-only DataHub connector documents;
- `schemabridge-semantic-reconciler-secrets`: `control-reconciler-dsn`,
  `audit-signing-key`;
- `schemabridge-semantic-reconciler-datahub-reader`: `reader.env`, containing only the
  mutation-free immutable-registry reader endpoint/token configuration;
- `schemabridge-semantic-profile-worker-secrets`: `control-worker-dsn` only;
- `schemabridge-semantic-profile-connector-secrets`: profile-only PostgreSQL connector documents.

Each connector-secret data key must be the lowercase SHA-256 of its opaque route binding followed
by `.json`. PostgreSQL execution/profile documents have exactly `format_version`, `dialect`,
`expected_reader`, and `dsn`; `format_version` is `1` and `dialect` is `postgresql`. Catalog
documents have exactly `format_version`, `kind`, `server`, `token`, and `platform`;
`format_version` is `1` and `kind` is `datahub_graphql`. PostgreSQL DSNs must identify the exact
approved read-only role. Catalog tokens must be metadata-read-only. Do not put an opaque binding,
DSN, endpoint, username, password, or token in the manifest, a ConfigMap, a filename other than the
one-way digest, or an operator log.

The connector inputs are deliberately separated by capability. An unprivileged UID-10001 init
container copies each projected `*.json` entry into a capability-specific memory-backed directory,
then verifies a real non-symlink directory owned by UID 10001 with mode `0700` and real files owned
by UID 10001 with mode `0600`. The long-running container receives only that staged parent mount,
read-only. It does not receive the Kubernetes projection. The execution worker uses
`/var/run/schemabridge/connectors/execution`, the catalog indexer uses
`/var/run/schemabridge/connectors/catalog`, and the aggregate profile worker uses
`/var/run/schemabridge/connectors/profile`.

This local owner-only directory is M28 contract evidence, not an operated remote secret-manager
integration. Kubernetes Secret object size, rotation propagation, workload identity, remote
retrieval, audit, alerting, and network policy remain M29 production gates. An environment whose
route set cannot fit this bounded reference must fail deployment rather than merge capabilities or
fall back to a global credential.

The API receives no source, worker, DataHub, OpenAI, migrator, reconciler, or writer credential.
The execution worker receives no global source `DATABASE_URL` and no API, OIDC,
pseudonymization, OpenAI, migrator, reconciler, or writer credential. Its separate DataHub
`reader.env` reads the governed registry only; it is not a catalog route. The catalog indexer
receives neither global `DATAHUB_GMS_URL`/`DATAHUB_GMS_TOKEN` nor a source-database, API, OIDC,
pseudonymization, OpenAI, migrator, reconciler, worker, or DataHub-writer credential. Its pod name
is its unique lease owner, and both replicas claim refreshes through PostgreSQL fencing.

The semantic reconciler receives only its reconciler-role control DSN, audit key, configured opaque
actor/role, and mutation-free DataHub reader file. It receives no source DSN, direct DataHub token,
catalog-route secret, OpenAI key, OIDC/API secret, migrator, or identity-migration key. An
unprivileged init container copies the projected DataHub file into a memory-backed volume and
verifies a regular, non-symlink, UID-10001, mode-0600 file before the reconciler starts. The
aggregate profile worker receives only the worker-role control DSN and its profile-only routed
connector documents; it receives no global source DSN, audit, DataHub, OpenAI, OIDC, API,
publication, or migration credential. It claims target-bearing jobs dynamically rather than being
deployed for one fixed workspace/connection. Both derive their lease owners from the pod name.

Startup and readiness invoke `--probe-ready`, which checks only the exact existing control schema;
neither command migrates, polls a queue, reads DataHub, nor opens the source database. Liveness is
process-only, shutdown has a 180-second grace period, and all long-running containers run as
non-root with dropped capabilities, a read-only root filesystem, and bounded resource requests and
limits. Apply reviewed migrations separately before any deployment starts. Network policy, TLS
ingress, remote secret operation, database pooling, autoscaling, and operated SLOs remain
environment-specific production controls covered by later milestones.
