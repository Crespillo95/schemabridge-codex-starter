# Codex milestone M07: Canonical review, logical models, and approved write-back

Use the `$schemabridge-milestone` skill. Work only on **M07**. Recommended setting: **GPT-5.6 Sol — Extra High (xhigh)**.

## Read first

1. `AGENTS.md` and all nested `AGENTS.md` files governing files you may edit.
2. `docs/00_PRODUCT_VISION.md`
3. `docs/01_SCOPE.md`
4. `docs/02_ARCHITECTURE.md`
5. `docs/06_SECURITY.md`
6. `plans/M07_CANONICAL_REVIEW_WRITEBACK.md`
7. `tasks/PROJECT_STATE.md`
8. `tasks/DECISION_LOG.md`

## Objective

Turn semantic candidates into explicitly reviewed canonical models and persist approved knowledge in DataHub.

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

- Implement review use cases for edit, approve, reject, and mark-as-different-concept actions.
- Implement a draft store and immutable/versioned decision records.
- Define a catalog-write port requiring an explicit approval object.
- Implement DataHub write-back for the supported combination of logical models/links, glossary terms, descriptions, structured properties, and decision documents.
- Use proposal workflows where practical; document a faithful fallback when a native logical-field operation is unavailable.
- Add idempotency and partial-failure handling plus integration tests.

## Acceptance criteria

- [ ] No DataHub write can occur without an explicit approval value passed by the use case.
- [ ] Approving a candidate creates a versioned decision; rejecting it never publishes approved metadata.
- [ ] Replaying the same approved write is idempotent or yields a typed already-current result.
- [ ] Partial failures are visible and recoverable without falsely marking the whole decision published.
- [ ] Customer canonical context is visible in DataHub and retrievable through the read adapter.
- [ ] Every published item records or links its SchemaBridge decision/version.

## Expected checks

```bash
pytest tests/unit -k "approval or decision or publish"
pytest -m integration -k "datahub and write"
make check
git diff --check
```

If a listed command contains a placeholder, determine and document the verified command rather than pretending the placeholder ran.

## Do not do

- Do not discover joins.
- Do not allow natural-language input to trigger writes.

## Operator test to prepare

1. Approve the three Customer key mappings and registration-date mapping in the current review interface/CLI.
2. Inspect DataHub before and after, including descriptions/properties/document and logical-model linkage or documented fallback.
3. Rerun publication and confirm no duplicate terms/documents are created.

Do not commit automatically unless the operator explicitly asks. End using `tasks/HANDOFF_TEMPLATE.md` and include a single proposed commit message.
