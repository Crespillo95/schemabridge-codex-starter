# M23 implementation prompt

Execute `plans/M23_DURABLE_CONTROL_PLANE.md` as one vertical productionization milestone.

Preserve every invariant in `AGENTS.md`. In particular:

- never write to a source database;
- do not use DataHub as the authoritative CAS pointer;
- never auto-run DDL from a web/API process;
- never activate a legacy-shim-only registry version;
- never rewrite historical actors, decisions, recipes, versions, or audit records;
- never persist raw OIDC claims, secrets, SQL, or durable preview rows;
- never repair reconciliation drift without an exact typed approval;
- never let an LLM produce executable SQL.

Write tests before or alongside each behavior. Prove the complete service-backed and internal
browser paths before marking the milestone complete.
