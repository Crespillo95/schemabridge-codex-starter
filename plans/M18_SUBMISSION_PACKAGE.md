# M18: README, examples, video, and Devpost submission package

- Status: submitted and publicly participating; public VPS judge path passes, external reviewer/network evidence pending
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

- [x] Every current non-public claim in README/Devpost/video corresponds to implemented, tested release behavior.
- [x] The repository is public, Apache-2.0, complete, and free of secrets/proprietary data.
- [x] The live URL and all links work without an authenticated application session on the operator
  network; a second-network repetition remains an operator evidence task.
- [x] The video is public, under three minutes, readable, and shows the project functioning.
- [x] `examples/` contains final generated artifacts that a judge can inspect without running code.
- [x] Materials are in English or include complete English translation.
- [x] The submission clearly proves DataHub read, act/write, and reuse plus the semantic-layer differentiation.

## Current candidate evidence

- The recorded Docker image completed the north-star workflow through publication in a human
  browser audit with no developer-popup leak and with exact English request/result labels.
- Pinned local DataHub ingested 11 synthetic datasets; its read-only MCP identity passed search and
  schema reads with mutation tools absent.
- The immutable `synthetic_enterprise` v1 registry was published under one exact approval, read
  back as 7 models/31 mappings/5 joins/37 decisions, replayed as `already_current`, survived a full
  DataHub restart, and executed the two live ground-truth requests without recorded fallback.
- Full acceptance passes 66 with one superseded historical skip. Full integration passes 184 with
  three explicit skips: one superseded v1 fixture, one separately provisioned M34 publisher IAM
  path, and one retained historical browser corpus.
- Frozen source `c5817af` passes the complete strict clean-room gate: 4,094 unit tests, 184
  integration tests, 66 acceptance tests, 4,340 coverage tests at 81.32%, fresh PostgreSQL/DataHub,
  registry publication/read-back/restart, deterministic evaluation, Streamlit smoke, and final
  release audit. Hosted PR CI run `31003886659` also passes all three jobs.
- Annotated tag `devpost-m18-c5817af`, generated release-ready artifacts, ten final Streamlit
  captures, and the verified 2:55 captioned local video export are complete.
- The VPS replacement at `https://rcr-ia.eu/schemabridge/` serves the exact frozen image, passes
  public health/smoke and the full anonymous browser journey, and is saved in both Devpost demo
  fields. YouTube video `R8PPBJ5ot84` is public at 2:55 with a clear copyright check, and Devpost
  submission `1109948` is publicly participating at `https://devpost.com/software/schemabridge`.
  Unfamiliar-reviewer and second-network acceptance remain post-submit work.

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
3. Open every Devpost, repository, demo, video, and example link from a second network/device and
   record the result before the deadline.

## Explicit non-goals

- Do not add features after release freeze except true submission blockers.
- Do not overstate scalability, production readiness, or unsupported dialects.

## Handoff requirements

Update `tasks/PROJECT_STATE.md`, `tasks/CURRENT_TASK.md`, and `tasks/DECISION_LOG.md`. Return the format in `tasks/HANDOFF_TEMPLATE.md`, including exact commands, results, limitations, and the proposed commit message.
