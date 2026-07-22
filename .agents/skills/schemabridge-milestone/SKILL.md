---
name: schemabridge-milestone
description: Execute one SchemaBridge milestone end-to-end. Use for M00-M19 implementation, debugging, testing, review, and handoff tasks in this repository.
---

# SchemaBridge milestone workflow

## Inputs

- A milestone ID such as `M03`.
- The matching file in `plans/`.
- The current durable state under `tasks/`.

## Workflow

1. Read root `AGENTS.md` and every nested `AGENTS.md` governing files you may edit.
2. Read the milestone plan, architecture, security rules, project state, and decision log.
3. Inspect the repository and relevant tests. Do not assume the plan reflects the current implementation.
4. Restate the milestone objective, dependencies, proposed files, and acceptance tests in no more than 12 lines.
5. Identify blockers or ambiguous requirements. If they affect safety or architecture, stop and ask; otherwise choose the smallest reversible implementation and record the assumption.
6. Implement only the milestone scope. Preserve ports-and-adapters boundaries.
7. Add or update unit, integration, and acceptance tests required by the plan.
8. Run formatting, linting, type checking, focused tests, and then `make check`.
9. Review the complete diff for:
   - source-data writes;
   - SQL injection or statement smuggling;
   - unvalidated LLM output;
   - broken dependency direction;
   - hidden assumptions and fanout errors;
   - secrets and proprietary data;
   - documentation drift.
10. Update project state, decisions, and current task.
11. Return the exact handoff format from `tasks/HANDOFF_TEMPLATE.md`.

## Subagents

Use subagents only for independent, read-heavy work such as codebase inspection, test-log analysis, security review, or documentation verification. Avoid parallel edits to overlapping files. M16 explicitly requests parallel audit subagents.

## Stop conditions

Stop rather than improvise when:

- a source database write is required;
- a DataHub mutation lacks explicit approval;
- the only proposed solution executes raw LLM SQL;
- a test would require real employer data;
- a requirement contradicts an accepted ADR;
- credentials or external services are missing and no fake adapter can preserve the contract.
