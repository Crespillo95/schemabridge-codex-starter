# Codex milestone M13: Validated query recipes, context write-back, and reuse

Use the `$schemabridge-milestone` skill. Work only on **M13**. Recommended setting: **GPT-5.6 Sol — Extra High (xhigh)**.

## Read first

1. `AGENTS.md` and all nested `AGENTS.md` files governing files you may edit.
2. `docs/00_PRODUCT_VISION.md`
3. `docs/01_SCOPE.md`
4. `docs/02_ARCHITECTURE.md`
5. `docs/06_SECURITY.md`
6. `plans/M13_CONTEXT_WRITEBACK_REUSE.md`
7. `tasks/PROJECT_STATE.md`
8. `tasks/DECISION_LOG.md`

## Objective

Publish a validated query recipe and retrieve it in a new workflow so repeated requests inherit approved semantic knowledge.

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

- Define a versioned `QueryRecipe` containing business question, normalized intent, approved model/mapping/join versions, SQL/plan fingerprint, validation summary, limitations, and linked assets.
- Implement explicit review and DataHub publication as a context document plus appropriate properties/links.
- Implement search/retrieval and compatibility checks for prior recipes.
- Reuse only recipes whose approved context versions remain current; otherwise explain staleness and replan.
- Add idempotency, provenance, and before/after/restart acceptance tests.
- Generate initial judge-readable artifacts under `examples/` from real execution.

## Acceptance criteria

- [ ] A recipe is never published before successful validation/execution and explicit approval.
- [ ] Publishing the same version is idempotent.
- [ ] A new process finds the recipe through DataHub and references its provenance.
- [ ] Stale context causes replanning or review, not blind SQL reuse.
- [ ] The reused path still runs SQL policy and read-only preview validation.
- [ ] Examples are generated from the implementation and clearly marked synthetic.

## Expected checks

```bash
pytest tests/unit -k "recipe or fingerprint or reuse"
pytest -m integration -k "document or recipe"
pytest -m acceptance -k context_reuse
make check
git diff --check
```

If a listed command contains a placeholder, determine and document the verified command rather than pretending the placeholder ran.

## Do not do

- Do not automatically execute saved SQL without replanning/validation.
- Do not build a general query marketplace.

## Operator test to prepare

1. Publish the north-star recipe, close the application, reopen it, and issue the same request.
2. Confirm the application states what was reused and what was revalidated.
3. Modify a mapping version in the demo and verify the recipe is marked stale.

Do not commit automatically unless the operator explicitly asks. End using `tasks/HANDOFF_TEMPLATE.md` and include a single proposed commit message.
