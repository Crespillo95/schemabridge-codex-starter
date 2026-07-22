# Synthetic north-star query validation report

Regenerated on 2026-07-22 from the hardened M16 workflow and the local synthetic PostgreSQL 16 service. This is demo evidence, not production evidence. The checked-in SQL is judge-readable output only; recipe reuse never executes this saved text.

## Generation commands

```bash
SCHEMABRIDGE_DRAFT_STORE_PATH=/private/tmp/schemabridge-m16-recipe-20260722.db DATABASE_URL=postgresql://schemabridge_reader:schemabridge_reader@127.0.0.1:55433/schemabridge .venv/bin/schemabridge workflow-demo --action start --workflow-id m16-recipe-artifact --json
SCHEMABRIDGE_DRAFT_STORE_PATH=/private/tmp/schemabridge-m16-recipe-20260722.db DATABASE_URL=postgresql://schemabridge_reader:schemabridge_reader@127.0.0.1:55433/schemabridge .venv/bin/schemabridge workflow-demo --action confirm-intent --workflow-id m16-recipe-artifact --json
SCHEMABRIDGE_DRAFT_STORE_PATH=/private/tmp/schemabridge-m16-recipe-20260722.db DATABASE_URL=postgresql://schemabridge_reader:schemabridge_reader@127.0.0.1:55433/schemabridge .venv/bin/schemabridge workflow-demo --action approve-execution --workflow-id m16-recipe-artifact --json
SCHEMABRIDGE_DRAFT_STORE_PATH=/private/tmp/schemabridge-m16-recipe-20260722.db .venv/bin/schemabridge recipe-show --workflow-id m16-recipe-artifact --adapter fake --json
```

## Observed validation

- SQL policy: `accepted`; no findings.
- Query fingerprint: `b81cc0de555200592339e36ffcfd78a237cda32ccf32a7ec58e3327735b84066`.
- Preview identity: `schemabridge_reader`; transaction read-only: `true`; timeout: `5000 ms`.
- Exact synthetic rows: `(2026-01-01, 2)`, `(2026-01-02, 1)`, `(2026-01-03, 1)`.
- Rejections: `127.5 → non_integral_identifier`, `NaN → non_finite_identifier`, `NULL → null_join_key`.
- Fanout mitigation: approved `customer_to_account_holder` v1 requires `COUNT DISTINCT Customer.customer_key`.
- Recipe publication is not performed while regenerating this SQL-free review artifact. Explicit approval remains required, and the live DataHub document round trip is covered separately by the integration suite.

## Safe reuse behavior

A repeated request retrieves only typed recipe context and provenance. The current workflow still resolves current approved mappings and joins, compiles from the typed IR, reparses the final SQL through the independent policy guard, requests execution approval, and runs a bounded read-only preview. Stored SQL is never an execution input.
