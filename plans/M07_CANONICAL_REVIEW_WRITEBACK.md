# M07: Canonical review, logical models, and approved write-back

- Status: planned
- Timebox: 3 hours
- Recommended Codex: GPT-5.6 Sol — Extra High (xhigh)
- Dependencies: M04, M05, M06

## Objective

Turn semantic candidates into explicitly reviewed canonical models and persist approved knowledge in DataHub.

## Why this milestone exists now

The category expects an agent that acts and writes results back so later users and agents inherit the knowledge.

## Deliverables

- Implement review use cases for edit, approve, reject, and mark-as-different-concept actions.
- Implement a draft store and immutable/versioned decision records.
- Define a catalog-write port requiring an explicit approval object.
- Implement DataHub write-back for the supported combination of logical models/links, glossary terms, descriptions, structured properties, and decision documents.
- Use proposal workflows where practical; document a faithful fallback when a native logical-field operation is unavailable.
- Add idempotency and partial-failure handling plus integration tests.

## Implementation sequence

1. Design approval and version transitions in the domain.
2. Implement in-memory/SQLite draft storage and fake writer first.
3. Enable DataHub MCP mutation tools only in a controlled local configuration and retain write approvals.
4. Implement the minimal real writer with idempotent identifiers and explicit target summaries.
5. Publish `Customer` and its key/date mappings, then retrieve and compare the result.
6. Record exact before/after artifacts for the demo.

## Acceptance criteria

- [ ] No DataHub write can occur without an explicit approval value passed by the use case.
- [ ] Approving a candidate creates a versioned decision; rejecting it never publishes approved metadata.
- [ ] Replaying the same approved write is idempotent or yields a typed already-current result.
- [ ] Partial failures are visible and recoverable without falsely marking the whole decision published.
- [ ] Customer canonical context is visible in DataHub and retrievable through the read adapter.
- [ ] Every published item records or links its SchemaBridge decision/version.

## Required automated checks

```bash
pytest tests/unit -k "approval or decision or publish"
pytest -m integration -k "datahub and write"
make check
git diff --check
```

Replace angle-bracket placeholders only when the actual environment has established the command. Never report an unexecuted placeholder as passing.

## Manual test for the operator

1. Approve the three Customer key mappings and registration-date mapping in the current review interface/CLI.
2. Inspect DataHub before and after, including descriptions/properties/document and logical-model linkage or documented fallback.
3. Rerun publication and confirm no duplicate terms/documents are created.

## Explicit non-goals

- Do not discover joins.
- Do not allow natural-language input to trigger writes.

## Handoff requirements

Update `tasks/PROJECT_STATE.md`, `tasks/CURRENT_TASK.md`, and `tasks/DECISION_LOG.md`. Return the format in `tasks/HANDOFF_TEMPLATE.md`, including exact commands, results, limitations, and the proposed commit message.
