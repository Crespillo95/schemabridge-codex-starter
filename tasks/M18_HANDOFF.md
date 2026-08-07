# Milestone handoff

## Summary

- Milestone: M18 — README, examples, video, and Devpost submission package
- Status: partial — source, public demo, and public video complete; owner/legal and external acceptance pending
- Recommended operator decision: needs owner attestations and manual external tests before Devpost submission
- Proposed commit message: `docs: record public M18 demo and video`

## Implemented

- Froze and pushed source `c5817af6d01b8a98cd7f1950d57e1be667614696`; published annotated
  tag `devpost-m18-c5817af` pointing exactly to that source.
- Completed the strict clean-room from empty PostgreSQL/DataHub volumes, including approval-gated
  registry publication, integration, acceptance, coverage, evaluation, restart persistence,
  Streamlit health, and release/history/link/license scans.
- Built and smoke-tested the final `linux/amd64` secret-free recorded judge image as non-root user;
  image digest is `sha256:b8ee73024ed6a33e32033bde69974dcb1c969e2a54875753dbb0a6cbd08b65f7`.
- Completed the exact north-star journey in Safari WebDriver, captured ten release Streamlit views,
  and retained six genuine pinned local DataHub views. Privacy and rights review passes.
- Regenerated the complete final example package with `release_ready: true` and source revision
  `c5817af`; package SHA-256 is
  `5b4a2fa1a8a01920ac54ba0f5efc2155236d268f32d8cc19d4a0ff7c2bbcc0e9`.
- Published the secret-free recorded judge path at
  `https://schemabridge-governed-agent.streamlit.app/`; an anonymous human browser completed the
  ambiguity, governed-plan, SQL-safety, `2/1/1`, rejection, and fake-publication journey against
  visible release `c5817af`.
- Produced and visually reviewed the final public silent, English-captioned H.264 1920×1080/30 fps
  video at exactly 2:55. SHA-256 is
  `a7ac6119d93ee5001b40bdc63811eb562db513b5571d31f10ca528ca17739d3b`.
- Published that video at `https://youtu.be/R8PPBJ5ot84`. YouTube reports processing complete and
  the copyright check complete with no issue; an unauthenticated watch request and oEmbed lookup
  return the expected title. Monetization and paid promotion remain untouched.
- Updated existing Devpost draft `1109948`: elevator pitch, 5,788-character project story, tags,
  repository, public demo, and video are saved; category/repo/demo/examples/DataHub Core+MCP are
  prefilled but deliberately unsaved beside owner residence and creation-period attestations. The
  optional payment-related Feedback Prize remains untouched.
- Updated GitHub repository description and topics. Public repo and detected Apache-2.0 license
  remain verified.

## Files changed

- `README.md`: public judge/video links plus frozen source/tag and release evidence.
- `examples/final/`: release-ready generated evaluation summary and manifest.
- `docs/screenshots/m18/*.png`, `docs/screenshots/m18/README.md`: final release UI evidence and inventory.
- `docs/15_SUBMISSION_CHECKLIST.md`: exact source/tag/image/package/video evidence and remaining owner checks.
- `docs/18_DEVPOST_SUBMISSION.md`: saved public demo/video fields and remaining owner-only gate.
- `docs/18_VIDEO_PRODUCTION.md`: final public export checksum, URL, YouTube checks, and privacy review.
- `plans/M18_SUBMISSION_PACKAGE.md`: current acceptance status and public evidence.
- `tasks/CURRENT_TASK.md`, `tasks/PROJECT_STATE.md`, `tasks/DECISION_LOG.md`: D149 and current M18 state.
- `tasks/M18_HANDOFF.md`: this handoff.

## Commands executed

| Command | Result | Notes |
|---|---|---|
| `SCHEMABRIDGE_RELEASE_DATAHUB_CONFIRMATION=publish-approved-registry-version make release-clean` | pass | Exact source `c5817af`; complete fresh-service strict gate |
| `.venv/bin/python scripts/release_audit.py --require-release --check-external --check-history` | pass | 1,071 files, 23 licenses, 41 links, 43 revisions |
| `gh run view 31003886659 ...` | pass | Hosted supply-chain, quality, PostgreSQL/DataHub integration/acceptance/coverage all green; head `c5817af` |
| `docker build --platform linux/amd64 --build-arg SCHEMABRIDGE_RELEASE_REF=c5817af ...` | pass | Digest `sha256:b8ee73…b65f7`, release ref exact |
| final container health and deployment smoke | pass | Non-root `user`; healthy; root page returned successfully |
| Safari WebDriver north-star journey | pass | Interpretation, plan, SQL guard, `2/1/1`, three rejections, publication, decisions, relationships, reset |
| `make submission-package` | pass | 11 outputs; release-ready manifest bound to `c5817af` |
| `scripts/package_huggingface_space.sh c5817af...` | pass | Secret-free Docker Space package; exact `RELEASE_COMMIT` |
| `ffprobe ...` and `shasum -a 256 ...mp4` | pass | 175 s; H.264 1920×1080 30 fps; checksum recorded above |
| anonymous Streamlit browser north-star | pass | `2/1/1`, three rejections, governed SQL, fake publication, release `c5817af` |
| YouTube publish and public HTTP/oEmbed lookup | pass | `R8PPBJ5ot84`; copyright clear; expected title returned without authentication |
| Devpost Project details save | pass | Repository, demo, video, story, and tags saved; advanced owner fields not submitted |
| `.venv/bin/python scripts/release_audit.py --check-external --check-history` | pass | Evidence-only tree; architecture, secret, license, internal/external link, and history audit |
| annotated tag creation/push | pass | `devpost-m18-c5817af` resolves to exact source commit |
| public tag clone, build, and documented smoke | pass after docs correction | Clean detached source; initial `make judge-smoke` exposed absent `.venv`; stdlib `python3 scripts/smoke_deployment.py` passes and is now the judge instruction |
| `git diff --check` | pass | Public URL/evidence documentation diff has no whitespace errors |

## Automated test results

- Focused tests: all release-clean prerequisite/focal suites pass.
- `make check`: pass inside strict clean-room; format, Ruff, strict Mypy, supply-chain/release policy,
  performance node, 4,094 passed and 250 deselected in 942.70 seconds.
- Integration tests: 184 passed, 3 explicit skips in 202.66 seconds.
- Acceptance tests: 66 passed, 1 explicit skip in 50.80 seconds.
- Coverage: 4,340 passed, 3 skipped, 1 deselected; 81.32% against an 80% threshold.
- Post-restart: four live integration tests pass; DataHub catalog/MCP/registry checks pass.
- Hosted CI: run `31003886659` completed successfully in all three jobs.

## Operator manual test

1. Ask one unfamiliar person the five questions in `docs/15_SUBMISSION_CHECKLIST.md` using only the
   public README/video; record their verbatim answers.
2. Test repo, demo, video, examples, and Devpost links from a second device/network; complete the Devpost
   eligibility/legal attestations and submit.

Expected result:

```text
The public demo requires no login/payment, displays recorded/fake modes, returns 2/1/1, exposes
127.5/NaN/NULL rejections, and matches source c5817af. The reviewer identifies semantic ambiguity,
DataHub-governed mappings/joins, deterministic compilation plus independent AST validation, and the
concrete result. Every public link works signed out.
```

## Architecture and security review

- Dependency direction: unchanged; evidence-only changes do not alter the ports-and-adapters boundaries.
- Source database writes: none; the public image contains no source or DataHub writer credential.
- SQL/LLM validation: typed intent only, deterministic compiler, independent SQL AST guard,
  allowlists/limits/timeout, and explicit fanout mitigation remain enforced.
- DataHub mutation approval: real local writer path required exact confirmation/approval/audit and
  read-back; public fallback is clearly labeled fake local publication.
- Secrets/proprietary data: synthetic data only; strict tree/history scan and media review pass.
- Fanout/semantic risks: approved one-to-many contract requires exact `COUNT DISTINCT`; name
  similarity never grants equivalence and ambiguity still requires a human.

## Decisions made

- Decision: keep the public hackathon topology secret-free/recorded while retaining separate genuine
  local DataHub read/write/restart/reuse evidence.
- Decision: use non-production annotated tag `devpost-m18-c5817af`, not M30's stable SemVer channel.
- Decision: accept both real fixed-minute-window outcomes in the CI test without changing runtime policy.
- Reason: the public service must be stable and credential-free; release identity must not imply M30
  production acceptance; a time-boundary test must assert semantics rather than scheduler timing.
- Logged in: D141 and D145–D149 in `tasks/DECISION_LOG.md`.

## Known limitations or unverified items

- The final public Devpost URL is not yet available.
- The Devpost slug `https://devpost.com/software/schemabridge` is reserved but redirects signed-out
  visitors to login while the submission remains a draft.
- Second-network/device and unfamiliar-reviewer checks remain unperformed.
- The video is an edited progression of genuine captured release/DataHub states with burned captions,
  not a narrated continuous cursor recording; it truthfully labels local live versus recorded modes.
- This is a hackathon MVP with PostgreSQL-only bounded execution and small tuned synthetic evaluation.
- M18 does not satisfy M30: all 24 commercial/production external controls remain NO-GO.

## Blockers

- Devpost residence, creation-period eligibility, final terms acceptance, and Submit require exact
  owner facts/confirmation and cannot be fabricated.
- An unfamiliar human and a second network/device are required for the checklist's external acceptance.

## Next milestone readiness

- Dependencies satisfied: exact source/tag, public demo/video, local image, strict gates, generated
  artifacts, and screenshots are complete.
- Recommended next prompt: confirm the two Devpost eligibility facts, perform external acceptance,
  accept the final terms at action time, and submit without changing source.
- Required operator prerequisites: Devpost owner attestations, unfamiliar reviewer, and second
  device/network.
