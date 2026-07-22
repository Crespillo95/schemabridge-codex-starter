# SchemaBridge repository instructions

## Mission

Build a DataHub-native governed semantic query agent that resolves inconsistent physical schemas into approved logical concepts and join contracts, compiles business requests into safe SQL, and writes reusable context back to DataHub.

## Required reading before editing

Read, in order:

1. `docs/00_PRODUCT_VISION.md`
2. `docs/01_SCOPE.md`
3. `docs/02_ARCHITECTURE.md`
4. `docs/06_SECURITY.md`
5. the current milestone in `plans/`
6. `tasks/PROJECT_STATE.md`
7. `tasks/DECISION_LOG.md`

Use the local `$schemabridge-milestone` skill for milestone work.

## Non-negotiable product invariants

- Never modify source databases.
- Use synthetic data only in the public repository.
- Never treat name similarity as sufficient evidence of semantic equivalence.
- Every proposed mapping and join contract has evidence, confidence, risks, and status.
- Ambiguous semantic decisions require explicit human approval.
- DataHub write operations require explicit approval and an audit record.
- The LLM never produces SQL that is executed directly.
- The LLM may produce only validated typed intent objects or explanations.
- A deterministic compiler produces SQL from a typed query plan.
- Validate SQL with an AST before execution.
- Permit exactly one read-only `SELECT` or `WITH ... SELECT` statement.
- Reject DDL, DML, utility statements, comments that conceal statements, Cartesian joins, and unknown assets.
- Enforce table allowlists, maximum three tables, result limits, and statement timeout.
- Detect one-to-many fanout and require an explicit mitigation such as `COUNT DISTINCT`.
- Reject unsafe float identifiers; never silently truncate them.
- Preserve `NULL` according to the approved policy.

## Architecture boundaries

- `domain/` contains no I/O, framework, database, DataHub, LLM, Streamlit, SQLAlchemy, or environment imports.
- `application/` depends on domain types and `typing.Protocol` ports, never concrete adapters.
- `adapters/` implement ports and may depend on external libraries.
- `entrypoints/` translate user input/output and invoke application use cases; business rules do not live there.
- `bootstrap.py` is the only composition root.
- Cross-module calls flow inward: entrypoint → application → domain; adapters are injected through ports.
- Avoid global mutable state and hidden service locators.

## Development behavior

- Work on one milestone only.
- Inspect existing code and tests before editing.
- State a concise plan before implementation.
- Prefer a vertical slice over broad scaffolding.
- Do not add a production dependency without documenting why in the handoff.
- Keep public interfaces typed and small.
- Write tests before or alongside behavior.
- Do not weaken assertions merely to make tests pass.
- Keep commits focused; do not refactor unrelated code.
- Do not use `git reset --hard`, discard user work, or rewrite history.
- Never place secrets in code, logs, fixtures, screenshots, or commits.

## Quality gate

Before declaring a task complete, run the relevant subset and then the full gate:

```bash
make check
```

For milestones involving services, also run their integration and acceptance tests. Record every command and result in the handoff.

## Completion contract

A milestone is complete only when:

1. all acceptance criteria in its plan are met;
2. automated tests pass;
3. the manual test is documented for the operator;
4. examples and docs match actual behavior;
5. `tasks/PROJECT_STATE.md`, `tasks/DECISION_LOG.md`, and `tasks/CURRENT_TASK.md` are updated;
6. the final response follows `tasks/HANDOFF_TEMPLATE.md`.

## Communication

Be precise. Report uncertainty, omitted tests, unavailable services, and known limitations explicitly. Never claim a command passed when it was not executed.
