# M02: Domain model and deterministic normalization

- Status: planned
- Timebox: 4 hours
- Recommended Codex: GPT-5.6 Sol — Extra High (xhigh)
- Dependencies: M00, M01

## Objective

Implement the pure typed domain for physical fields, logical concepts, mappings, transformations, decisions, joins, analytical requests, and validation results, with a fully tested identifier-normalization vertical slice.

## Why this milestone exists now

All independently developed modules need stable contracts and safety invariants before external integrations are introduced.

## Deliverables

- Create focused domain modules rather than one catch-all models file.
- Implement immutable Pydantic value objects and enums for the core concepts described in `docs/03_DOMAIN_MODEL.md`.
- Implement a closed transformation algebra and pure interpreter for padded string, integer, and float identifiers.
- Support leading-zero policies `preserve`, `strip`, and `pad_to_length` with explicit configuration.
- Return typed accepted/rejected outcomes with machine-readable rejection codes.
- Add exhaustive boundary tests and YAML round-trip tests for mapping plans.

## Implementation sequence

1. Design public domain types and invariants before implementation; update the domain document only when a justified decision changes.
2. Implement transformation step discriminated unions and validation.
3. Implement pure normalization without database or SQL imports.
4. Test strings, booleans, integers, decimals/floats, negative values, very large identifiers, empty values, Unicode whitespace, NaN, infinities, non-integral floats, and NULL.
5. Add serialization fixtures compatible with the ground-truth YAML.
6. Review dependency imports to enforce domain purity.

## Acceptance criteria

- [ ] `"00000000123"`, `123`, and `123.0` normalize to the approved canonical representation under strip policy.
- [ ] `123.5`, NaN, positive/negative infinity, booleans, and malformed strings are rejected without truncation.
- [ ] NULL behavior follows the transformation plan and is not conflated with invalid data.
- [ ] Padding/stripping behavior is explicit and tested, including the all-zero identifier.
- [ ] Domain modules import no external-system or presentation libraries.
- [ ] Typed mapping and join fixtures serialize and deserialize deterministically.

## Required automated checks

```bash
pytest tests/unit -k "normaliz or domain or transform"
ruff check src/schemabridge/domain tests/unit
mypy src/schemabridge/domain
make check
git diff --check
```

Replace angle-bracket placeholders only when the actual environment has established the command. Never report an unexecuted placeholder as passing.

## Manual test for the operator

1. Run the new CLI normalization demonstration on all valid and invalid fixture values.
2. Inspect the output and confirm each rejection includes a stable code and readable reason.
3. Review one serialized mapping file and verify that no executable code or raw SQL is embedded.

## Explicit non-goals

- Do not implement SQL compilation, DataHub, UI, or LLM integration.
- Do not add arbitrary transformation callbacks or expressions.

## Handoff requirements

Update `tasks/PROJECT_STATE.md`, `tasks/CURRENT_TASK.md`, and `tasks/DECISION_LOG.md`. Return the format in `tasks/HANDOFF_TEMPLATE.md`, including exact commands, results, limitations, and the proposed commit message.
