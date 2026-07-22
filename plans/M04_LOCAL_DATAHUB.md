# M04: Pinned local DataHub Core and MCP environment

- Status: planned
- Timebox: 4 hours
- Recommended Codex: GPT-5.6 Sol — Extra High (xhigh)
- Dependencies: M00, M01

## Objective

Bring up a reproducible DataHub Core environment, ingest the synthetic PostgreSQL metadata, enable logical models, and verify MCP read connectivity.

## Why this milestone exists now

Meaningful DataHub use is the strongest judging priority and must be established before application adapters are written.

## Deliverables

- Pin and document the tested DataHub CLI/Core/MCP versions and resource prerequisites.
- Create repeatable start, health, ingest, reset, and stop scripts or Make targets around the official quickstart.
- Ingest the four demo schemas with descriptions and profiling signals.
- Enable logical-model UI support and document required permissions.
- Create a scoped demo token/service account where supported.
- Enable `.codex/config.toml` DataHub MCP only after a successful tool-list/read check; keep mutations disabled.
- Capture sanitized MCP/schema fixtures for unit tests.

## Implementation sequence

1. Verify current official quickstart and compatibility instead of relying on the starter placeholders.
2. Install a pinned CLI/tooling set in the project environment or isolated tool environment.
3. Start DataHub Core, record actual URLs/health checks, and enable logical models.
4. Run the PostgreSQL ingestion recipe and verify datasets/fields/descriptions/profiles in the UI.
5. Configure the self-hosted MCP server with environment-based credentials and list/read tools.
6. Record exact troubleshooting and clean-reset steps.
7. Export sanitized read responses needed by M05 tests.

## Acceptance criteria

- [ ] A clean-machine procedure in the runbook starts DataHub and ingests the demo assets.
- [ ] CRM, legacy, bank, and reporting datasets and fields are visible in DataHub.
- [ ] Logical-model UI support is enabled or a verified documented limitation/fallback is recorded.
- [ ] MCP can search the catalog and list schema fields using the scoped identity.
- [ ] MCP mutation tools are unavailable or disabled during this milestone.
- [ ] No DataHub token is committed or printed in logs.

## Required automated checks

```bash
make demo-up
<pinned DataHub quickstart command>
<pinned DataHub ingest command>
<MCP connectivity command>
make check
git diff --check
```

Replace angle-bracket placeholders only when the actual environment has established the command. Never report an unexecuted placeholder as passing.

## Manual test for the operator

1. Open DataHub in a browser and locate `crm.customers`, `bank.account_holders`, and their column descriptions.
2. Run one MCP search and one schema-field read from Codex or the MCP inspector.
3. Restart DataHub and prove metadata persists; then document the full reset separately.

## Explicit non-goals

- Do not enable write tools.
- Do not implement the application adapter or logical-model mutation yet.

## Handoff requirements

Update `tasks/PROJECT_STATE.md`, `tasks/CURRENT_TASK.md`, and `tasks/DECISION_LOG.md`. Return the format in `tasks/HANDOFF_TEMPLATE.md`, including exact commands, results, limitations, and the proposed commit message.
