# M05: DataHub catalog read adapter

- Status: planned
- Timebox: 3 hours
- Recommended Codex: GPT-5.6 Sol — Extra High (xhigh)
- Dependencies: M02, M04

## Objective

Expose DataHub search, asset/schema inspection, lineage, query context, governance metadata, and saved decisions through stable application ports.

## Why this milestone exists now

Matching and join discovery must use real catalog context without leaking MCP or SDK payloads into the domain.

## Deliverables

- Define minimal catalog-read ports in `application/ports` using domain-facing values.
- Implement DataHub MCP/Agent Context adapter methods for asset search, entity details, schema fields, lineage paths, dataset queries/SQL context, and document lookup as available.
- Provide fake and recorded-fixture adapters for unit tests and offline demos.
- Translate missing permissions, unavailable tools, pagination, and partial metadata into typed application errors/results.
- Add integration tests against local DataHub and contract tests shared by fake/real adapters.
- Ensure logs contain URNs/operations but no tokens or unrestricted sample data.

## Implementation sequence

1. Design the smallest port needed by M06/M08; avoid mirroring every DataHub API method.
2. Inspect sanitized M04 responses and define translation functions.
3. Implement read operations with pagination and bounded requests.
4. Implement fake and recorded adapters with equivalent contracts.
5. Add contract tests and one local integration path per major capability.
6. Document unsupported features and version-specific behavior.

## Acceptance criteria

- [ ] Application/domain code has no MCP client or DataHub SDK model imports.
- [ ] The adapter retrieves the three north-star datasets and required field metadata.
- [ ] Lineage and query-context absence is represented as missing evidence, not an exception or fabricated signal.
- [ ] Pagination and partial response behavior are tested.
- [ ] Fake, recorded, and real adapters satisfy the same contract tests.
- [ ] Integration tests skip with an explicit reason when DataHub credentials are absent.

## Required automated checks

```bash
python -m pip install -e ".[dev,datahub]"
pytest tests/unit -k "catalog or datahub"
pytest -m integration -k datahub
make check
git diff --check
```

Replace angle-bracket placeholders only when the actual environment has established the command. Never report an unexecuted placeholder as passing.

## Manual test for the operator

1. Run a CLI catalog inspection for each north-star dataset and compare it with the DataHub UI.
2. Disable DataHub temporarily and confirm the application reports an unavailable-catalog error rather than crashing or using hidden hard-coded context.
3. Run the same inspection with the recorded adapter and confirm it is visibly labeled.

## Explicit non-goals

- Do not score semantic candidates.
- Do not mutate DataHub or create logical models.

## Handoff requirements

Update `tasks/PROJECT_STATE.md`, `tasks/CURRENT_TASK.md`, and `tasks/DECISION_LOG.md`. Return the format in `tasks/HANDOFF_TEMPLATE.md`, including exact commands, results, limitations, and the proposed commit message.
