# Official references to verify during development

Last M18 verification: 2026-07-22. Streamlit authentication references rechecked for M20 on
2026-07-23. DataHub GraphQL scroll references rechecked for M25 on 2026-07-23.

These URLs are included for Codex/operator research. Product code must not depend on documentation text or assume that unpinned “latest” behavior remains stable.

## Hackathon

- https://datahub.devpost.com/
- https://datahub.devpost.com/rules

Current official rules verified for the 2026 event:

- Submission deadline: August 10, 2026 at 5:00 pm EDT; judging runs August 17–31.
- The application must use DataHub open source plus at least one of MCP Server, Agent Context Kit,
  DataHub Skills, or Analytics Agent. Agents That Do Real Work read context, act, and write results
  back so later people/agents inherit knowledge.
- Judges need an easy project URL and a public repository with complete source/instructions. The
  repository must be Apache-2.0 and the license should be visible/detected at the top of its page.
- The public demonstration video must be below three minutes, show the project functioning, be
  public on YouTube/Vimeo/Youku, and avoid unlicensed music/material or unauthorized trademarks.
- Testing must remain free and unrestricted through the judging period. Judges may evaluate only
  the text, images, and video. All materials must be English or have complete English translation.
- Equally weighted criteria are Use of DataHub, Technical Execution, Originality, Real-World
  Usefulness, and Submission Quality; a meaningful open-source contribution is an optional bonus.

## DataHub

- MCP Server: https://docs.datahub.com/docs/features/feature-guides/mcp
- Agent Context Kit: https://docs.datahub.com/docs/dev-guides/agent-context/agent-context
- DataHub Skills: https://docs.datahub.com/docs/dev-guides/agent-context/skills
- Logical Models: https://docs.datahub.com/docs/features/feature-guides/logical-models/overview
- Analytics Agent: https://docs.datahub.com/docs/features/feature-guides/analytics-agent
- Quickstart: https://docs.datahub.com/docs/quickstart
- Service accounts: https://docs.datahub.com/docs/features/feature-guides/service-accounts
- Personal access tokens: https://docs.datahub.com/docs/authentication/personal-access-tokens
- GraphQL queries (`scrollAcrossEntities`):
  https://docs.datahub.com/docs/graphql/queries
- GraphQL input objects (`ScrollAcrossEntitiesInput`):
  https://docs.datahub.com/docs/graphql/inputObjects
- GraphQL deep-pagination guidance:
  https://docs.datahub.com/docs/api/graphql/graphql-best-practices
- Search at scale:
  https://docs.datahub.com/docs/how/search
- Core releases: https://github.com/datahub-project/datahub/releases
- MCP server releases: https://github.com/acryldata/mcp-server-datahub/releases

The current DataHub guidance recommends the `scroll*` APIs for deep pagination rather than
`search*`; `scrollAcrossEntities` exposes an opaque `scrollId`. M25 additionally binds its own
interactive PostgreSQL cursor to tenant, scope, filter, and generation instead of exposing the
DataHub scroll pointer to clients.

## Codex

- Models: https://developers.openai.com/codex/models
- Config: https://developers.openai.com/codex/config-basic
- Config reference: https://developers.openai.com/codex/config-reference
- AGENTS.md: https://developers.openai.com/codex/agent-configuration/agents-md
- Skills: https://developers.openai.com/codex/build-skills
- Subagents: https://developers.openai.com/codex/subagents

## Streamlit authentication

- OIDC authentication concepts and secret shape:
  https://docs.streamlit.io/develop/concepts/connections/authentication
- `st.login`: https://docs.streamlit.io/develop/api-reference/user/st.login
- `st.user`: https://docs.streamlit.io/develop/api-reference/user/st.user
- `st.logout`: https://docs.streamlit.io/develop/api-reference/user/st.logout

Streamlit authenticates with OIDC but does not define application authorization. Its browser
identity cookie has a documented lifetime independent of the ID-token expiry, so SchemaBridge
revalidates token claims and owns its RBAC/session-age policy.

M04, M17, and M18 must browse the current official sources and update any changed commands, versions, hosting facts, or submission requirements.
