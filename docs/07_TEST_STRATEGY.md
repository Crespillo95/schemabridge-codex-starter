# Test strategy

## Test pyramid

### Unit tests

No network or containers. Cover:

- value-object validation;
- transformation semantics;
- candidate scoring;
- cardinality and fanout rules;
- analytical-request validation;
- query-plan resolution with fakes;
- deterministic SQL compilation;
- SQL guard policy;
- LLM structured-output validation;
- view-model formatting.

### Integration tests

Marked `integration`. Cover:

- PostgreSQL read-only enforcement and preview;
- compiler + parser + database execution;
- DataHub read adapter against local Core;
- DataHub mutation adapter with explicit approval;
- logical-model/document write and retrieval;
- SQLite draft storage;
- OpenAI adapter only when an opt-in API key is available; deterministic fixtures remain the CI path.

### Acceptance tests

Marked `acceptance`. Cover complete user-observable paths:

1. approve semantic mappings;
2. approve a join contract;
3. guided north-star query;
4. natural-language request produces the same typed plan;
5. preview returns the ground-truth rows;
6. rejected identifiers are visible;
7. context is published and reused;
8. malicious request cannot bypass safety.

## Ground truth

Versioned fixtures under `demo/ground_truth` define expected mappings, joins, request interpretations, rows, and rejection reasons. Evaluation code must never train or tune against hidden copies.

## Metrics

- candidate precision, recall, and F1;
- top-k field recall;
- join-path accuracy;
- cardinality accuracy;
- intent exact/semantic match;
- SQL compile success;
- SQL execution success;
- result-set correctness;
- security-case rejection rate;
- context-reuse rate;
- median and p95 demo latency where reproducible.

All reported values must come from a checked-in command and artifact. Do not invent targets as achieved results.

## Fixtures

- small deterministic tables;
- explicit invalid values;
- duplicate relationships to expose fanout;
- fake DataHub payloads recorded without secrets;
- fake LLM responses validated against schemas;
- frozen clocks/IDs when snapshots depend on them.

## Quality gate

`make check` is the base gate. Milestones add focused commands. Before release, run:

```bash
make check
pytest -m integration
pytest -m acceptance
python -m schemabridge.entrypoints.cli.main evaluate --output reports/evaluation.json
```

The exact final command may evolve, but documentation and CI must match the executable interface.
