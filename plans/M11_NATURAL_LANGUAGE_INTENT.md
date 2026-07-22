# M11: Natural-language intent resolver

- Status: automated complete; operator acceptance pending
- Timebox: 3 hours
- Recommended Codex: GPT-5.6 Sol — Extra High (xhigh)
- Dependencies: M09, M10

## Objective

Translate business-language requests into the existing typed AnalyticalRequest with explicit ambiguities, alternatives, and schema validation, never executable SQL.

## Why this milestone exists now

The user should be able to describe the problem naturally while all trusted behavior remains in the governed planner.

## Deliverables

- Define an `IntentParserPort` with live and deterministic fake implementations.
- Implement structured-output parsing constrained to approved logical models, fields, enum operations, and current user language.
- Provide only the minimum catalog vocabulary and definitions needed for the request, not raw credentials or unrestricted samples.
- Implement ambiguity detection and alternatives for count-vs-list, distinct-vs-relationship count, date meaning, and unknown role values.
- Add prompt-injection and hallucinated-field tests; invalid model output must fail validation and never reach the planner.
- Prove the Spanish north-star request resolves to the same typed request as guided mode after confirmation.

## Implementation sequence

1. Finalize the parser contract and deterministic fixture adapter.
2. Design a short system/developer prompt with explicit output schema and untrusted-input boundaries.
3. Implement the live adapter using the configured provider/model without coupling the domain to OpenAI.
4. Validate and normalize output through Pydantic/domain constructors.
5. Add ambiguity and injection fixtures in Spanish and English.
6. Compare guided and natural-language plan fingerprints in acceptance tests.

## Acceptance criteria

- [ ] The LLM adapter cannot return or execute SQL through its output schema.
- [ ] The Spanish north-star phrase produces the expected entities, dimension, metric, filter, and required confirmation/assumption.
- [ ] Hallucinated logical fields, unsupported operators, and raw SQL payloads are rejected before planning.
- [ ] Prompt-injection text is treated as business input and cannot change tool, approval, or safety policy.
- [ ] Deterministic fake parsing makes CI and the demo fallback independent of an API key.
- [ ] After confirmation, guided and natural-language modes produce equivalent request and plan fingerprints.

## Required automated checks

```bash
python -m pip install -e ".[dev,llm]"
pytest tests/unit -k "intent or language or prompt"
pytest -m acceptance -k natural_language
make check
git diff --check
```

Replace angle-bracket placeholders only when the actual environment has established the command. Never report an unexecuted placeholder as passing.

## Manual test for the operator

1. Enter the Spanish north-star request and inspect the interpretation before confirming.
2. Enter “agrupa clientes” and verify alternatives are shown rather than a silent guess.
3. Enter an injection-style request asking to drop a table or ignore rules and confirm no SQL is generated/executed.

## Explicit non-goals

- Do not implement multi-turn conversational memory or chart generation.
- Do not give the LLM direct database, DataHub-write, or SQL-execution tools.

## Handoff requirements

Update `tasks/PROJECT_STATE.md`, `tasks/CURRENT_TASK.md`, and `tasks/DECISION_LOG.md`. Return the format in `tasks/HANDOFF_TEMPLATE.md`, including exact commands, results, limitations, and the proposed commit message.
