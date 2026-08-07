# Codex milestone M20: Production identity, RBAC, and workflow isolation

Use the `$schemabridge-milestone` skill. Work only on **M20**.

Read the repository instructions, product vision, scope, architecture, security model,
`plans/M20_PRODUCTION_IDENTITY_RBAC.md`, project state, and decision log before editing.

Implement provider-neutral Streamlit OIDC authentication, development-only local identity, closed
RBAC, and durable tenant/owner workflow isolation. Browser users must never type or override their
audit actor. OIDC tokens, email, and raw claims must not be persisted or logged.

Run the focused tests, `make check`, service-backed integration/acceptance, full coverage, and the
internal-browser manual path. Preserve all source-write, SQL, LLM, fanout, approval, and DataHub
mutation invariants. Do not claim M18 acceptance or commit automatically.
