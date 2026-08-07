# M26 implementation prompt

Implement `plans/M26_JOIN_DRIFT_CHANGE_MANAGEMENT.md` as one vertical slice.

Preserve every repository invariant. In particular:

- bind governed physical fields explicitly to tenant/connection/asset/field inventory identities;
- never infer semantic equivalence or replacement from names, definitions, tags, or an LLM;
- compare only governed resources, not every pair in a 5,434+ table catalog;
- require an explicit first baseline and exact approval for compatible revalidation;
- make type/key/nullability/removal/ambiguity changes non-waivable for the same registry version;
- use a corrected immutable registry plus M23 activation for blocking remediation;
- compute a complete, fingerprinted, paginated mapping/join/workflow/recipe blast radius;
- fail closed before compiler or source I/O for affected plans while allowing unaffected plans;
- make catalog/registry change scans durable, leased, fenced, idempotent, and crash-resumable;
- keep HTTP read-only and mutation in a separate trusted operator flow;
- persist no raw values, samples, rows, SQL, parameters, prompts, claims, DSNs, tokens, or keys;
- do not call OpenAI or implement M27 Query Studio/description matching;
- run real PostgreSQL/DataHub, process, package, security, and internal-browser acceptance;
- keep local evidence distinct from production/release approval.

Write tests alongside behavior. Do not weaken existing assertions, edit migrations 0001–0004,
silently auto-establish a baseline, or claim production/release readiness. Record every command
and exact result in the M26 handoff.
