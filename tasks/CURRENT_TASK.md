# Current task

- Current milestone: M18 — README, examples, video, and Devpost submission package
- Status: source release audited/tagged; account-owned publication and external acceptance pending
- Plan: `plans/M18_SUBMISSION_PACKAGE.md`
- Handoff: `tasks/M18_HANDOFF.md`
- Hackathon submission GO: **NO — public demo/video URLs, external checks, and owner attestations pending**
- Commercial/production GO: **NO — M30 remains 0/24 external controls and is not changed by M18**

## Objective

Freeze a truthful judge-first release, publish the secret-free recorded Docker demo, produce a
public sub-three-minute video, and complete the DataHub Devpost submission without converting local
or synthetic evidence into a production claim.

## Completed on the frozen source release

- Froze source commit `c5817af6d01b8a98cd7f1950d57e1be667614696`, pushed it, passed hosted
  PR CI run `31003886659`, and published annotated tag `devpost-m18-c5817af` pointing exactly to it.
- Ran the full strict clean-room from empty service volumes with explicit synthetic registry
  publication confirmation. Unit, integration, acceptance, coverage, evaluation, Streamlit smoke,
  restart persistence, starter integrity, and strict release scan all pass.

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
- Captured final Streamlit and live DataHub screenshots from synthetic state only; privacy and
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
- Regenerated `examples/final/` with `release_ready: true` and source revision `c5817af`; package
  SHA-256 is `5b4a2fa1a8a01920ac54ba0f5efc2155236d268f32d8cc19d4a0ff7c2bbcc0e9`.
- Human-tested the exact final image in Safari through reset, interpretation, governed plan, SQL
  safety, execution, rejection evidence, publication, decisions, relationships, and fresh reset.
- Exported and visually reviewed a silent English-captioned 1920×1080 H.264 video at exactly 2:55;
  SHA-256 is `754b4f36e588da941a225ed35c313d4dd0656b443d740186e01a05a4d88bfdaa`.
- Updated the existing Devpost draft `1109948`: final elevator pitch, 5,788-character verified
  project story, public tags, and repository URL are saved. Category, repository, examples, and
  DataHub Core/MCP are prefilled in Additional info but remain unsaved beside owner-only eligibility
  fields. Reserved public slug is `https://devpost.com/software/schemabridge`.
- Verified Chrome has authenticated YouTube Studio and Devpost sessions. Video upload is prepared
  but file selection remains blocked until the user grants the ChatGPT Chrome extension access to
  file URLs. Hugging Face is not authenticated in Safari, in-app browser, or Chrome.

## Final automated evidence

- Focused release/UI/web tests: 34 passed before the live-service cut.
- DataHub-specific integration: 9 passed, 1 explicit M34 publisher-IAM skip.
- Full acceptance: 66 passed, 1 superseded historical skip.
- Full integration: 184 passed, 3 explicit skips.
- Live post-restart acceptance: 3 passed.
- Docker build/smoke/deployed acceptance and manual browser north-star: passed.
- Strict `make release-clean` on `c5817af` passes: 4,094 unit tests, 184 integration tests with 3
  explicit skips, 66 acceptance tests with 1 explicit skip, and 4,340 coverage tests with 3 skips
  and 1 deselection at 81.32%. Fresh PostgreSQL 16.13, DataHub Core v1.6.0 ingest/reader/writer,
  registry 7/31/5/37, evaluation, restart/reuse, Streamlit smoke, and final scan all pass.
- Independent strict audit passes 1,071 files, 23 direct dependency licenses, 41 external links,
  and 43 Git-history revisions.
- Hosted run `31003886659` passes `supply-chain`, `quality`, and `postgres-integration`, including
  complete coverage and evaluation on the PR merge ref associated with head `c5817af`.
- Final `linux/amd64` image digest is
  `sha256:b8ee73024ed6a33e32033bde69974dcb1c969e2a54875753dbb0a6cbd08b65f7`;
  it ran as non-root user `user`, reached healthy status, and passed the deployment smoke.

## Remaining account/external sequence

1. Authenticate the installed `hf` CLI using the owner's account, create the
   public Docker Space, upload the exact commit package, and test it signed out.
2. Upload the verified local video to public YouTube or Vimeo and test the public link signed out.
3. Obtain an unfamiliar human reviewer and second-network/device checks, update every pending URL,
   merge the evidence-only commit, and submit through the owner's Devpost session.

## External/operator prerequisites

- Hugging Face account authentication for one public Docker Space.
- YouTube Studio is authenticated; public upload still needs Chrome extension file-URL access.
- Devpost is authenticated; final legal/eligibility attestations remain owner-only.
- Chrome extension file-URL access for the already authenticated YouTube upload.
- A person unfamiliar with the project plus a second network/device for the required manual review.

## Preserved production boundary

M18 is a hackathon release milestone, not M30 production acceptance. M30's protected branch/tag,
independent evaluator, blind 1,000-case campaign, external IAM/operations/security evidence,
assessor decisions, and M31 pilot remain open exactly as recorded in `tasks/M30_HANDOFF.md` and
`plans/M30_PRODUCTION_EVALUATION_SECURITY.md`.
