# Codex milestone M10: Governed semantic planner and read-only execution

Use the `$schemabridge-milestone` skill. Work only on **M10**. Recommended setting: **GPT-5.6 Sol — Extra High (xhigh)**.

## Read first

1. `AGENTS.md` and all nested `AGENTS.md` files governing files you may edit.
2. `docs/00_PRODUCT_VISION.md`
3. `docs/01_SCOPE.md`
4. `docs/02_ARCHITECTURE.md`
5. `docs/06_SECURITY.md`
6. `plans/M10_GOVERNED_PLANNER_EXECUTION.md`
7. `tasks/PROJECT_STATE.md`
8. `tasks/DECISION_LOG.md`

## Objective

Resolve a guided analytical request through approved mappings and join contracts into a validated query plan, compile it, execute a bounded preview, and return results plus rejected records.

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

- Implement the semantic planner use case that selects approved mapping versions, physical datasets, transformations, and shortest approved join path.
- Implement type compatibility, stale/unapproved-context checks, table-count limits, and fanout analysis.
- Automatically apply only contract-defined fanout mitigations; expose every assumption and mitigation in the resolved plan.
- Wire the planner to the deterministic compiler, SQL guard, and PostgreSQL preview executor through ports.
- Return a typed result containing plan, SQL, parameters, policy findings, rows, execution metadata, and rejected-source report.
- Complete an acceptance test for the guided north-star request and a no-join control case.

## Acceptance criteria

- [ ] The guided north-star request resolves to `crm.customers` and `bank.account_holders` through the approved contract.
- [ ] The plan uses approved normalization rules and role-code mapping.
- [ ] The one-to-many relationship causes `COUNT DISTINCT Customer.customer_key` and the reason is visible.
- [ ] Unapproved, stale, disconnected, or ambiguous context blocks execution with a typed explanation.
- [ ] The final preview returns exactly the ground-truth rows and reports `127.5`, NaN, and NULL rejections as specified.
- [ ] The no-join request uses only Customer and does not manufacture a relationship.
- [ ] Application code depends on ports, not concrete PostgreSQL/SQL/DataHub adapters.

## Expected checks

```bash
pytest tests/unit -k "planner or resolution or fanout"
pytest -m integration -k "planner or preview"
pytest -m acceptance -k guided_north_star
make check
git diff --check
```

If a listed command contains a placeholder, determine and document the verified command rather than pretending the placeholder ran.

## Do not do

- Do not parse natural language.
- Do not publish query recipes or build the final Streamlit interface.

## Operator test to prepare

1. Run the guided north-star request and inspect the complete resolved plan before execution.
2. Change the metric to relationship count and confirm the interpretation/result changes explicitly.
3. Mark the join contract unapproved in a test draft and confirm execution is disabled.

Do not commit automatically unless the operator explicitly asks. End using `tasks/HANDOFF_TEMPLATE.md` and include a single proposed commit message.
