# Codex milestone M16: Ultra end-to-end architecture, security, integration, and submission audit

Use the `$schemabridge-milestone` skill. Work only on **M16**. Recommended setting: **GPT-5.6 Sol — Ultra**.

## Read first

1. `AGENTS.md` and all nested `AGENTS.md` files governing files you may edit.
2. `docs/00_PRODUCT_VISION.md`
3. `docs/01_SCOPE.md`
4. `docs/02_ARCHITECTURE.md`
5. `docs/06_SECURITY.md`
6. `plans/M16_END_TO_END_HARDENING.md`
7. `tasks/PROJECT_STATE.md`
8. `tasks/DECISION_LOG.md`

## Objective

Use bounded parallel subagents to audit the release candidate from independent perspectives, integrate only verified fixes, and prove a clean end-to-end build.

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

- Run five read-heavy subagents: architecture/domain boundaries, SQL/database security, DataHub integration/governance, tests/evaluation, and judge experience/documentation.
- Require each subagent to return ranked findings with file evidence, reproduction steps, severity, and proposed tests; subagents must not edit overlapping files.
- Have the main agent deduplicate findings, challenge false positives, prioritize release blockers, and implement focused fixes sequentially.
- Add regression tests for every accepted correctness/security finding.
- Run a complete clean reset, install, DataHub ingest, acceptance flow, evaluation, UI smoke, and secret/license scan.
- Produce a release audit report and unresolved-risk register.

## Acceptance criteria

- [ ] All critical/high accepted findings are fixed or explicitly block release.
- [ ] No domain/application dependency-direction violation remains.
- [ ] All security-matrix cases pass at compiler, guard, and database layers as applicable.
- [ ] DataHub read/write/reuse works from a clean reset or a documented verified fallback is honestly represented.
- [ ] Full deterministic evaluation and acceptance suites pass from the release commit.
- [ ] No secrets, proprietary data, broken links, missing license, or unverified submission claims remain.
- [ ] The audit report lists unresolved medium/low risks without hiding them.

## Expected checks

```bash
<clean-room release script created during this milestone>
make check
pytest -m integration
pytest -m acceptance
<evaluation command>
<secret/license/link scan commands>
git diff --check
```

If a listed command contains a placeholder, determine and document the verified command rather than pretending the placeholder ran.

## Do not do

- Do not add new product features.
- Do not let subagents make concurrent broad edits or merge unaudited suggestions.

## Operator test to prepare

1. Run the full north-star journey from clean services and a clean browser while recording timing and any manual interventions.
2. Review the five subagent reports and the main triage; challenge at least one finding to ensure evidence quality.
3. Confirm the release-audit go/no-go decision before starting deployment.

Do not commit automatically unless the operator explicitly asks. End using `tasks/HANDOFF_TEMPLATE.md` and include a single proposed commit message.
