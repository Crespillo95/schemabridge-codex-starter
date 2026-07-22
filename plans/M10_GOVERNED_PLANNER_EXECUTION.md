# M10: Governed semantic planner and read-only execution

- Status: planned
- Timebox: 4 hours
- Recommended Codex: GPT-5.6 Sol — Extra High (xhigh)
- Dependencies: M03, M07, M08, M09

## Objective

Resolve a guided analytical request through approved mappings and join contracts into a validated query plan, compile it, execute a bounded preview, and return results plus rejected records.

## Why this milestone exists now

This milestone proves the first complete business-value path before natural-language and UI complexity are added.

## Deliverables

- Implement the semantic planner use case that selects approved mapping versions, physical datasets, transformations, and shortest approved join path.
- Implement type compatibility, stale/unapproved-context checks, table-count limits, and fanout analysis.
- Automatically apply only contract-defined fanout mitigations; expose every assumption and mitigation in the resolved plan.
- Wire the planner to the deterministic compiler, SQL guard, and PostgreSQL preview executor through ports.
- Return a typed result containing plan, SQL, parameters, policy findings, rows, execution metadata, and rejected-source report.
- Complete an acceptance test for the guided north-star request and a no-join control case.

## Implementation sequence

1. Define planner inputs/outputs and typed resolution errors before implementation.
2. Implement logical-field resolution against approved mappings and versions.
3. Implement graph/path selection over approved join contracts only.
4. Add cardinality propagation and fanout mitigation validation.
5. Compose compilation, guarding, and execution in an application use case without importing concrete adapters.
6. Run the north-star acceptance path against the synthetic PostgreSQL database.

## Acceptance criteria

- [ ] The guided north-star request resolves to `crm.customers` and `bank.account_holders` through the approved contract.
- [ ] The plan uses approved normalization rules and role-code mapping.
- [ ] The one-to-many relationship causes `COUNT DISTINCT Customer.customer_key` and the reason is visible.
- [ ] Unapproved, stale, disconnected, or ambiguous context blocks execution with a typed explanation.
- [ ] The final preview returns exactly the ground-truth rows and reports `127.5`, NaN, and NULL rejections as specified.
- [ ] The no-join request uses only Customer and does not manufacture a relationship.
- [ ] Application code depends on ports, not concrete PostgreSQL/SQL/DataHub adapters.

## Required automated checks

```bash
pytest tests/unit -k "planner or resolution or fanout"
pytest -m integration -k "planner or preview"
pytest -m acceptance -k guided_north_star
make check
git diff --check
```

Replace angle-bracket placeholders only when the actual environment has established the command. Never report an unexecuted placeholder as passing.

## Manual test for the operator

1. Run the guided north-star request and inspect the complete resolved plan before execution.
2. Change the metric to relationship count and confirm the interpretation/result changes explicitly.
3. Mark the join contract unapproved in a test draft and confirm execution is disabled.

## Explicit non-goals

- Do not parse natural language.
- Do not publish query recipes or build the final Streamlit interface.

## Handoff requirements

Update `tasks/PROJECT_STATE.md`, `tasks/CURRENT_TASK.md`, and `tasks/DECISION_LOG.md`. Return the format in `tasks/HANDOFF_TEMPLATE.md`, including exact commands, results, limitations, and the proposed commit message.
