# M00: Repository baseline

- Status: planned
- Timebox: 2 hours
- Recommended Codex: GPT-5.6 Sol — High
- Dependencies: None

## Objective

Turn the supplied scaffold into a reproducible, trusted, quality-gated repository on the operator’s actual machine.

## Why this milestone exists now

Every later module depends on stable commands, typed configuration, CI, durable state files, and an honest runbook.

## Deliverables

- Validate and, where necessary, correct `pyproject.toml`, package installation, CLI entrypoints, Make targets, CI, and cross-platform bootstrap scripts.
- Run the baseline doctor, formatting, linting, strict type checking, and unit tests in a clean virtual environment.
- Record the actual Python, OS, Docker, Codex, and package-manager versions in `docs/12_RUNBOOK.md` without pretending unavailable tools were tested.
- Initialize `tasks/PROJECT_STATE.md`, `tasks/CURRENT_TASK.md`, and the first accepted environment decisions.
- Ensure the repository can be committed without generated files or secrets.

## Implementation sequence

1. Inspect all root configuration and executable starter files before editing.
2. Create a clean virtual environment and install only the development extra.
3. Fix invalid dependency metadata, console scripts, typing issues, or platform assumptions discovered by real commands.
4. Add a minimal smoke test for package import, CLI version, and doctor behavior.
5. Run the full baseline gate twice: once before and once after cleanup.
6. Update the runbook and durable task state with exact results.

## Acceptance criteria

- [ ] `python -m pip install -e ".[dev]"` succeeds in a clean environment.
- [ ] `schemabridge version` and `schemabridge doctor` succeed from the repository root.
- [ ] `make check` or the documented PowerShell-equivalent commands pass.
- [ ] CI configuration invokes commands that pass locally.
- [ ] No secret, `.env`, virtual environment, cache, or runtime artifact is tracked.
- [ ] The runbook distinguishes verified commands from future placeholders.

## Required automated checks

```bash
python -m pip install -e ".[dev]"
ruff format --check src tests scripts
ruff check src tests scripts
mypy src
pytest -m "not integration and not acceptance"
schemabridge doctor
git diff --check
```

Replace angle-bracket placeholders only when the actual environment has established the command. Never report an unexecuted placeholder as passing.

## Manual test for the operator

1. Close the active shell, open a new one, activate the virtual environment, and run `schemabridge doctor`.
2. Run `git status --short` and confirm only intentional source changes appear.
3. Open `.codex/config.toml` through Codex settings and confirm the project configuration is recognized after trust is granted.

## Explicit non-goals

- Do not install DataHub, PostgreSQL runtime extras, Streamlit, or an LLM SDK yet.
- Do not implement product domain behavior.

## Handoff requirements

Update `tasks/PROJECT_STATE.md`, `tasks/CURRENT_TASK.md`, and `tasks/DECISION_LOG.md`. Return the format in `tasks/HANDOFF_TEMPLATE.md`, including exact commands, results, limitations, and the proposed commit message.
