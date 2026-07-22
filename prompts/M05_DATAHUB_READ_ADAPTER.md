# Codex milestone M05: DataHub catalog read adapter

Use the `$schemabridge-milestone` skill. Work only on **M05**. Recommended setting: **GPT-5.6 Sol — Extra High (xhigh)**.

## Read first

1. `AGENTS.md` and all nested `AGENTS.md` files governing files you may edit.
2. `docs/00_PRODUCT_VISION.md`
3. `docs/01_SCOPE.md`
4. `docs/02_ARCHITECTURE.md`
5. `docs/06_SECURITY.md`
6. `plans/M05_DATAHUB_READ_ADAPTER.md`
7. `tasks/PROJECT_STATE.md`
8. `tasks/DECISION_LOG.md`

## Objective

Expose DataHub search, asset/schema inspection, lineage, query context, governance metadata, and saved decisions through stable application ports.

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

- Define minimal catalog-read ports in `application/ports` using domain-facing values.
- Implement DataHub MCP/Agent Context adapter methods for asset search, entity details, schema fields, lineage paths, dataset queries/SQL context, and document lookup as available.
- Provide fake and recorded-fixture adapters for unit tests and offline demos.
- Translate missing permissions, unavailable tools, pagination, and partial metadata into typed application errors/results.
- Add integration tests against local DataHub and contract tests shared by fake/real adapters.
- Ensure logs contain URNs/operations but no tokens or unrestricted sample data.

## Acceptance criteria

- [ ] Application/domain code has no MCP client or DataHub SDK model imports.
- [ ] The adapter retrieves the three north-star datasets and required field metadata.
- [ ] Lineage and query-context absence is represented as missing evidence, not an exception or fabricated signal.
- [ ] Pagination and partial response behavior are tested.
- [ ] Fake, recorded, and real adapters satisfy the same contract tests.
- [ ] Integration tests skip with an explicit reason when DataHub credentials are absent.

## Expected checks

```bash
python -m pip install -e ".[dev,datahub]"
pytest tests/unit -k "catalog or datahub"
pytest -m integration -k datahub
make check
git diff --check
```

If a listed command contains a placeholder, determine and document the verified command rather than pretending the placeholder ran.

## Do not do

- Do not score semantic candidates.
- Do not mutate DataHub or create logical models.

## Operator test to prepare

1. Run a CLI catalog inspection for each north-star dataset and compare it with the DataHub UI.
2. Disable DataHub temporarily and confirm the application reports an unavailable-catalog error rather than crashing or using hidden hard-coded context.
3. Run the same inspection with the recorded adapter and confirm it is visibly labeled.

Do not commit automatically unless the operator explicitly asks. End using `tasks/HANDOFF_TEMPLATE.md` and include a single proposed commit message.
