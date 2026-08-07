# M16: Ultra end-to-end architecture, security, integration, and submission audit

- Status: partial; development remediation verified, pending reviewed release commit and operator acceptance
- Timebox: 4 hours
- Recommended Codex: GPT-5.6 Sol — Ultra
- Dependencies: M00–M15

## Objective

Use bounded parallel subagents to audit the release candidate from independent perspectives, integrate only verified fixes, and prove a clean end-to-end build.

## Why this milestone exists now

This is the one milestone where Ultra and subagents materially improve quality because review work is independent and broad.

## Deliverables

- Run five read-heavy subagents: architecture/domain boundaries, SQL/database security, DataHub integration/governance, tests/evaluation, and judge experience/documentation.
- Require each subagent to return ranked findings with file evidence, reproduction steps, severity, and proposed tests; subagents must not edit overlapping files.
- Have the main agent deduplicate findings, challenge false positives, prioritize release blockers, and implement focused fixes sequentially.
- Add regression tests for every accepted correctness/security finding.
- Run a complete clean reset, install, DataHub ingest, acceptance flow, evaluation, UI smoke, and secret/license scan.
- Produce a release audit report and unresolved-risk register.

## Implementation sequence

1. Freeze feature scope and create a release-candidate commit before the audit.
2. Spawn the five specified subagents with no-write or non-overlapping review instructions and wait for all results.
3. Triage findings against accepted ADRs and reproduce each blocker.
4. Implement fixes one area at a time through the main thread, rerunning focused tests.
5. Run the full clean-room release script and manual north-star journey.
6. Write `reports/release-audit.md` with evidence, accepted risks, and final go/no-go.

## Acceptance criteria

- [x] All critical/high accepted findings are fixed or explicitly block release.
- [x] No domain/application dependency-direction violation remains.
- [x] All security-matrix cases pass at compiler, guard, and database layers as applicable.
- [x] DataHub read/write/reuse works from a clean reset or a documented verified fallback is honestly represented.
- [ ] Full deterministic evaluation and acceptance suites pass from the release commit.
- [ ] No secrets, proprietary data, broken links, missing license, or unverified submission claims remain.
- [x] The audit report lists unresolved medium/low risks without hiding them.

## Required automated checks

```bash
<clean-room release script created during this milestone>
make check
pytest -m integration
pytest -m acceptance
<evaluation command>
<secret/license/link scan commands>
git diff --check
```

Replace angle-bracket placeholders only when the actual environment has established the command. Never report an unexecuted placeholder as passing.

## Manual test for the operator

1. Run the full north-star journey from clean services and a clean browser while recording timing and any manual interventions.
2. Review the five subagent reports and the main triage; challenge at least one finding to ensure evidence quality.
3. Confirm the release-audit go/no-go decision before starting deployment.

## Explicit non-goals

- Do not add new product features.
- Do not let subagents make concurrent broad edits or merge unaudited suggestions.

## Handoff requirements

Update `tasks/PROJECT_STATE.md`, `tasks/CURRENT_TASK.md`, and `tasks/DECISION_LOG.md`. Return the format in `tasks/HANDOFF_TEMPLATE.md`, including exact commands, results, limitations, and the proposed commit message.
