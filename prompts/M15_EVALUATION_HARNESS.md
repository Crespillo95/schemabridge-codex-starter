# Codex milestone M15: Reproducible evaluation harness and evidence

Use the `$schemabridge-milestone` skill. Work only on **M15**. Recommended setting: **GPT-5.6 Sol — High**.

## Read first

1. `AGENTS.md` and all nested `AGENTS.md` files governing files you may edit.
2. `docs/00_PRODUCT_VISION.md`
3. `docs/01_SCOPE.md`
4. `docs/02_ARCHITECTURE.md`
5. `docs/06_SECURITY.md`
6. `plans/M15_EVALUATION_HARNESS.md`
7. `tasks/PROJECT_STATE.md`
8. `tasks/DECISION_LOG.md`

## Objective

Measure semantic matching, join reasoning, request interpretation, SQL execution, result correctness, safety rejection, and context reuse with reproducible artifacts.

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

- Implement a CLI evaluation command that consumes versioned ground truth and emits JSON plus Markdown summaries.
- Measure candidate precision/recall/F1/top-k, join path/cardinality accuracy, intent equivalence, compile/execution success, result correctness, safety rejection rate, and recipe reuse.
- Keep deterministic and optional live-LLM evaluations separate and labeled.
- Add bootstrap confidence intervals or case counts only when statistically meaningful; otherwise report raw counts honestly.
- Generate `reports/evaluation.json` and a judge-readable example report from the release candidate.
- Add regression thresholds only after baseline measurements are real and justified.

## Acceptance criteria

- [ ] One documented command reproduces all deterministic metrics from a clean seeded environment.
- [ ] Metric denominators, skipped cases, and failures are visible.
- [ ] False positives/negatives and rejected cases are retained in detailed output.
- [ ] Result correctness compares normalized rows, not just SQL string equality.
- [ ] Live-LLM numbers are never mixed with deterministic fallback results.
- [ ] No claimed score appears in docs unless generated from the current release commit.

## Expected checks

```bash
python -m schemabridge.entrypoints.cli.main evaluate --output reports/evaluation.json
pytest tests/unit -k evaluation
pytest -m acceptance
make check
git diff --check
```

If a listed command contains a placeholder, determine and document the verified command rather than pretending the placeholder ran.

## Do not do

- Do not tune on hidden employer examples.
- Do not fabricate a large benchmark or statistically overstate the small synthetic set.

## Operator test to prepare

1. Run evaluation twice after a clean reset and compare deterministic outputs.
2. Inspect at least one false or difficult case rather than only aggregate scores.
3. Change a ground-truth expected row and confirm result correctness fails visibly.

Do not commit automatically unless the operator explicitly asks. End using `tasks/HANDOFF_TEMPLATE.md` and include a single proposed commit message.
