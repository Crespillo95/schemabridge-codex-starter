# M15: Reproducible evaluation harness and evidence

- Status: planned
- Timebox: 3 hours
- Recommended Codex: GPT-5.6 Sol — High
- Dependencies: M06, M08, M10, M11, M13

## Objective

Measure semantic matching, join reasoning, request interpretation, SQL execution, result correctness, safety rejection, and context reuse with reproducible artifacts.

## Why this milestone exists now

Real-world usefulness and technical execution are stronger when claims are backed by a command and checked-in ground truth.

## Deliverables

- Implement a CLI evaluation command that consumes versioned ground truth and emits JSON plus Markdown summaries.
- Measure candidate precision/recall/F1/top-k, join path/cardinality accuracy, intent equivalence, compile/execution success, result correctness, safety rejection rate, and recipe reuse.
- Keep deterministic and optional live-LLM evaluations separate and labeled.
- Add bootstrap confidence intervals or case counts only when statistically meaningful; otherwise report raw counts honestly.
- Generate `reports/evaluation.json` and a judge-readable example report from the release candidate.
- Add regression thresholds only after baseline measurements are real and justified.

## Implementation sequence

1. Define metric formulas and matching semantics in documentation before coding.
2. Implement dataset loaders and evaluator interfaces.
3. Add each metric incrementally with unit tests for edge cases and denominators.
4. Run the full deterministic suite and inspect false-positive/negative details.
5. Add optional live-LLM mode guarded by environment/key and excluded from CI.
6. Update README/submission evidence only with generated results.

## Acceptance criteria

- [ ] One documented command reproduces all deterministic metrics from a clean seeded environment.
- [ ] Metric denominators, skipped cases, and failures are visible.
- [ ] False positives/negatives and rejected cases are retained in detailed output.
- [ ] Result correctness compares normalized rows, not just SQL string equality.
- [ ] Live-LLM numbers are never mixed with deterministic fallback results.
- [ ] No claimed score appears in docs unless generated from the current release commit.

## Required automated checks

```bash
python -m schemabridge.entrypoints.cli.main evaluate --output reports/evaluation.json
pytest tests/unit -k evaluation
pytest -m acceptance
make check
git diff --check
```

Replace angle-bracket placeholders only when the actual environment has established the command. Never report an unexecuted placeholder as passing.

## Manual test for the operator

1. Run evaluation twice after a clean reset and compare deterministic outputs.
2. Inspect at least one false or difficult case rather than only aggregate scores.
3. Change a ground-truth expected row and confirm result correctness fails visibly.

## Explicit non-goals

- Do not tune on hidden employer examples.
- Do not fabricate a large benchmark or statistically overstate the small synthetic set.

## Handoff requirements

Update `tasks/PROJECT_STATE.md`, `tasks/CURRENT_TASK.md`, and `tasks/DECISION_LOG.md`. Return the format in `tasks/HANDOFF_TEMPLATE.md`, including exact commands, results, limitations, and the proposed commit message.
