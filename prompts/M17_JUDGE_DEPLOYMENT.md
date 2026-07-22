# Codex milestone M17: Stable judge deployment and operations

Use the `$schemabridge-milestone` skill. Work only on **M17**. Recommended setting: **GPT-5.6 Sol — Extra High (xhigh)**.

## Read first

1. `AGENTS.md` and all nested `AGENTS.md` files governing files you may edit.
2. `docs/00_PRODUCT_VISION.md`
3. `docs/01_SCOPE.md`
4. `docs/02_ARCHITECTURE.md`
5. `docs/06_SECURITY.md`
6. `plans/M17_JUDGE_DEPLOYMENT.md`
7. `tasks/PROJECT_STATE.md`
8. `tasks/DECISION_LOG.md`

## Objective

Deploy a free, stable, honest judge-accessible application and document the full local DataHub path and any hosted fallback mode.

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

- Research and select the current hosting topology based on real resource limits, sleep behavior, secrets, public access, and DataHub feasibility.
- Package the release with deterministic demo reset and health/status indicators.
- Deploy the UI and required services or a clearly labeled recorded-context fallback while preserving the full local integration path.
- Configure scoped secrets/service accounts and verify no admin credentials are exposed.
- Add uptime/cold-start instructions, troubleshooting, and a rollback/redeploy path.
- Test the public URL from incognito and a separate network/device.

## Acceptance criteria

- [ ] The public URL works without payment or hidden operator steps for judges.
- [ ] Live versus recorded/fake DataHub and LLM modes are unambiguously labeled.
- [ ] The public path produces the expected north-star result and safety/rejection evidence.
- [ ] Secrets are stored outside Git and scoped to demo assets.
- [ ] A clean deploy from the release commit is documented and repeatable.
- [ ] Local full-DataHub instructions remain functional even if the hosted mode uses a constrained fallback.

## Expected checks

```bash
<platform-specific build/deploy commands verified during milestone>
<public smoke-test command>
make check
pytest -m acceptance -k deployed
git diff --check
```

If a listed command contains a placeholder, determine and document the verified command rather than pretending the placeholder ran.

## Do not do

- Do not move to production enterprise infrastructure.
- Do not fake a live DataHub connection or commit deployment secrets.

## Operator test to prepare

1. Open the URL in an incognito browser on another network and run the scenario.
2. Let the service sleep/restart where applicable and measure cold-start behavior.
3. Disconnect one dependency and confirm the UI reports its real mode/status rather than silently switching.

Do not commit automatically unless the operator explicitly asks. End using `tasks/HANDOFF_TEMPLATE.md` and include a single proposed commit message.
