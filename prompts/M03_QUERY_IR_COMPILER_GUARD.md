# Codex milestone M03: Restricted query IR, deterministic compiler, and SQL guard

Use the `$schemabridge-milestone` skill. Work only on **M03**. Recommended setting: **GPT-5.6 Sol — Extra High (xhigh)**.

## Read first

1. `AGENTS.md` and all nested `AGENTS.md` files governing files you may edit.
2. `docs/00_PRODUCT_VISION.md`
3. `docs/01_SCOPE.md`
4. `docs/02_ARCHITECTURE.md`
5. `docs/06_SECURITY.md`
6. `plans/M03_QUERY_IR_COMPILER_GUARD.md`
7. `tasks/PROJECT_STATE.md`
8. `tasks/DECISION_LOG.md`

## Objective

Compile an approved typed query plan into parameterized PostgreSQL and independently reject unsafe SQL before execution.

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

- Define a restricted query intermediate representation covering scans, mapped expressions, filters, approved joins, aggregates, date grains, group/order, and limit.
- Implement a PostgreSQL compiler adapter using structured AST construction where practical.
- Implement a separate SQL policy guard that reparses final SQL and validates one read-only statement, allowlisted assets, join predicates, table count, and preview limit.
- Use bound parameters for values; identifiers must originate from approved domain values.
- Add security regression fixtures for statement smuggling, DDL/DML, destructive CTEs, unknown assets, Cartesian joins, malicious values, and fourth-table queries.
- Compile the north-star plan and compare its execution result with ground truth through the read-only adapter.

## Acceptance criteria

- [ ] No IR node permits arbitrary raw SQL.
- [ ] Filter values are emitted as parameters, not interpolated literals.
- [ ] The north-star plan compiles to one `WITH ... SELECT` or `SELECT` and returns ground-truth rows.
- [ ] The guard rejects every case listed in `docs/06_SECURITY.md` that is in current scope.
- [ ] A compiler bug cannot bypass the independent parse/guard step.
- [ ] Preview execution enforces limit and timeout even when the request omits them.

## Expected checks

```bash
python -m pip install -e ".[dev,postgres,sql]"
pytest tests/unit -k "compiler or sql_guard or query_plan"
pytest -m integration -k "query or sql"
make check
git diff --check
```

If a listed command contains a placeholder, determine and document the verified command rather than pretending the placeholder ran.

## Do not do

- Do not parse natural language.
- Do not infer mappings or joins; consume explicit approved fixtures.

## Operator test to prepare

1. Compile and print the north-star plan with parameters, then run the preview.
2. Attempt at least three malicious examples from the security matrix and confirm stable rejection codes.
3. Inspect the executor connection identity and verify it is `schemabridge_reader`.

Do not commit automatically unless the operator explicitly asks. End using `tasks/HANDOFF_TEMPLATE.md` and include a single proposed commit message.
