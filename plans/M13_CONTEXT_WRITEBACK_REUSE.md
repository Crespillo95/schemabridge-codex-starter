# M13: Validated query recipes, context write-back, and reuse

- Status: automated complete; operator acceptance pending
- Timebox: 2 hours
- Recommended Codex: GPT-5.6 Sol — Extra High (xhigh)
- Dependencies: M07, M08, M10, M12

## Objective

Publish a validated query recipe and retrieve it in a new workflow so repeated requests inherit approved semantic knowledge.

## Why this milestone exists now

Persistent context reuse is the clearest proof that the agent does real work and contributes back to DataHub.

## Deliverables

- Define a versioned `QueryRecipe` containing business question, normalized intent, approved model/mapping/join versions, SQL/plan fingerprint, validation summary, limitations, and linked assets.
- Implement explicit review and DataHub publication as a context document plus appropriate properties/links.
- Implement search/retrieval and compatibility checks for prior recipes.
- Reuse only recipes whose approved context versions remain current; otherwise explain staleness and replan.
- Add idempotency, provenance, and before/after/restart acceptance tests.
- Generate initial judge-readable artifacts under `examples/` from real execution.

## Implementation sequence

1. Design the recipe schema and stable fingerprinting rules.
2. Build the publish/retrieve use cases against existing catalog ports.
3. Add compatibility checks for changed mappings, joins, source schemas, or compiler version.
4. Publish the north-star recipe after explicit approval.
5. Start a new process/workflow and prove reuse reduces discovery steps while retaining validation.
6. Export sanitized YAML/SQL/Markdown examples from the actual result.

## Acceptance criteria

- [x] A recipe is never published before successful validation/execution and explicit approval.
- [x] Publishing the same version is idempotent.
- [x] A new process finds the recipe through DataHub and references its provenance.
- [x] Stale context causes replanning or review, not blind SQL reuse.
- [x] The reused path still runs SQL policy and read-only preview validation.
- [x] Examples are generated from the implementation and clearly marked synthetic.

## Required automated checks

```bash
pytest tests/unit -k "recipe or fingerprint or reuse"
pytest -m integration -k "document or recipe"
pytest -m acceptance -k context_reuse
make check
git diff --check
```

Replace angle-bracket placeholders only when the actual environment has established the command. Never report an unexecuted placeholder as passing.

## Manual test for the operator

1. Publish the north-star recipe, close the application, reopen it, and issue the same request.
2. Confirm the application states what was reused and what was revalidated.
3. Modify a mapping version in the demo and verify the recipe is marked stale.

## Explicit non-goals

- Do not automatically execute saved SQL without replanning/validation.
- Do not build a general query marketplace.

## Handoff requirements

Update `tasks/PROJECT_STATE.md`, `tasks/CURRENT_TASK.md`, and `tasks/DECISION_LOG.md`. Return the format in `tasks/HANDOFF_TEMPLATE.md`, including exact commands, results, limitations, and the proposed commit message.
