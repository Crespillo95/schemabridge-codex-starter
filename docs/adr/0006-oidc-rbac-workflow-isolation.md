# ADR 0006: Provider-neutral OIDC, closed RBAC, and workflow isolation

- Status: accepted for M20
- Date: 2026-07-23

## Context

The hackathon MVP intentionally excluded enterprise authentication. Its Streamlit page and CLI
therefore accepted an arbitrary `actor` string while the server held database and DataHub
credentials. Fingerprints proved that an approval matched a payload, but did not prove who approved
it. Workflow drafts were also loaded by a global ID without tenant or owner scope.

That design is not acceptable for a production deployment: it permits actor spoofing, horizontal
access, and a confused-deputy path to a live publisher.

Streamlit provides provider-neutral OpenID Connect through `st.login`, `st.user`, and `st.logout`.
Its documentation states that OIDC authenticates but does not authorize, requires the Authlib
extra, and uses an identity cookie whose 30-day lifetime is not automatically tied to the ID-token
expiry. SchemaBridge therefore needs a separate application policy and must revalidate token times
on every rerun:

- <https://docs.streamlit.io/develop/concepts/connections/authentication>
- <https://docs.streamlit.io/develop/api-reference/user/st.login>

## Decision

1. `st.login` is the production browser authentication transport. Streamlit verifies the provider
   flow and ID token; SchemaBridge then validates the surfaced `iss`, `sub`, `aud`, `azp`, `iat`,
   `nbf`, `exp`, tenant, and group claims against server configuration. The tenant must match one
   exact value in the deployment's explicit tenant allowlist.
2. SchemaBridge derives deployment-scoped, version-prefixed actor and workspace IDs with
   HMAC-SHA-256 and a secret key of at least 32 UTF-8 bytes and eight distinct byte values. Email,
   name, tokens, and raw claims never
   enter domain objects, workflow persistence, application audit records, or logs. Streamlit still
   keeps its authentication material in its own browser identity cookie; this is outside
   the SchemaBridge control plane and is not represented as “no browser persistence.”
3. Authorization is deny-by-default and uses only five closed roles:

   | Role | Workflow permissions |
   |---|---|
   | `analyst` | create/view own, confirm, execute, skip, retry, view/export bounded result |
   | `steward` | view/confirm within the workspace |
   | `publisher` | view/publish within the workspace |
   | `auditor` | view within the workspace |
   | `platform_admin` | all currently defined permissions within its workspace |

   Unknown provider groups grant no permission and receive a 403 landing without governed
   reference data. Cross-workspace access is always denied. Result rows and rejection details are
   removed at the application boundary for steward, publisher, and auditor views; publishing does
   not imply result export.
4. Initial workflow persistence and its immutable `WorkflowAccessGrant` occur in one SQLite
   `BEGIN IMMEDIATE` transaction. The additive table binds a globally unique workflow ID to exactly
   one workspace and owner. An existing draft, an orphan reservation, or a conflicting owner fails
   closed. Existing workflows without a grant are `legacy_unverified` in effect and cannot be
   reached through the authenticated UI.
5. Browser decisions derive their actor exclusively from the authenticated principal. The actor
   field and client-selectable integration modes are removed. A full 128-bit random workflow ID is
   used.
6. `hosted-demo` is the only anonymous profile and is fixed to
   `local-demo/recorded/fake/recorded`. `staging` and `production` require OIDC. A local identity can
   never compose live publication. Managed deployment modes are server configuration, not browser
   controls.
7. Live publication requires a publisher different from the execution approver and an identity
   token issued within the previous 15 minutes. Publication retry rechecks freshness, current
   publisher permission, and the same principal that owns the stored approval.
8. The legacy CLI has no verified principal transport, so it is disabled completely in staging and
   production. A later authenticated API/worker milestone may expose the same policy through a
   service identity.
9. The local SQLite control-plane file is forced to owner-only mode `0600`.
10. Recorded execution is the development default. Anonymous local live catalog/query reads require
    the explicit `SCHEMABRIDGE_ALLOW_LOCAL_LIVE_READS=true` development-only gate; live
    publication still requires OIDC.
11. Before `st.user` is read, managed startup validates the Streamlit `[auth]` and named provider
    sections, HTTPS redirect and discovery URLs, issuer/discovery origin, client ID/audience,
    independent non-placeholder client/cookie secrets, callback path, and disabled token exposure.
12. Workflow inspection and the compatibility `resume` operation are read-only. Interrupted-state
    repair is a separate explicit action authorized against the exact pending operation before any
    revision is persisted.

## Secrets boundary

OIDC client ID, client secret, cookie secret, redirect URI, and provider metadata URL live in an
untracked or mounted `.streamlit/secrets.toml`. `expose_tokens` remains unset. Claim/policy metadata
uses environment settings, including the explicit tenant allowlist; the HMAC pseudonymization key
is separately injected from the deployment secret manager as
`SCHEMABRIDGE_PSEUDONYMIZATION_KEY`. The populated secrets and HMAC key must be shared securely
across replicas and never committed.

## Alternatives rejected

- **Editable actor plus fingerprint:** payload integrity is not authentication.
- **Trust `X-Forwarded-User` or email:** unsigned headers and mutable PII are not stable identity.
- **OIDC groups used directly in UI code:** authentication claims still require a closed,
  application-owned authorization policy.
- **Protect only Streamlit:** alternate production entrypoints would retain the same confused-deputy
  bypass, so the legacy CLI fails closed.
- **Turn legacy actor strings into principals:** historical labels are not verified identities.

## Consequences and residual work

The public judge path stays key-free and anonymous, while a production browser stops at OIDC before
loading governed data. Ownership is durable across process restart and access denial does not reveal
whether a workflow exists in another workspace.

Streamlit's signed identity cookie may outlive the ID token. SchemaBridge therefore rejects an
expired/future token on every rerun and limits accepted token lifetime to one hour. Group removal is
not immediate for an already issued token: general authorization changes take effect after a new
token or no later than expiry; live publication has the tighter 15-minute freshness window.
`st.logout` clears the current Streamlit session; operators must use provider-side revocation for
other active sessions.

Changing the HMAC key or version deliberately produces new actor/workspace IDs. Until M23 supplies a
reviewed identity-migration tool, an emergency rotation safely quarantines existing grants rather
than silently reassigning them; preserve the old key and database under incident controls if
forensic access is required.

SQLite remains a single-node development/demo control plane. M23 must introduce versioned
migrations, a production PostgreSQL control plane, outbox/reconciliation, backup/restore, and
tamper-evident audit controls plus authenticated identity provenance/policy versions. Identity-
provider administration, SCIM, invitations, and revocation of already issued tokens remain external
operational responsibilities.

The tenant allowlist and workflow grants isolate the control plane only. Live DataHub/PostgreSQL
adapters still use deployment-wide credentials, so one deployment may serve only tenants
authorized for the same external asset scope. A genuinely heterogeneous multi-tenant deployment
requires tenant-aware data-plane routing/credentials before production. M20 also has no
per-principal rate, quota, or concurrency limit; M24/M25 add those controls and measured local
regression budgets without claiming production SLOs.
