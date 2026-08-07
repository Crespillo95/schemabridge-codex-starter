# M22 implementation prompt

Use `$schemabridge-milestone` and execute
`plans/M22_COMPLETE_LIVE_DATAHUB_CONTEXT.md` end to end.

Preserve all SchemaBridge invariants. In particular, a live DataHub registry must never fall back
to the recorded manifest, publication requires an exact typed approval plus audit record, MCP stays
read-only, the source database stays read-only, and no raw LLM SQL may reach execution.

Finish with the complete automated/service/browser evidence and the exact handoff template.
