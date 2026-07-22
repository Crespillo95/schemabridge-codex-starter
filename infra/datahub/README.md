# Local DataHub Core and MCP

M04 uses the official DataHub quickstart with a checksum-pinned Compose source. The repository
enables authentication and logical models, binds every published port to loopback, and keeps all
credentials under ignored `.local/datahub/` files with mode `0600`.

## Version contract

| Component | Pin |
|---|---|
| DataHub CLI and PostgreSQL connector | `acryl-datahub[postgres]==1.6.0.15` |
| DataHub Core images / quickstart plan | `v1.6.0` |
| DataHub MCP server | `mcp-server-datahub==0.6.0` |
| Isolated tool runner | `uv==0.11.30`, CPython 3.11 |

The public pins, official Compose URL, and reviewed SHA-256 are in `versions.env`. The connector
and MCP server run in isolated uv environments so DataHub's SQLGlot constraint cannot change the
M03 compiler environment.

## Clean start and ingestion

Prerequisites are Docker Compose v2, Python 3.11–3.13 for SchemaBridge, and the project datahub
extra. DataHub's official quickstart guidance calls for at least 2 CPUs, 8 GB RAM, 2 GB swap, and
13 GB free disk; reserve more disk for cached images and repeated runs.

```bash
.venv/bin/python -m pip install -e ".[dev,postgres,sql,datahub]"
make demo-up
make datahub-start
make datahub-health
make datahub-init-admin
make datahub-ingest
make datahub-provision-mcp
make datahub-catalog-check
make datahub-mcp-check
```

The UI is `http://127.0.0.1:9002` and GMS is `http://127.0.0.1:8080`. The local quickstart login is
`datahub` / `datahub`; do not reuse that credential outside this loopback development environment.

`make datahub-init-admin` stores an admin token in DataHub CLI's standard `~/.datahubenv` file.
`make datahub-provision-mcp` creates or reuses the dedicated **SchemaBridge MCP Reader** service
account and writes its one-month token to ignored `.local/datahub/mcp.env`. Neither command prints
the token. Provisioning also fails if the token actor differs or any checked administrative/write
platform privilege is granted. The ingestion wrapper removes DataHub's partial token representation
from output.

## Read-only MCP and logical models

`.codex/config.toml` invokes `scripts/datahub-mcp.sh`, which pins MCP 0.6.0 and unconditionally sets:

```text
TOOLS_IS_MUTATION_ENABLED=false
SAVE_DOCUMENT_TOOL_ENABLED=false
DATAHUB_MCP_DOCUMENT_TOOLS_DISABLED=true
```

The connectivity check requires `search` and `list_schema_fields` and fails if known mutation tools
appear. Codex may need a project reload after trust is granted before it recognizes the enabled
server.

GMS runs with `LOGICAL_MODELS_ENABLED=true`; `make datahub-catalog-check` also verifies that the UI
feature flag reports `logicalModelsEnabled: true`. The approval-gated writer now publishes the
canonical logical model, terms, decision document, and physical links through bounded SDK/aspect
operations. The tested Core version lacks the expected batch logical-relationship route, so the
writer uses DataHub's `LogicalParent` aspect path and records that fallback explicitly. The writer
identity needs **Create Logical Models** plus bounded **Edit** permission on the linked synthetic
assets; MCP mutations remain disabled.

## Lifecycle

```bash
make datahub-restart  # preserve metadata, signing keys, and service-account token
make datahub-stop     # preserve metadata
make datahub-reset    # delete only the `datahub` Compose project volumes, then restart empty
```

After `make datahub-reset`, repeat `datahub-init-admin`, `datahub-ingest`,
`datahub-provision-mcp`, and both checks. Local signing material persists in ignored
`.local/datahub/quickstart.env`; delete that file only when deliberately rotating all local tokens.

## Troubleshooting

- A `401 Unauthorized` during ingestion means `make datahub-init-admin` has not been run for the
  current reset/signing keys. No records are written; initialize the CLI credential and retry.
- An MCP `401` after a reset means the service-account token refers to deleted state. Rerun
  `make datahub-provision-mcp`.
- If startup stalls or Docker reports disk pressure, check `docker system df` and `df -h .`. Do not
  prune images, volumes, or build cache without reviewing unrelated Docker projects.
- The MCP server currently emits an upstream experimental-SDK warning on startup; the pinned read
  tools still pass. Mutations remain disabled.
