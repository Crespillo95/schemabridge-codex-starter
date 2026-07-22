# Starter-kit validation report

Validation date: 2026-07-21

This report covers the generated starter kit before the first project milestone. It does not claim that the final SchemaBridge product is implemented.

## Verified in the artifact environment

- Python 3.13.5 virtual environment created successfully.
- `python -m pip install -e ".[dev]"` completed successfully.
- `ruff format --check src tests scripts` passed.
- `ruff check src tests scripts` passed.
- `mypy src` passed in strict mode.
- `pytest -m "not integration and not acceptance"` passed: 3 tests.
- `schemabridge doctor` passed all required starter checks.
- `python scripts/validate_starter.py` passed.
- `pyproject.toml` and `.codex/config.toml` parsed successfully.
- All milestone plans and matching prompts M00–M19 are present.
- YAML ground-truth fixtures parse successfully.
- Python sources compile successfully.

## Not verified in the artifact environment

Docker was not installed in the artifact-generation environment. Therefore these items were prepared but not executed here:

- synthetic PostgreSQL container startup;
- DataHub quickstart or ingestion;
- DataHub MCP connectivity;
- integration and acceptance tests that require services;
- public deployment.

M00 verifies the operator's local baseline. M01 verifies Docker/PostgreSQL. M04 verifies DataHub and MCP using current, pinned versions. No later milestone may claim these integrations work until those gates pass on the operator's machine.

## Expected first operator gate

After bootstrap, run:

```bash
make check
schemabridge doctor
python scripts/validate_starter.py
```

Then execute the manual test in `plans/M00_REPOSITORY_BASELINE.md` and return the completed handoff.
