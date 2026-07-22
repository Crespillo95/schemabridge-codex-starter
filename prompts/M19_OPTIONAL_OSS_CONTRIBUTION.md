# Codex milestone M19: Optional meaningful DataHub open-source contribution

Use the `$schemabridge-milestone` skill. Work only on **M19**. Recommended setting: **GPT-5.6 Sol — Extra High (xhigh)**.

## Read first

1. `AGENTS.md` and all nested `AGENTS.md` files governing files you may edit.
2. `docs/00_PRODUCT_VISION.md`
3. `docs/01_SCOPE.md`
4. `docs/02_ARCHITECTURE.md`
5. `docs/06_SECURITY.md`
6. `plans/M19_OPTIONAL_OSS_CONTRIBUTION.md`
7. `tasks/PROJECT_STATE.md`
8. `tasks/DECISION_LOG.md`

## Objective

Contribute one focused, useful improvement to the DataHub ecosystem without jeopardizing the submission.

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

- Select one verified pain point encountered during development: a DataHub Skill for governed join analysis, a documentation fix, a reproducible bug fix, or a concise RFC.
- Confirm contribution guidelines, issue relevance, ownership, tests, and licensing before work.
- Create the smallest complete contribution with tests/examples and clear upstream value independent of SchemaBridge promotion.
- Open an issue/PR or publish the contribution through the appropriate official channel.
- Link the contribution in the submission only if it is public and materially complete.
- Record feedback for the hackathon survey separately where applicable.

## Acceptance criteria

- [ ] The contribution solves a real DataHub user/developer problem and follows upstream rules.
- [ ] It includes tests or verifiable examples appropriate to its type.
- [ ] It is independently useful and not merely a link to SchemaBridge.
- [ ] No core release code or deployment is destabilized.
- [ ] The public issue/PR/RFC accurately states status and limitations.
- [ ] Any submission bonus claim links to the public artifact.

## Expected checks

```bash
<upstream repository setup and test commands>
<SchemaBridge release smoke check>
git diff --check
```

If a listed command contains a placeholder, determine and document the verified command rather than pretending the placeholder ran.

## Do not do

- Do not start M19 before M18 is complete.
- Do not create a superficial contribution solely to claim a bonus.

## Operator test to prepare

1. Read the public contribution as an upstream maintainer and confirm the problem and verification steps are clear.
2. Run all upstream-required checks.
3. Confirm the main SchemaBridge release remains unchanged and available.

Do not commit automatically unless the operator explicitly asks. End using `tasks/HANDOFF_TEMPLATE.md` and include a single proposed commit message.
