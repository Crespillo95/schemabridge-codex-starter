# M03: Restricted query IR, deterministic compiler, and SQL guard

- Status: planned
- Timebox: 4 hours
- Recommended Codex: GPT-5.6 Sol — Extra High (xhigh)
- Dependencies: M02

## Objective

Compile an approved typed query plan into parameterized PostgreSQL and independently reject unsafe SQL before execution.

## Why this milestone exists now

The application must prove that natural language can never flow directly into executable SQL and that safety is enforced in depth.

## Deliverables

- Define a restricted query intermediate representation covering scans, mapped expressions, filters, approved joins, aggregates, date grains, group/order, and limit.
- Implement a PostgreSQL compiler adapter using structured AST construction where practical.
- Implement a separate SQL policy guard that reparses final SQL and validates one read-only statement, allowlisted assets, join predicates, table count, and preview limit.
- Use bound parameters for values; identifiers must originate from approved domain values.
- Add security regression fixtures for statement smuggling, DDL/DML, destructive CTEs, unknown assets, Cartesian joins, malicious values, and fourth-table queries.
- Compile the north-star plan and compare its execution result with ground truth through the read-only adapter.

## Implementation sequence

1. Finalize the QueryPlan/IR boundary in domain/application without vendor SQL strings.
2. Add SQLGlot as an adapter dependency only and document the dependency decision.
3. Implement compiler support for the minimal north-star operations first.
4. Implement the guard as an independent component that accepts final SQL plus explicit allowlists.
5. Add unit tests for every allowed and denied node/statement.
6. Run a PostgreSQL integration test proving correct result and database-level write rejection.

## Acceptance criteria

- [ ] No IR node permits arbitrary raw SQL.
- [ ] Filter values are emitted as parameters, not interpolated literals.
- [ ] The north-star plan compiles to one `WITH ... SELECT` or `SELECT` and returns ground-truth rows.
- [ ] The guard rejects every case listed in `docs/06_SECURITY.md` that is in current scope.
- [ ] A compiler bug cannot bypass the independent parse/guard step.
- [ ] Preview execution enforces limit and timeout even when the request omits them.

## Required automated checks

```bash
python -m pip install -e ".[dev,postgres,sql]"
pytest tests/unit -k "compiler or sql_guard or query_plan"
pytest -m integration -k "query or sql"
make check
git diff --check
```

Replace angle-bracket placeholders only when the actual environment has established the command. Never report an unexecuted placeholder as passing.

## Manual test for the operator

1. Compile and print the north-star plan with parameters, then run the preview.
2. Attempt at least three malicious examples from the security matrix and confirm stable rejection codes.
3. Inspect the executor connection identity and verify it is `schemabridge_reader`.

## Explicit non-goals

- Do not parse natural language.
- Do not infer mappings or joins; consume explicit approved fixtures.

## Handoff requirements

Update `tasks/PROJECT_STATE.md`, `tasks/CURRENT_TASK.md`, and `tasks/DECISION_LOG.md`. Return the format in `tasks/HANDOFF_TEMPLATE.md`, including exact commands, results, limitations, and the proposed commit message.
