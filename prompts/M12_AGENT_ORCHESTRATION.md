# Codex milestone M12: Agent workflow orchestration

Use the `$schemabridge-milestone` skill. Work only on **M12**. Recommended setting: **GPT-5.6 Sol — Extra High (xhigh)**.

## Read first

1. `AGENTS.md` and all nested `AGENTS.md` files governing files you may edit.
2. `docs/00_PRODUCT_VISION.md`
3. `docs/01_SCOPE.md`
4. `docs/02_ARCHITECTURE.md`
5. `docs/06_SECURITY.md`
6. `plans/M12_AGENT_ORCHESTRATION.md`
7. `tasks/PROJECT_STATE.md`
8. `tasks/DECISION_LOG.md`

## Objective

Compose catalog reading, semantic reasoning, human decisions, planning, execution, and optional context publication into an observable agent workflow.

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

- Implement an explicit workflow/state machine with stages such as context retrieval, intent resolution, semantic resolution, decision required, plan ready, validated, executed, and publication proposed/completed.
- Represent human checkpoints as typed decisions; do not bury approval in UI booleans.
- Emit structured trace events/tool summaries that can be shown in the UI without exposing chain-of-thought or secrets.
- Implement resumable draft state so an approval pause can continue safely.
- Handle retries/idempotency for read failures and publication attempts without rerunning source queries unnecessarily.
- Add an end-to-end fake-adapter test and one real local acceptance path.

## Acceptance criteria

- [ ] The workflow cannot skip required approvals or SQL validation states.
- [ ] Every external action has an observable start/result/error event without raw secrets or private reasoning.
- [ ] A paused workflow resumes from durable draft state and references the same approved versions.
- [ ] Retries do not duplicate DataHub documents or execute the same preview unexpectedly.
- [ ] The fake-adapter path completes deterministically and the real local path completes the north-star flow.
- [ ] Existing domain/application boundaries remain intact.

## Expected checks

```bash
pytest tests/unit -k "workflow or orchestrator or trace"
pytest -m acceptance -k workflow
make check
git diff --check
```

If a listed command contains a placeholder, determine and document the verified command rather than pretending the placeholder ran.

## Do not do

- Do not create an autonomous background scheduler.
- Do not implement a multi-agent runtime inside the product solely for novelty.

## Operator test to prepare

1. Start a request, pause at an ambiguity/approval checkpoint, restart the application, and resume.
2. Force a temporary DataHub read failure and confirm the workflow offers a retry without falling back to hard-coded context.
3. Inspect the trace and confirm it explains actions without exposing prompts, tokens, or chain-of-thought.

Do not commit automatically unless the operator explicitly asks. End using `tasks/HANDOFF_TEMPLATE.md` and include a single proposed commit message.
