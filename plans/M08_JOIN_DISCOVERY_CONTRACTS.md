# M08: Relationship discovery and governed join contracts

- Status: planned
- Timebox: 4 hours
- Recommended Codex: GPT-5.6 Sol — Extra High (xhigh)
- Dependencies: M05, M06, M07

## Objective

Discover, explain, validate, approve, and persist relationships between logical models, including cardinality and fanout policy.

## Why this milestone exists now

Automatic joins are the highest semantic-risk expansion and the key to the user’s multi-table extraction goal.

## Deliverables

- Implement join candidate generation from declared constraints, DataHub lineage, historical queries, names/definitions, normalized value overlap, nulls, uniqueness, and row/cardinality estimates.
- Implement cardinality classification and confidence/evidence breakdown.
- Implement `JoinContract` approval/versioning and fanout policies.
- Validate the `Customer → AccountHolder` one-to-many and `AccountHolder → Account` many-to-one ground truth.
- Persist approved contracts as DataHub decision/context artifacts linked to relevant assets.
- Add adversarial tests for wrong keys, low overlap, duplicates, nulls, ambiguous paths, and many-to-many risk.

## Implementation sequence

1. Define a relationship-evidence port and pure cardinality calculations.
2. Retrieve declared and observed evidence through DataHub/PostgreSQL adapters with bounded queries.
3. Rank candidate joins without allowing name similarity to decide alone.
4. Calculate cardinality from distinctness/duplicates and compare with declared context.
5. Create review and approval flow with fanout mitigation requirements.
6. Publish and retrieve the two north-star join contracts.

## Acceptance criteria

- [ ] Customer to AccountHolder is classified one-to-many and explains duplicate holder links.
- [ ] AccountHolder to Account is classified many-to-one using the declared foreign key and data evidence.
- [ ] A candidate with matching names but poor normalized overlap is not recommended as approved.
- [ ] Every executable join contract includes normalized keys, join type, cardinality, version, evidence, risks, and fanout policy.
- [ ] Many-to-many joins fail closed unless an explicit supported mitigation exists; none is required in the MVP.
- [ ] Approved contracts persist in DataHub and are loaded by a new process.

## Required automated checks

```bash
pytest tests/unit -k "join or cardinality or fanout"
pytest -m integration -k "join or relationship"
make check
git diff --check
```

Replace angle-bracket placeholders only when the actual environment has established the command. Never report an unexecuted placeholder as passing.

## Manual test for the operator

1. Review the proposed Customer–AccountHolder contract and verify overlap/cardinality evidence against SQL counts.
2. Confirm the UI/CLI warning states that ordinary `COUNT(customer)` would overcount customer 123.
3. Approve and publish, restart the app, and confirm the contract is reused.

## Explicit non-goals

- Do not generate a natural-language query.
- Do not support arbitrary many-to-many execution or more than three tables.

## Handoff requirements

Update `tasks/PROJECT_STATE.md`, `tasks/CURRENT_TASK.md`, and `tasks/DECISION_LOG.md`. Return the format in `tasks/HANDOFF_TEMPLATE.md`, including exact commands, results, limitations, and the proposed commit message.
