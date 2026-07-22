# Codex milestone M02: Domain model and deterministic normalization

Use the `$schemabridge-milestone` skill. Work only on **M02**. Recommended setting: **GPT-5.6 Sol — Extra High (xhigh)**.

## Read first

1. `AGENTS.md` and all nested `AGENTS.md` files governing files you may edit.
2. `docs/00_PRODUCT_VISION.md`
3. `docs/01_SCOPE.md`
4. `docs/02_ARCHITECTURE.md`
5. `docs/06_SECURITY.md`
6. `plans/M02_DOMAIN_AND_NORMALIZATION.md`
7. `tasks/PROJECT_STATE.md`
8. `tasks/DECISION_LOG.md`

## Objective

Implement the pure typed domain for physical fields, logical concepts, mappings, transformations, decisions, joins, analytical requests, and validation results, with a fully tested identifier-normalization vertical slice.

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

- Create focused domain modules rather than one catch-all models file.
- Implement immutable Pydantic value objects and enums for the core concepts described in `docs/03_DOMAIN_MODEL.md`.
- Implement a closed transformation algebra and pure interpreter for padded string, integer, and float identifiers.
- Support leading-zero policies `preserve`, `strip`, and `pad_to_length` with explicit configuration.
- Return typed accepted/rejected outcomes with machine-readable rejection codes.
- Add exhaustive boundary tests and YAML round-trip tests for mapping plans.

## Acceptance criteria

- [ ] `"00000000123"`, `123`, and `123.0` normalize to the approved canonical representation under strip policy.
- [ ] `123.5`, NaN, positive/negative infinity, booleans, and malformed strings are rejected without truncation.
- [ ] NULL behavior follows the transformation plan and is not conflated with invalid data.
- [ ] Padding/stripping behavior is explicit and tested, including the all-zero identifier.
- [ ] Domain modules import no external-system or presentation libraries.
- [ ] Typed mapping and join fixtures serialize and deserialize deterministically.

## Expected checks

```bash
pytest tests/unit -k "normaliz or domain or transform"
ruff check src/schemabridge/domain tests/unit
mypy src/schemabridge/domain
make check
git diff --check
```

If a listed command contains a placeholder, determine and document the verified command rather than pretending the placeholder ran.

## Do not do

- Do not implement SQL compilation, DataHub, UI, or LLM integration.
- Do not add arbitrary transformation callbacks or expressions.

## Operator test to prepare

1. Run the new CLI normalization demonstration on all valid and invalid fixture values.
2. Inspect the output and confirm each rejection includes a stable code and readable reason.
3. Review one serialized mapping file and verify that no executable code or raw SQL is embedded.

Do not commit automatically unless the operator explicitly asks. End using `tasks/HANDOFF_TEMPLATE.md` and include a single proposed commit message.
