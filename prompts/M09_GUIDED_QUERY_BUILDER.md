# Codex milestone M09: Guided analytical request builder

Use the `$schemabridge-milestone` skill. Work only on **M09**. Recommended setting: **GPT-5.6 Sol — High**.

## Read first

1. `AGENTS.md` and all nested `AGENTS.md` files governing files you may edit.
2. `docs/00_PRODUCT_VISION.md`
3. `docs/01_SCOPE.md`
4. `docs/02_ARCHITECTURE.md`
5. `docs/06_SECURITY.md`
6. `plans/M09_GUIDED_QUERY_BUILDER.md`
7. `tasks/PROJECT_STATE.md`
8. `tasks/DECISION_LOG.md`

## Objective

Create a guided, LLM-free way to express an analytical request using only approved logical concepts and operations.

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

- Complete the `AnalyticalRequest` model and validation rules for primary entity, dimensions, metrics, filters, date grain, order, and limit.
- Expose a CLI or minimal UI builder populated from approved logical context.
- Implement supported operators and aggregations as closed enums.
- Detect missing concepts, incompatible aggregates/types, and unresolved ambiguities before planning.
- Create the exact north-star request fixture and a no-join control request.
- Add serialization and view-model tests.

## Acceptance criteria

- [ ] The guided builder creates the north-star request without raw SQL or physical field input.
- [ ] Users can select only approved logical fields and supported operations.
- [ ] Invalid `SUM(customer_key)`, unknown filters, and unsupported grains are rejected with actionable messages.
- [ ] The request serializes deterministically and is independent of Streamlit session internals.
- [ ] The no-join control request clearly requires only Customer.
- [ ] No LLM SDK is required to run the builder tests.

## Expected checks

```bash
pytest tests/unit -k "analytical_request or guided"
python -m schemabridge.entrypoints.cli.main request-demo
make check
git diff --check
```

If a listed command contains a placeholder, determine and document the verified command rather than pretending the placeholder ran.

## Do not do

- Do not resolve physical datasets or execute SQL.
- Do not add natural-language parsing.

## Operator test to prepare

1. Build the north-star request through the guided interface and compare its YAML/JSON with ground truth.
2. Try an invalid aggregate and confirm the UI explains why it is unsupported.
3. Reload the draft and verify the same typed request is restored.

Do not commit automatically unless the operator explicitly asks. End using `tasks/HANDOFF_TEMPLATE.md` and include a single proposed commit message.
