# Generated examples

M13 now includes initial judge-readable output from an actual guarded execution against the local synthetic PostgreSQL demo. M18 must regenerate the complete release artifact set from the release commit.

Current M13 artifacts:

- `query-recipe-secondary-holders.yml`: typed, SQL-free recipe payload.
- `generated-secondary-holders.sql`: parameterized compiler output for inspection only.
- `query-validation-report.md`: commands, synthetic rows, rejection evidence, and safe-reuse behavior.

Expected final artifacts:

```text
logical-model-customer.yml
column-mapping-customer-key.yml
join-contract-customer-account-holder.yml
analytical-request-secondary-holders.yml
resolved-query-plan-secondary-holders.yml
generated-secondary-holders.sql
query-validation-report.md
rejected-records.csv
datahub-writeback-summary.md
evaluation-summary.md
```

Rules:

- Every example must be generated or exported by the final implementation.
- Use synthetic data only.
- Record the release commit and generation command.
- Never hand-edit an example to look better than application output.
- Never execute the checked-in SQL as a recipe; replan, compile, guard, and preview through the application.
