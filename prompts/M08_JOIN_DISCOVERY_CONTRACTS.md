# Codex milestone M08: Relationship discovery and governed join contracts

Use the `$schemabridge-milestone` skill. Work only on **M08**. Recommended setting: **GPT-5.6 Sol — Extra High (xhigh)**.

## Read first

1. `AGENTS.md` and all nested `AGENTS.md` files governing files you may edit.
2. `docs/00_PRODUCT_VISION.md`
3. `docs/01_SCOPE.md`
4. `docs/02_ARCHITECTURE.md`
5. `docs/06_SECURITY.md`
6. `plans/M08_JOIN_DISCOVERY_CONTRACTS.md`
7. `tasks/PROJECT_STATE.md`
8. `tasks/DECISION_LOG.md`

## Objective

Discover, explain, validate, approve, and persist relationships between logical models, including cardinality and fanout policy.

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

- Implement join candidate generation from declared constraints, DataHub lineage, historical queries, names/definitions, normalized value overlap, nulls, uniqueness, and row/cardinality estimates.
- Implement cardinality classification and confidence/evidence breakdown.
- Implement `JoinContract` approval/versioning and fanout policies.
- Validate the `Customer → AccountHolder` one-to-many and `AccountHolder → Account` many-to-one ground truth.
- Persist approved contracts as DataHub decision/context artifacts linked to relevant assets.
- Add adversarial tests for wrong keys, low overlap, duplicates, nulls, ambiguous paths, and many-to-many risk.

## Acceptance criteria

- [ ] Customer to AccountHolder is classified one-to-many and explains duplicate holder links.
- [ ] AccountHolder to Account is classified many-to-one using the declared foreign key and data evidence.
- [ ] A candidate with matching names but poor normalized overlap is not recommended as approved.
- [ ] Every executable join contract includes normalized keys, join type, cardinality, version, evidence, risks, and fanout policy.
- [ ] Many-to-many joins fail closed unless an explicit supported mitigation exists; none is required in the MVP.
- [ ] Approved contracts persist in DataHub and are loaded by a new process.

## Expected checks

```bash
pytest tests/unit -k "join or cardinality or fanout"
pytest -m integration -k "join or relationship"
make check
git diff --check
```

If a listed command contains a placeholder, determine and document the verified command rather than pretending the placeholder ran.

## Do not do

- Do not generate a natural-language query.
- Do not support arbitrary many-to-many execution or more than three tables.

## Operator test to prepare

1. Review the proposed Customer–AccountHolder contract and verify overlap/cardinality evidence against SQL counts.
2. Confirm the UI/CLI warning states that ordinary `COUNT(customer)` would overcount customer 123.
3. Approve and publish, restart the app, and confirm the contract is reused.

Do not commit automatically unless the operator explicitly asks. End using `tasks/HANDOFF_TEMPLATE.md` and include a single proposed commit message.
