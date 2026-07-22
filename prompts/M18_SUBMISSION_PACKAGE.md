# Codex milestone M18: README, examples, video, and Devpost submission package

Use the `$schemabridge-milestone` skill. Work only on **M18**. Recommended setting: **GPT-5.6 Sol — Extra High (xhigh)**.

## Read first

1. `AGENTS.md` and all nested `AGENTS.md` files governing files you may edit.
2. `docs/00_PRODUCT_VISION.md`
3. `docs/01_SCOPE.md`
4. `docs/02_ARCHITECTURE.md`
5. `docs/06_SECURITY.md`
6. `plans/M18_SUBMISSION_PACKAGE.md`
7. `tasks/PROJECT_STATE.md`
8. `tasks/DECISION_LOG.md`

## Objective

Create a judge-first English submission that accurately communicates the problem, meaningful DataHub use, original contribution, verified results, safety, limitations, and exact testing path.

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

- Rewrite the README around the final verified behavior, quick test, architecture, DataHub before/after, evaluation results, limitations, and links.
- Generate final examples from the release build: mapping, logical model, join contract, analytical request, query plan, SQL, validation report, rejection report, DataHub write-back, and evaluation summary.
- Write final Devpost project description, elevator pitch, built-with tags, testing instructions, and disclosure in English.
- Record/edit a public video under three minutes following `docs/09_DEMO_STORY.md`; add captions or on-screen text and no unlicensed music.
- Capture clean screenshots and test every public link in incognito.
- Create a release tag/checksum and final submission checklist sign-off.

## Acceptance criteria

- [ ] Every claim in README/Devpost/video corresponds to implemented, tested release behavior.
- [ ] The repository is public, Apache-2.0, complete, and free of secrets/proprietary data.
- [ ] The live URL and all links work in incognito.
- [ ] The video is public, under three minutes, readable, and shows the project functioning.
- [ ] `examples/` contains final generated artifacts that a judge can inspect without running code.
- [ ] Materials are in English or include complete English translation.
- [ ] The submission clearly proves DataHub read, act/write, and reuse plus the semantic-layer differentiation.

## Expected checks

```bash
<clean-checkout judge test commands>
<artifact generation command>
<link/license/secret scan commands>
make check
pytest -m acceptance
git diff --check
```

If a listed command contains a placeholder, determine and document the verified command rather than pretending the placeholder ran.

## Do not do

- Do not add features after release freeze except true submission blockers.
- Do not overstate scalability, production readiness, or unsupported dialects.

## Operator test to prepare

1. Ask an unfamiliar reviewer to answer the five questions in `docs/15_SUBMISSION_CHECKLIST.md` using only README/video.
2. Run the judge testing instructions verbatim from a clean checkout.
3. Open every Devpost, repository, demo, video, and example link in incognito before submitting.

Do not commit automatically unless the operator explicitly asks. End using `tasks/HANDOFF_TEMPLATE.md` and include a single proposed commit message.
