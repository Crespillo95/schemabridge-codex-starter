# M20: Production identity, RBAC, and workflow isolation

- Status: complete locally; operator acceptance/commit pending
- Timebox: 6 hours
- Recommended Codex: GPT-5.6 Sol — Extra High (xhigh)
- Dependencies: M12/M14 workflow and UI contracts; M16 automated hardening evidence

## Objective

Replace caller-supplied browser actor strings with authenticated principals, closed authorization
roles, and durable tenant/owner workflow access so governance decisions identify a verified subject
and one browser session cannot operate another tenant's workflow.

M18's external submission evidence remains pending. Starting this explicitly requested production
track does not accept M18 or turn its dirty working tree into release evidence.

## Deliverables

- Add immutable authenticated-principal, role, authentication-method, and workflow-access values.
- Resolve local demo identity only outside production; resolve production identity from validated
  Streamlit OIDC claims without persisting tokens, email, or raw claims in the SchemaBridge control
  plane or logs (Streamlit retains its documented browser identity cookie).
- Enforce a closed `analyst`/`steward`/`publisher`/`auditor`/`platform_admin` policy at the
  application boundary.
- Reserve context publication for `publisher`/`platform_admin`; bind all workflow decisions to the
  authenticated principal rather than browser input.
- Persist the first workflow draft and its tenant/owner grant atomically in SQLite, with conflict
  detection, restart durability, and tenant isolation.
- Render authentication mode and effective roles without exposing tokens or raw identity claims.
- Document provider-neutral OIDC configuration and the required tenant/role claims.
- Make catalog, execution, and publication modes immutable deployment configuration in
  `staging`/`production`; the public judge profile remains anonymous but recorded/fake only.
- Default development execution to recorded and require an explicit development-only flag for any
  local-demo live catalog/query read.
- Fail closed for mutating CLI commands in `staging`/`production` until that entrypoint can receive
  a verified non-browser principal.

## Implementation sequence

1. Define pure identity/access invariants and the application authorization matrix.
2. Add a workflow-access port plus in-memory and SQLite adapters.
3. Add strict OIDC claim mapping and development-only local identity composition.
4. Bind the Streamlit service to one principal and remove the free-text actor control.
5. Add unit, storage, AppTest, acceptance, and browser regressions.
6. Update security, architecture, deployment, state, and decisions with exact evidence.

## Acceptance criteria

- [x] `production` and `staging` fail closed unless OIDC mode is configured.
- [x] Missing, expired, future, wrong-issuer, wrong-audience, malformed-role, missing-tenant, or
      validly signed but unallowlisted-tenant claims cannot create a principal.
- [x] Audit actor IDs are stable, deployment-scoped HMAC pseudonyms derived from issuer/subject
      with a length-and-diversity-validated secret, never typed by a browser user, and no
      token/email/raw claims enter application persistence.
- [x] Analysts may create, confirm, execute, skip, and retry only their own tenant workflows.
- [x] Only publishers/platform admins may publish reusable context; unknown roles grant nothing.
- [x] Steward/publisher/auditor workflow views omit result rows and rejection details; export stays
      analyst/platform-admin only.
- [x] A principal from another tenant cannot inspect or act on a known workflow ID.
- [x] Workflow ownership survives a new process and conflicting ownership fails closed.
- [x] Local/demo browser behavior remains explicit, key-free, and fully governed; no local identity
      may select a live publication adapter.
- [x] Internal-browser acceptance covers authenticated identity visibility, absence of actor input,
      the north-star result, publication authorization, responsive layout, and console errors.

## Required automated checks

```bash
pytest tests/unit/test_identity.py tests/unit/test_workflow_access.py \
  tests/unit/test_ui_view_models.py tests/unit/test_ui_components.py
pytest -m acceptance -k streamlit
make check
make test-integration
make test-acceptance
make coverage
git diff --check
```

Verified on 2026-07-23 with Streamlit 1.60.0:

- `make check`: 472 passed; Ruff and strict mypy passed.
- focused M20 security/RBAC cut: 149 passed.
- `make test-integration`: 29 passed.
- `make test-acceptance`: 13 passed.
- `make coverage`: 514 passed, 80.07%; seven expected DataHub idempotent-upsert warnings.
- `git diff --check`: passed.
- internal browser: local and native OIDC analyst/publisher/tenant/unknown-role journeys passed;
  clean stable warning/error log was empty and 756x924 had no horizontal overflow.

## Manual test for the operator

1. Start the local demo identity path and complete the governed north-star workflow.
2. Confirm the sidebar shows the authenticated mode/roles and exposes no editable actor.
3. Confirm an auditor cannot start, an analyst cannot publish, a distinct publisher can publish,
   and a cross-tenant principal receives one generic access-denied state.
4. Start OIDC mode without a session and confirm only the sign-in boundary is rendered.

## Explicit non-goals

- Hosting or administering the external identity provider.
- User invitation, billing, SCIM, or a general organization administration console.
- Delegated OAuth access tokens or acting on behalf of a user in DataHub.
- API gateway authentication; a later API/workers milestone will reuse the same principal/policy.

## Handoff requirements

Update `tasks/PROJECT_STATE.md`, `tasks/CURRENT_TASK.md`, `tasks/DECISION_LOG.md`, and the relevant
ADR. Return `tasks/HANDOFF_TEMPLATE.md` with exact commands, browser evidence, limitations, and the
single proposed commit message.
