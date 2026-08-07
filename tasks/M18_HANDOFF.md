# Milestone handoff

## Summary

- Milestone: M18 — README, examples, video, and Devpost submission package
- Status: submitted — public Devpost participation and VPS judge path active; external acceptance pending
- Recommended operator decision: participating; record unfamiliar-reviewer and second-network checks before the deadline
- Proposed commit message: `docs: record VPS judge deployment`

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
  visible release `c5817af`. That URL later became authentication-gated and is retained only as
  historical deployment evidence.
- Deployed exact frozen source `c5817af` at `https://rcr-ia.eu/schemabridge/` behind the existing
  Nginx TLS host. The container is non-root, root-filesystem read-only, resource-bounded,
  restartable, and loopback-only; no database, DataHub, LLM, or administrator credential was added.
- Passed public health/deployment smoke and the complete anonymous browser north-star journey on
  the VPS with exact `2/1/1`, all three rejection records, and no browser console error.
- Produced and visually reviewed the final public silent, English-captioned H.264 1920×1080/30 fps
  video at exactly 2:55. SHA-256 is
  `a7ac6119d93ee5001b40bdc63811eb562db513b5571d31f10ca528ca17739d3b`.
- Published that video at `https://youtu.be/R8PPBJ5ot84`. YouTube reports processing complete and
  the copyright check complete with no issue; an unauthenticated watch request and oEmbed lookup
  return the expected title. Monetization and paid promotion remain untouched.
- Submitted Devpost entry `1109948` with category, Spain residence, submission-period attestation,
  repository/demo/examples, DataHub Core/MCP selections, Feedback Prize opt-in, and four factual
  product-feedback answers. The owner authorized final terms and Submit at action time. Devpost
  confirmed the public entry at `https://devpost.com/software/schemabridge`; anonymous HTTP returns
  200 and the expected title. No payment, monetization, paid promotion, or purchase was performed.
- Saved the VPS URL in both Devpost Project details and Additional info. The submission remains
  `Submitted`, `5/5 steps done`, and `Project submitted!`; the public **Try it out** link shows the
  replacement URL.
- Updated GitHub repository description and topics. Public repo and detected Apache-2.0 license
  remain verified.

## Files changed

- `README.md`: public judge/video links plus frozen source/tag and release evidence.
- `examples/final/`: release-ready generated evaluation summary and manifest.
- `docs/screenshots/m18/*.png`, `docs/screenshots/m18/README.md`: final release UI evidence and inventory.
- `docs/14_DEPLOYMENT.md`, `docs/17_JUDGE_OPERATIONS.md`: exact VPS topology, smoke, operations,
  troubleshooting, and isolated rollback.
- `docs/15_SUBMISSION_CHECKLIST.md`: exact source/tag/image/package/video/submission evidence and remaining external checks.
- `docs/18_DEVPOST_SUBMISSION.md`: final submitted fields, feedback summary, and VPS public URL.
- `docs/18_VIDEO_PRODUCTION.md`: final public export checksum, URL, YouTube checks, and privacy review.
- `plans/M18_SUBMISSION_PACKAGE.md`: current acceptance status and public evidence.
- `tasks/CURRENT_TASK.md`, `tasks/PROJECT_STATE.md`, `tasks/DECISION_LOG.md`: D149–D151 and current M18 state.
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
| Devpost Additional info and final submission | pass | Owner-confirmed eligibility, Feedback Prize answers, legal acceptance, and Submit; public URL returns HTTP 200 |
| exact `c5817af` VPS Docker build and constrained run | pass | Image `sha256:fbd970…0558b`; non-root, healthy, read-only, loopback-only |
| Nginx configuration test/reload and public root/health checks | pass | `/schemabridge/` and health return 200; existing `rcr-ia.eu/` remains 200 |
| public VPS deployment smoke | pass | Five-attempt smoke succeeds against `https://rcr-ia.eu/schemabridge/` |
| anonymous VPS browser north-star | pass | Release `c5817af`; exact `2/1/1`; all three rejections; no console errors |
| Devpost Project details/Additional info URL replacement | pass | Both saved; public **Try it out** exposes VPS URL; submission remains active |
| `.venv/bin/python scripts/release_audit.py --check-external --check-history` | pass | 1,081 files, 23 licenses, 44 external links, 47 history revisions; expected dirty-tree warning before commit |
| `make check` | pass | Supply chain, release scan, formatting, Ruff, Mypy, performance, and 4,094 unit tests; 250 deselected; 910.74 s |
| post-submit Streamlit anonymous smoke | fail | `/` and `/_stcore/health` return `303` to Streamlit authentication; no payment fallback authorized |
| pre-submit `.venv/bin/python scripts/release_audit.py --check-external --check-history` | pass | Historical evidence-only tree; architecture, secret, license, internal/external link, and history audit |
| annotated tag creation/push | pass | `devpost-m18-c5817af` resolves to exact source commit |
| public tag clone, build, and documented smoke | pass after docs correction | Clean detached source; initial `make judge-smoke` exposed absent `.venv`; stdlib `python3 scripts/smoke_deployment.py` passes and is now the judge instruction |
| post-submit anonymous Devpost `curl -L` | pass | HTTP 200; final URL and title `SchemaBridge | Devpost` verified |
| post-submit anonymous Streamlit root/health `curl` | fail | HTTP 303 to Streamlit authentication; visibility regression recorded |
| post-submit release audit with external/history checks | fail as expected | 45 links checked; current Streamlit URL is the sole broken external link |
| `.venv/bin/python scripts/release_audit.py --check-history` | pass with dirty-tree warning | 1,081 files, 23 licenses, 46 history revisions; documentation not yet committed |
| `git diff --check` | pass | Final submission/visibility evidence diff has no whitespace errors |

## Automated test results

- Focused tests: all release-clean prerequisite/focal suites pass.
- `make check`: pass again on the documentation-only VPS evidence tree; format, Ruff, strict Mypy,
  supply-chain/release policy, performance node, 4,094 passed and 250 deselected in 910.74 seconds.
- Integration tests: 184 passed, 3 explicit skips in 202.66 seconds.
- Acceptance tests: 66 passed, 1 explicit skip in 50.80 seconds.
- Coverage: 4,340 passed, 3 skipped, 1 deselected; 81.32% against an 80% threshold.
- Post-restart: four live integration tests pass; DataHub catalog/MCP/registry checks pass.
- Hosted CI: run `31003886659` completed successfully in all three jobs.

## Operator manual test

1. Ask one unfamiliar person the five questions in `docs/15_SUBMISSION_CHECKLIST.md` using only the
   public README/video; record their verbatim answers.
2. Test repo, demo, video, examples, and Devpost links from a second device/network; record the
   result and correct any mismatch before the deadline.

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
- Logged in: D141 and D145–D151 in `tasks/DECISION_LOG.md`.

## Known limitations or unverified items

- The public Devpost entry is active at `https://devpost.com/software/schemabridge`; anonymous HTTP
  returned 200 with title `SchemaBridge | Devpost` on 2026-08-07.
- The old Streamlit URL remains authentication-gated and is superseded by the verified VPS path;
  judges are not instructed to use it.
- Second-network/device and unfamiliar-reviewer checks remain unperformed.
- The video is an edited progression of genuine captured release/DataHub states with burned captions,
  not a narrated continuous cursor recording; it truthfully labels local live versus recorded modes.
- This is a hackathon MVP with PostgreSQL-only bounded execution and small tuned synthetic evaluation.
- M18 does not satisfy M30: all 24 commercial/production external controls remain NO-GO.

## Remaining post-submit work

- An unfamiliar human and a second network/device are required for the checklist's external acceptance.

## Next milestone readiness

- Dependencies satisfied: exact source/tag, public VPS demo/video, local image, strict gates, generated
  artifacts, screenshots, and active Devpost participation are complete.
- Recommended next prompt: perform the unfamiliar-reviewer and second-network checks without
  changing the frozen source.
- Required operator prerequisites: unfamiliar reviewer and second device/network.
