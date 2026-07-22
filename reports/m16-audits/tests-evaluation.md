# M16 independent audit: tests and evaluation

Mode: bounded read-only review; the subagent made no edits.
Snapshot: pre-fix M16 working tree, 2026-07-21.
Owner of dispositions: main M16 agent.

## Ranked findings

### TE-001 — High — CI could skip database-backed acceptance/evaluation evidence

- Evidence: the workflow did not provide both canonical DSNs and did not assert the complete
  database-backed evaluation path.
- Reproduction: inspect `.github/workflows/ci.yml`, marker selection, and skip reasons.
- Risk: green CI could omit the release's central read-only execution proof.
- Proposed regression: export both DSNs, run Make integration/acceptance targets, run evaluation,
  and assert its result.
- Disposition: accepted and fixed at `.github/workflows/ci.yml:26-42`; local equivalents passed.

### TE-002 — High — No release revision exists

- Evidence: `git rev-parse --verify HEAD` fails and all candidate files are untracked.
- Reproduction: `git log -1 --oneline; git status --short`.
- Risk: tests cannot be tied to an immutable source revision or recreated from a clean checkout.
- Proposed regression: strict release commands require a clean `HEAD` and artifacts record it.
- Disposition: accepted as critical release blocker `RC-001`; strict commands fail closed.

### TE-003 — Medium — Result-correctness denominator includes non-executed cases

- Evidence: the report's result-correctness case count spans cases that can fail before execution.
- Reproduction: force one compile failure and inspect numerator/denominator/failed-case detail.
- Risk: the metric label can be interpreted more broadly than executed comparisons.
- Proposed regression: separate eligible, executed, and correct counts.
- Disposition: retained; failures/skips remain visible and blocking, so no result is fabricated.

### TE-004 — Medium — Zero denominators fail before a report is emitted

- Evidence: metric construction assumes the checked-in fixture contains eligible cases.
- Reproduction: use a valid empty/fully skipped fixture and run evaluation.
- Risk: a boundary fixture produces an exception rather than a typed undefined metric.
- Proposed regression: emit raw zero counts plus an explicit undefined/skipped status.
- Disposition: retained; current versioned denominators are nonzero.

### TE-005 — Medium — Configured coverage threshold is not a release gate

- Evidence: `pyproject.toml` configures coverage behavior, but `make check` does not execute the
  coverage target and observed aggregate is below the nominal threshold.
- Reproduction: compare `make check` and `make coverage` behavior.
- Risk: documentation could overstate coverage enforcement.
- Proposed regression: add failure-path tests before enabling a justified threshold in CI.
- Disposition: retained and explicitly not claimed as a passing coverage gate.

### TE-006 — Medium — Dependency ranges are not locked

- Evidence: `pyproject.toml:13-44` uses bounded ranges; a fresh solve installed Streamlit 1.60.0
  rather than the earlier observed 1.59.2.
- Reproduction: recreate `.venv` at different index snapshots and compare installed versions.
- Risk: clean builds can drift inside allowed ranges.
- Proposed regression: generate/review constraints or a lock and record exact installed versions.
- Disposition: retained; the M16 runbook/audit records the actual solve and license inventory.

### TE-007 — Medium — Audit artifacts could be silently ignored

- Evidence: the `reports/` directory was broadly ignored before an explicit exception existed.
- Reproduction: `git check-ignore -v reports/release-audit.md`.
- Risk: mandatory audit evidence would not enter the release candidate.
- Proposed regression: explicit allowlist plus candidate-file scan.
- Disposition: accepted and fixed for the release report and five panel reports.

### TE-008 — Low — Duplicate result columns can collapse during normalization

- Evidence: column-keyed normalization would overwrite a duplicate name.
- Reproduction: pass a result with duplicate aliases to the model-level normalizer.
- Risk: one cell could disappear in an adversarial/malformed result.
- Proposed regression: enforce unique columns at the result model boundary.
- Disposition: retained; the SQL guard currently rejects duplicate aliases.

## Conclusion

Local clean-state unit/integration/acceptance/evaluation paths all passed, and CI now invokes their
equivalents. The missing release revision remains a blocker; raw-count metric limitations remain
visible rather than statistically overstated.
