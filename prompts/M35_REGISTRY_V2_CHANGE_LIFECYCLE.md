# M35 implementation prompt — governed registry-v2 change lifecycle

Implement `plans/M35_REGISTRY_V2_CHANGE_LIFECYCLE.md` as one security-first milestone.

Start with the smallest complete vertical: one newly approved same-connection join between two
models already present in an exact active registry-v2. Reuse the M26 aggregate-only relationship
evidence and the M34 publication worker; do not call a source writer, let an LLM approve semantics,
place DataHub credentials in web/API, mutate an existing registry document or auto-activate.

Keep existing M33 proposal fingerprints and persisted payloads compatible. Any storage evolution
must be an additive v15 migration with explicit kind checks, append-only decisions, tenant
isolation, optimistic concurrency, exact idempotency and least privilege. A new join must preserve
the base byte-for-byte in meaning and add exactly one approved logical summary, physical contract
and provenance decision.

After the join vertical passes, add one-model replacement/remediation bound to a complete current
M26 report and incident-join accounting. Do not claim M35 complete until both phases, PostgreSQL,
acceptance, browser, M23/M26 regressions, full `make check`, state documents and handoff pass.
