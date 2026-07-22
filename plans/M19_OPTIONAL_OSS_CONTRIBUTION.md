# M19: Optional meaningful DataHub open-source contribution

- Status: planned
- Timebox: 3 hours
- Recommended Codex: GPT-5.6 Sol — Extra High (xhigh)
- Dependencies: M18 accepted; outside critical path

## Objective

Contribute one focused, useful improvement to the DataHub ecosystem without jeopardizing the submission.

## Why this milestone exists now

The hackathon includes favorable consideration for meaningful open-source contributions, but the core project remains the priority.

## Deliverables

- Select one verified pain point encountered during development: a DataHub Skill for governed join analysis, a documentation fix, a reproducible bug fix, or a concise RFC.
- Confirm contribution guidelines, issue relevance, ownership, tests, and licensing before work.
- Create the smallest complete contribution with tests/examples and clear upstream value independent of SchemaBridge promotion.
- Open an issue/PR or publish the contribution through the appropriate official channel.
- Link the contribution in the submission only if it is public and materially complete.
- Record feedback for the hackathon survey separately where applicable.

## Implementation sequence

1. Review the development log for repeated DataHub friction backed by evidence.
2. Choose one contribution and validate it is not duplicate/out of scope upstream.
3. Read the upstream repository instructions and create an isolated branch/fork.
4. Implement, test, document, and self-review the contribution.
5. Submit it with a concise problem statement and reproduction/use case.
6. Update SchemaBridge disclosure/submission links only after publication.

## Acceptance criteria

- [ ] The contribution solves a real DataHub user/developer problem and follows upstream rules.
- [ ] It includes tests or verifiable examples appropriate to its type.
- [ ] It is independently useful and not merely a link to SchemaBridge.
- [ ] No core release code or deployment is destabilized.
- [ ] The public issue/PR/RFC accurately states status and limitations.
- [ ] Any submission bonus claim links to the public artifact.

## Required automated checks

```bash
<upstream repository setup and test commands>
<SchemaBridge release smoke check>
git diff --check
```

Replace angle-bracket placeholders only when the actual environment has established the command. Never report an unexecuted placeholder as passing.

## Manual test for the operator

1. Read the public contribution as an upstream maintainer and confirm the problem and verification steps are clear.
2. Run all upstream-required checks.
3. Confirm the main SchemaBridge release remains unchanged and available.

## Explicit non-goals

- Do not start M19 before M18 is complete.
- Do not create a superficial contribution solely to claim a bonus.

## Handoff requirements

Update `tasks/PROJECT_STATE.md`, `tasks/CURRENT_TASK.md`, and `tasks/DECISION_LOG.md`. Return the format in `tasks/HANDOFF_TEMPLATE.md`, including exact commands, results, limitations, and the proposed commit message.
