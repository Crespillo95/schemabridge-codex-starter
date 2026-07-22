# M18: README, examples, video, and Devpost submission package

- Status: planned
- Timebox: 5 hours
- Recommended Codex: GPT-5.6 Sol — Extra High (xhigh)
- Dependencies: M15–M17

## Objective

Create a judge-first English submission that accurately communicates the problem, meaningful DataHub use, original contribution, verified results, safety, limitations, and exact testing path.

## Why this milestone exists now

Judges may decide from the description, images, README, and sub-three-minute video without installing the project.

## Deliverables

- Rewrite the README around the final verified behavior, quick test, architecture, DataHub before/after, evaluation results, limitations, and links.
- Generate final examples from the release build: mapping, logical model, join contract, analytical request, query plan, SQL, validation report, rejection report, DataHub write-back, and evaluation summary.
- Write final Devpost project description, elevator pitch, built-with tags, testing instructions, and disclosure in English.
- Record/edit a public video under three minutes following `docs/09_DEMO_STORY.md`; add captions or on-screen text and no unlicensed music.
- Capture clean screenshots and test every public link in incognito.
- Create a release tag/checksum and final submission checklist sign-off.

## Implementation sequence

1. Freeze the release commit and regenerate all evidence artifacts from it.
2. Draft Devpost copy that differentiates SchemaBridge from DataHub Analytics Agent without disparaging existing features.
3. Run a judge comprehension review using someone unfamiliar with the project or an independent subagent.
4. Rehearse the video to fit the time limit, then record a clean uninterrupted flow plus concise edits.
5. Validate repository/license/disclosure/links/test instructions and English language requirements.
6. Complete the final checklist and retain a local copy of submitted text and URLs.

## Acceptance criteria

- [ ] Every claim in README/Devpost/video corresponds to implemented, tested release behavior.
- [ ] The repository is public, Apache-2.0, complete, and free of secrets/proprietary data.
- [ ] The live URL and all links work in incognito.
- [ ] The video is public, under three minutes, readable, and shows the project functioning.
- [ ] `examples/` contains final generated artifacts that a judge can inspect without running code.
- [ ] Materials are in English or include complete English translation.
- [ ] The submission clearly proves DataHub read, act/write, and reuse plus the semantic-layer differentiation.

## Required automated checks

```bash
<clean-checkout judge test commands>
<artifact generation command>
<link/license/secret scan commands>
make check
pytest -m acceptance
git diff --check
```

Replace angle-bracket placeholders only when the actual environment has established the command. Never report an unexecuted placeholder as passing.

## Manual test for the operator

1. Ask an unfamiliar reviewer to answer the five questions in `docs/15_SUBMISSION_CHECKLIST.md` using only README/video.
2. Run the judge testing instructions verbatim from a clean checkout.
3. Open every Devpost, repository, demo, video, and example link in incognito before submitting.

## Explicit non-goals

- Do not add features after release freeze except true submission blockers.
- Do not overstate scalability, production readiness, or unsupported dialects.

## Handoff requirements

Update `tasks/PROJECT_STATE.md`, `tasks/CURRENT_TASK.md`, and `tasks/DECISION_LOG.md`. Return the format in `tasks/HANDOFF_TEMPLATE.md`, including exact commands, results, limitations, and the proposed commit message.
