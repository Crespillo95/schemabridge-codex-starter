# M12: Agent workflow orchestration

- Status: automated complete; operator acceptance pending
- Timebox: 2 hours
- Recommended Codex: GPT-5.6 Sol — Extra High (xhigh)
- Dependencies: M06–M11

## Objective

Compose catalog reading, semantic reasoning, human decisions, planning, execution, and optional context publication into an observable agent workflow.

## Why this milestone exists now

The contest category requires an agent that reads DataHub, acts, and leaves knowledge for the next user or agent, not a set of disconnected screens.

## Deliverables

- Implement an explicit workflow/state machine with stages such as context retrieval, intent resolution, semantic resolution, decision required, plan ready, validated, executed, and publication proposed/completed.
- Represent human checkpoints as typed decisions; do not bury approval in UI booleans.
- Emit structured trace events/tool summaries that can be shown in the UI without exposing chain-of-thought or secrets.
- Implement resumable draft state so an approval pause can continue safely.
- Handle retries/idempotency for read failures and publication attempts without rerunning source queries unnecessarily.
- Add an end-to-end fake-adapter test and one real local acceptance path.

## Implementation sequence

1. Define workflow states, allowed transitions, and terminal outcomes.
2. Wrap existing use cases rather than moving their logic into an orchestrator.
3. Add trace event types for operation, input references, output summary, timing, and status.
4. Implement pause/resume around ambiguity, mapping, join, and write approvals.
5. Add failure/retry tests and ensure safe steps are idempotent.
6. Document the workflow diagram and operator-visible trace.

## Acceptance criteria

- [ ] The workflow cannot skip required approvals or SQL validation states.
- [ ] Every external action has an observable start/result/error event without raw secrets or private reasoning.
- [ ] A paused workflow resumes from durable draft state and references the same approved versions.
- [ ] Retries do not duplicate DataHub documents or execute the same preview unexpectedly.
- [ ] The fake-adapter path completes deterministically and the real local path completes the north-star flow.
- [ ] Existing domain/application boundaries remain intact.

## Required automated checks

```bash
pytest tests/unit -k "workflow or orchestrator or trace"
pytest -m acceptance -k workflow
make check
git diff --check
```

Replace angle-bracket placeholders only when the actual environment has established the command. Never report an unexecuted placeholder as passing.

## Manual test for the operator

1. Start a request, pause at an ambiguity/approval checkpoint, restart the application, and resume.
2. Force a temporary DataHub read failure and confirm the workflow offers a retry without falling back to hard-coded context.
3. Inspect the trace and confirm it explains actions without exposing prompts, tokens, or chain-of-thought.

## Explicit non-goals

- Do not create an autonomous background scheduler.
- Do not implement a multi-agent runtime inside the product solely for novelty.

## Handoff requirements

Update `tasks/PROJECT_STATE.md`, `tasks/CURRENT_TASK.md`, and `tasks/DECISION_LOG.md`. Return the format in `tasks/HANDOFF_TEMPLATE.md`, including exact commands, results, limitations, and the proposed commit message.
