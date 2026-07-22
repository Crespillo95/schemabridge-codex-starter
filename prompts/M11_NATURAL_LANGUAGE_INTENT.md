# Codex milestone M11: Natural-language intent resolver

Use the `$schemabridge-milestone` skill. Work only on **M11**. Recommended setting: **GPT-5.6 Sol — Extra High (xhigh)**.

## Read first

1. `AGENTS.md` and all nested `AGENTS.md` files governing files you may edit.
2. `docs/00_PRODUCT_VISION.md`
3. `docs/01_SCOPE.md`
4. `docs/02_ARCHITECTURE.md`
5. `docs/06_SECURITY.md`
6. `plans/M11_NATURAL_LANGUAGE_INTENT.md`
7. `tasks/PROJECT_STATE.md`
8. `tasks/DECISION_LOG.md`

## Objective

Translate business-language requests into the existing typed AnalyticalRequest with explicit ambiguities, alternatives, and schema validation, never executable SQL.

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

- Define an `IntentParserPort` with live and deterministic fake implementations.
- Implement structured-output parsing constrained to approved logical models, fields, enum operations, and current user language.
- Provide only the minimum catalog vocabulary and definitions needed for the request, not raw credentials or unrestricted samples.
- Implement ambiguity detection and alternatives for count-vs-list, distinct-vs-relationship count, date meaning, and unknown role values.
- Add prompt-injection and hallucinated-field tests; invalid model output must fail validation and never reach the planner.
- Prove the Spanish north-star request resolves to the same typed request as guided mode after confirmation.

## Acceptance criteria

- [ ] The LLM adapter cannot return or execute SQL through its output schema.
- [ ] The Spanish north-star phrase produces the expected entities, dimension, metric, filter, and required confirmation/assumption.
- [ ] Hallucinated logical fields, unsupported operators, and raw SQL payloads are rejected before planning.
- [ ] Prompt-injection text is treated as business input and cannot change tool, approval, or safety policy.
- [ ] Deterministic fake parsing makes CI and the demo fallback independent of an API key.
- [ ] After confirmation, guided and natural-language modes produce equivalent request and plan fingerprints.

## Expected checks

```bash
python -m pip install -e ".[dev,llm]"
pytest tests/unit -k "intent or language or prompt"
pytest -m acceptance -k natural_language
make check
git diff --check
```

If a listed command contains a placeholder, determine and document the verified command rather than pretending the placeholder ran.

## Do not do

- Do not implement multi-turn conversational memory or chart generation.
- Do not give the LLM direct database, DataHub-write, or SQL-execution tools.

## Operator test to prepare

1. Enter the Spanish north-star request and inspect the interpretation before confirming.
2. Enter “agrupa clientes” and verify alternatives are shown rather than a silent guess.
3. Enter an injection-style request asking to drop a table or ignore rules and confirm no SQL is generated/executed.

Do not commit automatically unless the operator explicitly asks. End using `tasks/HANDOFF_TEMPLATE.md` and include a single proposed commit message.
