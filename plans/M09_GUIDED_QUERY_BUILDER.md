# M09: Guided analytical request builder

- Status: planned
- Timebox: 2 hours
- Recommended Codex: GPT-5.6 Sol — High
- Dependencies: M02, M07, M08

## Objective

Create a guided, LLM-free way to express an analytical request using only approved logical concepts and operations.

## Why this milestone exists now

Natural language must be an optional translation layer over a proven typed request workflow, not the foundation of query behavior.

## Deliverables

- Complete the `AnalyticalRequest` model and validation rules for primary entity, dimensions, metrics, filters, date grain, order, and limit.
- Expose a CLI or minimal UI builder populated from approved logical context.
- Implement supported operators and aggregations as closed enums.
- Detect missing concepts, incompatible aggregates/types, and unresolved ambiguities before planning.
- Create the exact north-star request fixture and a no-join control request.
- Add serialization and view-model tests.

## Implementation sequence

1. Reconcile request types with M03 query IR and M08 contracts.
2. Implement validation for field roles and operation/type compatibility.
3. Build a guided input adapter that cannot submit unknown identifiers.
4. Render a confirmation summary independent of SQL.
5. Add fixtures for the north-star and active-customers-by-country requests.
6. Hand the typed request to a fake planner port to prove the seam.

## Acceptance criteria

- [ ] The guided builder creates the north-star request without raw SQL or physical field input.
- [ ] Users can select only approved logical fields and supported operations.
- [ ] Invalid `SUM(customer_key)`, unknown filters, and unsupported grains are rejected with actionable messages.
- [ ] The request serializes deterministically and is independent of Streamlit session internals.
- [ ] The no-join control request clearly requires only Customer.
- [ ] No LLM SDK is required to run the builder tests.

## Required automated checks

```bash
pytest tests/unit -k "analytical_request or guided"
python -m schemabridge.entrypoints.cli.main request-demo
make check
git diff --check
```

Replace angle-bracket placeholders only when the actual environment has established the command. Never report an unexecuted placeholder as passing.

## Manual test for the operator

1. Build the north-star request through the guided interface and compare its YAML/JSON with ground truth.
2. Try an invalid aggregate and confirm the UI explains why it is unsupported.
3. Reload the draft and verify the same typed request is restored.

## Explicit non-goals

- Do not resolve physical datasets or execute SQL.
- Do not add natural-language parsing.

## Handoff requirements

Update `tasks/PROJECT_STATE.md`, `tasks/CURRENT_TASK.md`, and `tasks/DECISION_LOG.md`. Return the format in `tasks/HANDOFF_TEMPLATE.md`, including exact commands, results, limitations, and the proposed commit message.
