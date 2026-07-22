# Codex milestone M01: Synthetic demo database

Use the `$schemabridge-milestone` skill. Work only on **M01**. Recommended setting: **GPT-5.6 Sol — High**.

## Read first

1. `AGENTS.md` and all nested `AGENTS.md` files governing files you may edit.
2. `docs/00_PRODUCT_VISION.md`
3. `docs/01_SCOPE.md`
4. `docs/02_ARCHITECTURE.md`
5. `docs/06_SECURITY.md`
6. `plans/M01_SYNTHETIC_DEMO_DATABASE.md`
7. `tasks/PROJECT_STATE.md`
8. `tasks/DECISION_LOG.md`

## Objective

Provide a deterministic PostgreSQL dataset that reproduces heterogeneous identifiers, role codes, invalid values, and one-to-many fanout.

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

- Verify and harden `docker-compose.demo.yml` and ordered SQL initialization scripts.
- Add an application-level PostgreSQL health/readiness probe behind a future-friendly port or a temporary CLI command without leaking SQLAlchemy into domain code.
- Add integration tests for seeded row counts, expected north-star result, invalid identifier cases, and read-only role enforcement.
- Document reset, inspect, and troubleshooting commands.
- Keep the reference query explicitly separated from production compiler logic.

## Acceptance criteria

- [ ] `make demo-reset` creates a healthy PostgreSQL 16 service from zero state.
- [ ] The reader can select all demo assets but cannot insert, update, delete, create, or alter.
- [ ] The reference north-star query returns 2, 1, and 1 for the three expected dates.
- [ ] Duplicate relationships for customer 123 exist and would overcount without `COUNT DISTINCT`.
- [ ] `127.5`, `NaN`, and `NULL` demonstrate distinct rejection paths.
- [ ] Integration tests pass after a complete volume reset.

## Expected checks

```bash
make demo-reset
docker compose -f docker-compose.demo.yml ps
pytest -m integration -k postgres
make check
git diff --check
```

If a listed command contains a placeholder, determine and document the verified command rather than pretending the placeholder ran.

## Do not do

- Do not ingest metadata into DataHub.
- Do not implement generic query compilation or semantic matching.

## Operator test to prepare

1. Run `make demo-reset` and inspect `docker compose -f docker-compose.demo.yml ps`.
2. Execute the reference query as the reader and compare the exact rows with `demo/ground_truth/query_cases.yml`.
3. Attempt an insert as `schemabridge_reader` and confirm PostgreSQL rejects it.

Do not commit automatically unless the operator explicitly asks. End using `tasks/HANDOFF_TEMPLATE.md` and include a single proposed commit message.
