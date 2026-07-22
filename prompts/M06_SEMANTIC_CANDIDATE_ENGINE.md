# Codex milestone M06: Explainable semantic candidate engine

Use the `$schemabridge-milestone` skill. Work only on **M06**. Recommended setting: **GPT-5.6 Sol — Extra High (xhigh)**.

## Read first

1. `AGENTS.md` and all nested `AGENTS.md` files governing files you may edit.
2. `docs/00_PRODUCT_VISION.md`
3. `docs/01_SCOPE.md`
4. `docs/02_ARCHITECTURE.md`
5. `docs/06_SECURITY.md`
6. `plans/M06_SEMANTIC_CANDIDATE_ENGINE.md`
7. `tasks/PROJECT_STATE.md`
8. `tasks/DECISION_LOG.md`

## Objective

Rank candidate physical fields for logical concepts using deterministic and semantic evidence while keeping approval separate from confidence.

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

- Implement candidate retrieval to avoid unrestricted all-pairs comparison.
- Implement configurable evidence signals for normalized names, descriptions/terms, type compatibility, value patterns, normalized overlap, lineage, and historical query usage.
- Produce a typed score breakdown, confidence, evidence, missing evidence, risks, and suggested transformation plan.
- Use an optional LLM only for bounded description interpretation/explanation; deterministic behavior and fake responses must remain testable.
- Create a labeled evaluation dataset from `semantic_mappings.yml` with negative/homonym cases.
- Report precision/recall/top-k metrics without presenting tuned fixtures as production evidence.

## Acceptance criteria

- [ ] `customer_id`, `client_no`, and `gf_customer_id` rank as candidates for `Customer.customer_key` with explainable evidence.
- [ ] Name similarity alone cannot cross the recommendation threshold.
- [ ] Unsafe float representation is surfaced as a risk and a transformation requirement.
- [ ] Scores are deterministic for deterministic adapters and all weights sum/normalize correctly.
- [ ] High confidence never sets status to approved.
- [ ] Evaluation output includes false positives and false negatives rather than hiding them.

## Expected checks

```bash
pytest tests/unit -k "candidate or matching or scoring"
python -m schemabridge.entrypoints.cli.main candidates --concept Customer.customer_key --adapter recorded
make check
git diff --check
```

If a listed command contains a placeholder, determine and document the verified command rather than pretending the placeholder ran.

## Do not do

- Do not auto-approve or write candidates to DataHub.
- Do not build the final Streamlit review screen.

## Operator test to prepare

1. Run candidate generation for Customer Identifier and inspect every signal for the top candidates.
2. Run a deliberately misleading same-name case and confirm the engine lowers confidence or requires review.
3. Change one description in a fixture and verify the score change is traceable to a named signal.

Do not commit automatically unless the operator explicitly asks. End using `tasks/HANDOFF_TEMPLATE.md` and include a single proposed commit message.
