# Current task

- Current milestone: M18 — README, examples, video, and Devpost submission package
- Status: local release candidate audited; clean freeze and account-owned publication pending
- Plan: `plans/M18_SUBMISSION_PACKAGE.md`
- Handoff: `tasks/M18_HANDOFF.md`
- Hackathon submission GO: **NO — public URL, video, release/tag, and final human checks pending**
- Commercial/production GO: **NO — M30 remains 0/24 external controls and is not changed by M18**

## Objective

Freeze a truthful judge-first release, publish the secret-free recorded Docker demo, produce a
public sub-three-minute video, and complete the DataHub Devpost submission without converting local
or synthetic evidence into a production claim.

## Completed on the current candidate

- Corrected public Streamlit privacy/UX configuration: minimal toolbar, no developer error details
  or links, no usage telemetry, English north-star text, and one active-workflow focus.
- Made the Docker build reproducible from the hashed runtime/build locks, copied required migrations
  and Streamlit configuration, and verified the pinned Python 3.13.13 / Streamlit 1.60.0 /
  SQLGlot 29.0.1 image as `linux/amd64`.
- Hardened the release link audit to scan human-facing Markdown, ignore fenced/inline code and
  reserved placeholders, perform redirect-following GET checks, and report source lines.
- Updated disclosure, official OpenAI references, video-host requirements, Hugging Face packaging,
  M22 actor guidance, and the exact M32 Spanish example placeholder.
- Human-tested the recorded Docker UI through interpretation, one-to-many fanout mitigation,
  independent SQL safety checks, exact `2/1/1` result, `127.5`/`NaN`/`NULL` rejection, and fake
  approval-gated publication. No Streamlit developer popup was visible.
- Captured candidate Streamlit and live DataHub screenshots from synthetic state only; privacy and
  rights review is recorded in `docs/screenshots/m18/README.md`.
- Started pinned DataHub Core v1.6.0, ingested/profiled 11 synthetic datasets through 121 events,
  provisioned separate mode-0600 reader/writer identities, and verified MCP search/schema reads
  with mutation tools absent.
- Prepared and published immutable registry `synthetic_enterprise` v1 only after the exact
  fingerprint confirmation. Audit
  `registry-publication-v1-48a537c2722a9258064c4033c9cd50ca2215bac84d2f2b5b00c474b45a0bdb04`
  records the pseudonymous local actor, 37 decisions, and successful read-back. Exact replay is
  `already_current`.
- Restarted the complete DataHub stack and reverified health, 11 datasets, registry shape
  7/31/5/37, MCP read-only behavior, two live registry ground-truth requests, and the approval-gated
  workflow publication/reuse path.
- Corrected one live integration test that attempted to reissue the stable existing actor's
  approval ID from a new empty ledger. The test now uses a distinct stable authenticated local
  operator and preserves all fail-closed production semantics and idempotence assertions.

## Current automated evidence

- Focused release/UI/web tests: 34 passed before the live-service cut.
- DataHub-specific integration: 9 passed, 1 explicit M34 publisher-IAM skip.
- Full acceptance: 66 passed, 1 superseded historical skip.
- Full integration: 184 passed, 3 explicit skips.
- Live post-restart acceptance: 3 passed.
- Docker build/smoke/deployed acceptance and manual browser north-star: passed.
- Final current-byte `make check` passes supply-chain/release policy, formatting, Ruff, strict Mypy,
  isolated performance, and 4,080 functional tests with 250 deselected in 898.48 seconds.
- Clean release package, clean-checkout clone, and public URL/video checks remain pending until the
  source release identity is frozen.

## Remaining release sequence

1. Commit and push the fully gated candidate, wait for hosted CI, then regenerate `examples/final/` from the
   clean source commit with `release_ready: true`.
2. Authenticate the installed `hf` CLI using the owner's scoped Hugging Face credential, create the
   public Docker Space, upload the exact commit package, and test it signed out.
3. Record/edit the release UI plus real local DataHub evidence into a silent/captioned video below
   three minutes; upload publicly to YouTube or Vimeo and verify duration/checksum/link.
4. Obtain an unfamiliar human reviewer and second-network/device checks, then merge/tag/release,
   update every pending URL/hash, and submit through the owner's Devpost session.

## External/operator prerequisites

- Hugging Face account authentication for one public Docker Space.
- YouTube or Vimeo account authentication for the public video.
- Devpost account authentication and the final legal/eligibility attestations.
- A person unfamiliar with the project plus a second network/device for the required manual review.

## Preserved production boundary

M18 is a hackathon release milestone, not M30 production acceptance. M30's protected branch/tag,
independent evaluator, blind 1,000-case campaign, external IAM/operations/security evidence,
assessor decisions, and M31 pilot remain open exactly as recorded in `tasks/M30_HANDOFF.md` and
`plans/M30_PRODUCTION_EVALUATION_SECURITY.md`.
