# M28 implementation prompt

Implement `plans/M28_COMPILER_CONNECTORS_COST_CONTROLS.md` as one governed vertical slice.

Preserve every repository invariant. In particular:

- keep `QueryPlan` vendor-neutral and credential-free;
- derive the sole execution connection from the current M26 semantic-evidence gate, never from
  browser state, name similarity, an LLM, or a global DSN;
- bind workspace, connection, PostgreSQL dialect, immutable route revision/fingerprint, expected
  reader, type contract, and cost budget into the approved resolved-plan fingerprint;
- keep PostgreSQL 16 as the only executable dialect and reject every unsupported connector/dialect
  before compiler, route, `EXPLAIN`, or source I/O;
- create an additive schema-v9 versioned route contract with CAS activation/rotation/disable,
  immutable audit, lease/fence-bound route reads, and no credential material;
- persist only opaque private secret-binding references; resolve them inside concrete adapters
  immediately before use;
- use an owner-only bounded local secret adapter for integration evidence and leave operated remote
  secret management to M29;
- never expose a binding reference, file path, endpoint, DSN, token, password, raw plan, SQL,
  parameter, source value, result row, prompt, claim, or private identity in public models, jobs,
  responses, logs, screenshots, or fingerprints;
- make compiler and independent guard carry and verify the exact same dialect and target
  fingerprint;
- run only bounded read-only PostgreSQL `EXPLAIN (FORMAT JSON, ANALYZE FALSE, BUFFERS FALSE,
  VERBOSE FALSE, SETTINGS FALSE)` after AST validation and before execution approval;
- repeat route, semantic, and cost validation immediately before preview and use the same target
  for rejected-source inspection;
- keep one connection, three tables, two joins, allowlists, fanout, row limit, timeout, expected
  reader, read-only transaction, fetched-row cap, and rollback unchanged;
- route the execution worker, profile worker, and catalog indexer dynamically by exact
  workspace/connection and isolate their capabilities;
- populate reserved platform/database/normalized-type metadata only through deterministic
  connector contracts; unknown types remain explicit and non-executable;
- prove two tenants with identical connection/schema/table/field names reach distinct synthetic
  databases, roles, routes, and results without crossing;
- preserve migrations 0001–0008 byte-for-byte and fail closed on legacy non-terminal jobs that
  cannot be assigned a target without guessing;
- write tests alongside behavior and run every command in the M28 plan;
- complete final desktop/mobile acceptance only in Codex's internal browser; do not bypass a URL
  policy block or present it as product success;
- keep M29 operations/supply-chain, M30 production security/evaluation, and M31 pilot/GA work out
  of this milestone;
- do not claim production or release readiness from local synthetic evidence.

Record every command, failure, correction, migration checksum, role/route proof, cost-budget fact,
two-tenant result, browser observation, limitation, and exact final result in
`tasks/M28_HANDOFF.md`.
