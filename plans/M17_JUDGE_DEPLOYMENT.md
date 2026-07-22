# M17: Stable judge deployment and operations

- Status: planned
- Timebox: 3 hours
- Recommended Codex: GPT-5.6 Sol — Extra High (xhigh)
- Dependencies: M16

## Objective

Deploy a free, stable, honest judge-accessible application and document the full local DataHub path and any hosted fallback mode.

## Why this milestone exists now

Judges need easy access, but deployment constraints must not cause misleading claims or destabilize the verified release.

## Deliverables

- Research and select the current hosting topology based on real resource limits, sleep behavior, secrets, public access, and DataHub feasibility.
- Package the release with deterministic demo reset and health/status indicators.
- Deploy the UI and required services or a clearly labeled recorded-context fallback while preserving the full local integration path.
- Configure scoped secrets/service accounts and verify no admin credentials are exposed.
- Add uptime/cold-start instructions, troubleshooting, and a rollback/redeploy path.
- Test the public URL from incognito and a separate network/device.

## Implementation sequence

1. Compare current hosting options using official documentation and record the decision in an ADR.
2. Containerize/package only what the chosen platform needs; avoid architecture rewrites.
3. Configure secrets and least-privilege demo identities.
4. Deploy, seed/reset, and run the full north-star test publicly.
5. Test service restart/cold start and failure labeling.
6. Update README testing instructions and release URLs.

## Acceptance criteria

- [ ] The public URL works without payment or hidden operator steps for judges.
- [ ] Live versus recorded/fake DataHub and LLM modes are unambiguously labeled.
- [ ] The public path produces the expected north-star result and safety/rejection evidence.
- [ ] Secrets are stored outside Git and scoped to demo assets.
- [ ] A clean deploy from the release commit is documented and repeatable.
- [ ] Local full-DataHub instructions remain functional even if the hosted mode uses a constrained fallback.

## Required automated checks

```bash
<platform-specific build/deploy commands verified during milestone>
<public smoke-test command>
make check
pytest -m acceptance -k deployed
git diff --check
```

Replace angle-bracket placeholders only when the actual environment has established the command. Never report an unexecuted placeholder as passing.

## Manual test for the operator

1. Open the URL in an incognito browser on another network and run the scenario.
2. Let the service sleep/restart where applicable and measure cold-start behavior.
3. Disconnect one dependency and confirm the UI reports its real mode/status rather than silently switching.

## Explicit non-goals

- Do not move to production enterprise infrastructure.
- Do not fake a live DataHub connection or commit deployment secrets.

## Handoff requirements

Update `tasks/PROJECT_STATE.md`, `tasks/CURRENT_TASK.md`, and `tasks/DECISION_LOG.md`. Return the format in `tasks/HANDOFF_TEMPLATE.md`, including exact commands, results, limitations, and the proposed commit message.
