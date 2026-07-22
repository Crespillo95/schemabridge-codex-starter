# Codex milestone M00: Repository baseline

Use the `$schemabridge-milestone` skill. Work only on **M00**. Recommended setting: **GPT-5.6 Sol — High**.

## Read first

1. `AGENTS.md` and all nested `AGENTS.md` files governing files you may edit.
2. `docs/00_PRODUCT_VISION.md`
3. `docs/01_SCOPE.md`
4. `docs/02_ARCHITECTURE.md`
5. `docs/06_SECURITY.md`
6. `plans/M00_REPOSITORY_BASELINE.md`
7. `tasks/PROJECT_STATE.md`
8. `tasks/DECISION_LOG.md`

## Objective

Turn the supplied scaffold into a reproducible, trusted, quality-gated repository on the operator’s actual machine.

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

- Validate and, where necessary, correct `pyproject.toml`, package installation, CLI entrypoints, Make targets, CI, and cross-platform bootstrap scripts.
- Run the baseline doctor, formatting, linting, strict type checking, and unit tests in a clean virtual environment.
- Record the actual Python, OS, Docker, Codex, and package-manager versions in `docs/12_RUNBOOK.md` without pretending unavailable tools were tested.
- Initialize `tasks/PROJECT_STATE.md`, `tasks/CURRENT_TASK.md`, and the first accepted environment decisions.
- Ensure the repository can be committed without generated files or secrets.

## Acceptance criteria

- [ ] `python -m pip install -e ".[dev]"` succeeds in a clean environment.
- [ ] `schemabridge version` and `schemabridge doctor` succeed from the repository root.
- [ ] `make check` or the documented PowerShell-equivalent commands pass.
- [ ] CI configuration invokes commands that pass locally.
- [ ] No secret, `.env`, virtual environment, cache, or runtime artifact is tracked.
- [ ] The runbook distinguishes verified commands from future placeholders.

## Expected checks

```bash
python -m pip install -e ".[dev]"
ruff format --check src tests scripts
ruff check src tests scripts
mypy src
pytest -m "not integration and not acceptance"
schemabridge doctor
git diff --check
```

If a listed command contains a placeholder, determine and document the verified command rather than pretending the placeholder ran.

## Do not do

- Do not install DataHub, PostgreSQL runtime extras, Streamlit, or an LLM SDK yet.
- Do not implement product domain behavior.

## Operator test to prepare

1. Close the active shell, open a new one, activate the virtual environment, and run `schemabridge doctor`.
2. Run `git status --short` and confirm only intentional source changes appear.
3. Open `.codex/config.toml` through Codex settings and confirm the project configuration is recognized after trust is granted.

Do not commit automatically unless the operator explicitly asks. End using `tasks/HANDOFF_TEMPLATE.md` and include a single proposed commit message.
