# Repair the current SchemaBridge milestone

Use `$schemabridge-milestone`. Read `AGENTS.md`, the current milestone plan, `tasks/PROJECT_STATE.md`, `tasks/CURRENT_TASK.md`, `tasks/DECISION_LOG.md`, and the complete failure output supplied after this prompt.

Rules:

- Stay within the current milestone.
- Reproduce the failure before changing code when feasible.
- Identify the narrowest responsible layer.
- Add a regression test that fails for the reported defect.
- Fix the cause, not the assertion or symptom.
- Run focused checks and the full `make check` gate.
- Do not discard unrelated user changes.
- Update durable state and return `tasks/HANDOFF_TEMPLATE.md`.

Failure output follows:

```text
PASTE COMPLETE COMMAND, STDOUT, STDERR, ENVIRONMENT, AND MANUAL STEPS HERE
```
