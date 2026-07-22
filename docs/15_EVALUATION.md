# Evaluation methodology

M15 evaluates only the checked-in synthetic SchemaBridge release-candidate fixtures. It is a
reproducibility harness, not a claim about production data, an employer benchmark, or general model
quality. The JSON report records the source revision or the explicit `working-tree-uncommitted`
state, a source fingerprint, every ground-truth version, and a combined fixture fingerprint.

## One-command deterministic run

From a prepared development environment, this resets only the M01 synthetic Compose volume and
then writes both report formats:

```bash
make evaluate
```

The equivalent non-resetting command uses the fixed public demo-reader default:

```bash
.venv/bin/python -m schemabridge.entrypoints.cli.main evaluate \
  --output reports/evaluation.json \
  --markdown examples/evaluation-report.md
```

An explicit `DATABASE_URL` overrides that default for a relocated copy of the same synthetic
reader. Evaluation never supplies a default credential to non-evaluation commands or other data.

`reports/` is ignored runtime output. The example Markdown report is judge-readable synthetic
evidence. Because this repository currently has no initial commit, generated scores are labeled as
an uncommitted working-tree snapshot and are not a release-commit claim. Regenerate both artifacts
after the reviewed release commit before quoting a score externally.

## Metric definitions

Every rate includes its numerator, denominator, evaluated cases, skipped cases, and failed cases.
The fixtures are too small for a meaningful bootstrap confidence interval, so the report contains
raw counts only and explicitly states that no confidence interval was calculated.

| Metric | Definition |
|---|---|
| Candidate precision | true positives / (true positives + false positives) at the configured deterministic recommendation threshold |
| Candidate recall | true positives / (true positives + false negatives) |
| Candidate F1 | `2 × true positives / (2 × true positives + false positives + false negatives)` |
| Candidate top-k recall | positive labels among the first k ranked cases / all positive labels |
| Join-path accuracy | query cases whose ordered approved contract IDs exactly equal ground truth / evaluated query cases |
| Cardinality accuracy | discovered contracts whose classified cardinality exactly equals ground truth / expected contracts |
| Intent equivalence | confirmed typed requests exactly equal the versioned request object / supported intent cases |
| Compile success | query cases reaching independently accepted final SQL / query cases |
| Execution success | compiled cases completing the bounded read-only preview and rejection inspection / query cases |
| Result correctness | cases whose type-tagged, column-keyed, order-independent row multisets exactly equal expected rows / executed cases |
| Safety rejection rate | malicious SQL cases rejected with the expected first stable guard code / security cases |
| Recipe reuse accuracy | current/stale compatibility cases matching expected status and staleness reasons / recipe cases |

Result comparison does not use SQL string equality. Values retain type tags, columns are keyed and
sorted, and complete rows are compared as multisets so duplicate rows remain observable.

## Failures, difficult cases, and thresholds

The semantic fixture intentionally retains one homonym false positive and one hidden-synonym false
negative. They remain detailed `observed` cases; no threshold is enforced because seven tuned
synthetic examples do not justify a release gate. Operational failures—wrong join/cardinality,
typed-intent mismatch, compile/preview failure, row mismatch, missed security rejection, or wrong
recipe compatibility—fail the deterministic run and cause the CLI to exit nonzero after writing the
report.

## Live LLM separation

The default run always records a deterministic, key-free evaluation plus a separate `live_llm`
entry marked `not_run`. `--intent-adapter live` adds a required, separately labeled live intent run;
it never replaces or merges with deterministic metrics and never falls back when credentials or a
model are unavailable. Live results remain provider/model observations for this tiny fixture, not
deterministic regression evidence.

## Operator mutation check

Copy `demo/ground_truth/query_cases.yml`, change one expected value, and point a temporary loader or
test fixture at the copy. Do not edit accepted ground truth in place. The affected result case must
be `failed`, the normalized expected/actual rows must both remain visible, the JSON must still be
written, and the command must exit nonzero.
