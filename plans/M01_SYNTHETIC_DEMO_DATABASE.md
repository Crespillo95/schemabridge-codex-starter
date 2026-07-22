# M01: Synthetic demo database

- Status: planned
- Timebox: 3 hours
- Recommended Codex: GPT-5.6 Sol — High
- Dependencies: M00

## Objective

Provide a deterministic PostgreSQL dataset that reproduces heterogeneous identifiers, role codes, invalid values, and one-to-many fanout.

## Why this milestone exists now

The entire product and evaluation require a small but adversarial ground truth that can prove semantic and SQL correctness.

## Deliverables

- Verify and harden `docker-compose.demo.yml` and ordered SQL initialization scripts.
- Add an application-level PostgreSQL health/readiness probe behind a future-friendly port or a temporary CLI command without leaking SQLAlchemy into domain code.
- Add integration tests for seeded row counts, expected north-star result, invalid identifier cases, and read-only role enforcement.
- Document reset, inspect, and troubleshooting commands.
- Keep the reference query explicitly separated from production compiler logic.

## Implementation sequence

1. Start from a clean Docker volume and inspect container logs.
2. Verify all schemas, comments, constraints, rows, role settings, and expected results.
3. Implement the smallest PostgreSQL test adapter required for integration checks, or use direct psycopg fixtures under tests if the production port is not yet defined.
4. Prove writes fail using the reader account and that the statement timeout is configured.
5. Add deterministic integration fixtures and mark them correctly.
6. Reset the database and rerun tests to prove repeatability.

## Acceptance criteria

- [ ] `make demo-reset` creates a healthy PostgreSQL 16 service from zero state.
- [ ] The reader can select all demo assets but cannot insert, update, delete, create, or alter.
- [ ] The reference north-star query returns 2, 1, and 1 for the three expected dates.
- [ ] Duplicate relationships for customer 123 exist and would overcount without `COUNT DISTINCT`.
- [ ] `127.5`, `NaN`, and `NULL` demonstrate distinct rejection paths.
- [ ] Integration tests pass after a complete volume reset.

## Required automated checks

```bash
make demo-reset
docker compose -f docker-compose.demo.yml ps
pytest -m integration -k postgres
make check
git diff --check
```

Replace angle-bracket placeholders only when the actual environment has established the command. Never report an unexecuted placeholder as passing.

## Manual test for the operator

1. Run `make demo-reset` and inspect `docker compose -f docker-compose.demo.yml ps`.
2. Execute the reference query as the reader and compare the exact rows with `demo/ground_truth/query_cases.yml`.
3. Attempt an insert as `schemabridge_reader` and confirm PostgreSQL rejects it.

## Explicit non-goals

- Do not ingest metadata into DataHub.
- Do not implement generic query compilation or semantic matching.

## Handoff requirements

Update `tasks/PROJECT_STATE.md`, `tasks/CURRENT_TASK.md`, and `tasks/DECISION_LOG.md`. Return the format in `tasks/HANDOFF_TEMPLATE.md`, including exact commands, results, limitations, and the proposed commit message.
