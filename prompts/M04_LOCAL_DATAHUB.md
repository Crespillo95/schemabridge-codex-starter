# Codex milestone M04: Pinned local DataHub Core and MCP environment

Use the `$schemabridge-milestone` skill. Work only on **M04**. Recommended setting: **GPT-5.6 Sol — Extra High (xhigh)**.

## Read first

1. `AGENTS.md` and all nested `AGENTS.md` files governing files you may edit.
2. `docs/00_PRODUCT_VISION.md`
3. `docs/01_SCOPE.md`
4. `docs/02_ARCHITECTURE.md`
5. `docs/06_SECURITY.md`
6. `plans/M04_LOCAL_DATAHUB.md`
7. `tasks/PROJECT_STATE.md`
8. `tasks/DECISION_LOG.md`

## Objective

Bring up a reproducible DataHub Core environment, ingest the synthetic PostgreSQL metadata, enable logical models, and verify MCP read connectivity.

## Implementation contract

- Inspect the current repository and tests before changing anything.
- Start with a concise plan of at most 12 lines and name the exact files or modules you expect to touch.
- Implement the smallest complete vertical slice that satisfies the milestone; do not pull later milestones forward.
- Preserve ports-and-adapters dependency direction and all security invariants.
- Add focused tests before or with behavior. Do not weaken existing tests.
- Run every relevant command and then the full `make check` gate.
- Review the final diff for architecture drift, source writes, unvalidated model output, SQL risk, secrets, proprietary data, and misleading documentation.
- Update durable project state and return the standard handoff.

## Required deliverables

- Pin and document the tested DataHub CLI/Core/MCP versions and resource prerequisites.
- Create repeatable start, health, ingest, reset, and stop scripts or Make targets around the official quickstart.
- Ingest the four demo schemas with descriptions and profiling signals.
- Enable logical-model UI support and document required permissions.
- Create a scoped demo token/service account where supported.
- Enable `.codex/config.toml` DataHub MCP only after a successful tool-list/read check; keep mutations disabled.
- Capture sanitized MCP/schema fixtures for unit tests.

## Acceptance criteria

- [ ] A clean-machine procedure in the runbook starts DataHub and ingests the demo assets.
- [ ] CRM, legacy, bank, and reporting datasets and fields are visible in DataHub.
- [ ] Logical-model UI support is enabled or a verified documented limitation/fallback is recorded.
- [ ] MCP can search the catalog and list schema fields using the scoped identity.
- [ ] MCP mutation tools are unavailable or disabled during this milestone.
- [ ] No DataHub token is committed or printed in logs.

## Expected checks

```bash
make demo-up
<pinned DataHub quickstart command>
<pinned DataHub ingest command>
<MCP connectivity command>
make check
git diff --check
```

If a listed command contains a placeholder, determine and document the verified command rather than pretending the placeholder ran.

## Do not do

- Do not enable write tools.
- Do not implement the application adapter or logical-model mutation yet.

## Operator test to prepare

1. Open DataHub in a browser and locate `crm.customers`, `bank.account_holders`, and their column descriptions.
2. Run one MCP search and one schema-field read from Codex or the MCP inspector.
3. Restart DataHub and prove metadata persists; then document the full reset separately.

Do not commit automatically unless the operator explicitly asks. End using `tasks/HANDOFF_TEMPLATE.md` and include a single proposed commit message.
