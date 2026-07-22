# M06: Explainable semantic candidate engine

- Status: planned
- Timebox: 4 hours
- Recommended Codex: GPT-5.6 Sol — Extra High (xhigh)
- Dependencies: M02, M05

## Objective

Rank candidate physical fields for logical concepts using deterministic and semantic evidence while keeping approval separate from confidence.

## Why this milestone exists now

This is the core original capability that repairs weak context before query generation.

## Deliverables

- Implement candidate retrieval to avoid unrestricted all-pairs comparison.
- Implement configurable evidence signals for normalized names, descriptions/terms, type compatibility, value patterns, normalized overlap, lineage, and historical query usage.
- Produce a typed score breakdown, confidence, evidence, missing evidence, risks, and suggested transformation plan.
- Use an optional LLM only for bounded description interpretation/explanation; deterministic behavior and fake responses must remain testable.
- Create a labeled evaluation dataset from `semantic_mappings.yml` with negative/homonym cases.
- Report precision/recall/top-k metrics without presenting tuned fixtures as production evidence.

## Implementation sequence

1. Define signal interfaces and score configuration in the application/domain boundary.
2. Implement candidate blocking using tokens, types, glossary context, and scoped assets.
3. Implement deterministic signals first and calibrate with labeled fixtures.
4. Add semantic-description adapter behind a port only if it improves hard cases.
5. Generate human-readable explanations from the same score components.
6. Add negative tests where similar names mean different concepts and different names mean the same concept.

## Acceptance criteria

- [ ] `customer_id`, `client_no`, and `gf_customer_id` rank as candidates for `Customer.customer_key` with explainable evidence.
- [ ] Name similarity alone cannot cross the recommendation threshold.
- [ ] Unsafe float representation is surfaced as a risk and a transformation requirement.
- [ ] Scores are deterministic for deterministic adapters and all weights sum/normalize correctly.
- [ ] High confidence never sets status to approved.
- [ ] Evaluation output includes false positives and false negatives rather than hiding them.

## Required automated checks

```bash
pytest tests/unit -k "candidate or matching or scoring"
python -m schemabridge.entrypoints.cli.main candidates --concept Customer.customer_key --adapter recorded
make check
git diff --check
```

Replace angle-bracket placeholders only when the actual environment has established the command. Never report an unexecuted placeholder as passing.

## Manual test for the operator

1. Run candidate generation for Customer Identifier and inspect every signal for the top candidates.
2. Run a deliberately misleading same-name case and confirm the engine lowers confidence or requires review.
3. Change one description in a fixture and verify the score change is traceable to a named signal.

## Explicit non-goals

- Do not auto-approve or write candidates to DataHub.
- Do not build the final Streamlit review screen.

## Handoff requirements

Update `tasks/PROJECT_STATE.md`, `tasks/CURRENT_TASK.md`, and `tasks/DECISION_LOG.md`. Return the format in `tasks/HANDOFF_TEMPLATE.md`, including exact commands, results, limitations, and the proposed commit message.
