# M33 implementation prompt — Generic governed semantic onboarding

Implement `plans/M33_GENERIC_SEMANTIC_ONBOARDING.md` as one vertical slice.

Start from an authenticated tenant and an exact retained dynamic-catalog generation. Build a new
tenant-scoped onboarding domain and durable PostgreSQL workflow; do not expose the unauthenticated
legacy canonical-review entrypoints as the commercial API. Confidence, name equality, or model
output must never approve a semantic fact.

Persist full physical authority: workspace, catalog scope, connection, generation/vector, asset and
field locators/fingerprints, normalized physical type, compiler field ref, and only an exactly
observed DataHub asset URN. Revalidate before decisions and preparation. Actor/workspace always
come from `AuthenticatedPrincipal`.

M33 must stop at an immutable, audited `ready_for_publication` proposal. Do not place a DataHub
writer credential in web/API composition, derive URNs from names, publish, activate, execute SQL,
or fall back to recorded/demo context. Those actions belong to later gated milestones.

Use test-first development, preserve every repository invariant, run the focused matrix and
`make check`, exercise the final bytes in the internal browser, and finish the milestone handoff.
