# Final submission checklist

Current sign-off: **SUBMITTED — actively participating; public VPS judge path passes and only the
unfamiliar-reviewer/second-network evidence remains**.
The frozen source, strict clean-room gate, hosted CI, annotated tag, final artifact package,
release screenshots, anonymous VPS demo journey, public video, eligibility fields, legal
acceptance, and Devpost submission are complete. Devpost's public and judge-only demo fields now
point to the VPS replacement. The unfamiliar-reviewer and second-network evidence remain and do not
undo the active hackathon entry.

## Release identity

- Release commit: `c5817af6d01b8a98cd7f1950d57e1be667614696`
- Release tag: `devpost-m18-c5817af` (annotated; resolves to the release commit)
- `examples/final/manifest.json` package SHA-256:
  `5b4a2fa1a8a01920ac54ba0f5efc2155236d268f32d8cc19d4a0ff7c2bbcc0e9`
- Manifest file SHA-256: `c25baeef12c372b69fae0a114be94d3bc86a4049e4d4ea373d643475d14dae2a`
- Video SHA-256 and duration:
  `a7ac6119d93ee5001b40bdc63811eb562db513b5571d31f10ca528ca17739d3b`,
  `175.000000` seconds (2:55)
- Configured demo URL: `https://rcr-ia.eu/schemabridge/`
- Public video URL: `https://youtu.be/R8PPBJ5ot84`
- Public Devpost project URL: `https://devpost.com/software/schemabridge`
- Anonymous verification: `HTTP 200`, title `SchemaBridge | Devpost`, 2026-08-07

## Repository

- [x] Public repository verified signed out on 2026-07-22.
- [x] GitHub detects Apache-2.0 and `LICENSE` contains the full text.
- [x] AI assistance and third-party dependencies are disclosed.
- [x] Strict release scan proves no secrets or employer data in the release tree and Git history.
- [x] Setup, run, reset, modes, limitations, and troubleshooting are documented.
- [x] `examples/final/` is regenerated from the clean release commit with `release_ready: true`.
- [x] Release tag and commit hash are recorded above and in the Devpost copy.

## Functionality

- [x] Strict clean-room run proves the complete north-star flow from the release commit.
- [x] Live local DataHub read, approval-gated write, read-back, restart, and reuse are recorded.
- [x] Logical concepts, mapping risks, join cardinality, and fanout mitigation are inspectable.
- [x] Guided and deterministic natural-language paths have equivalent typed north-star plans.
- [x] Unsafe identifiers and SQL-policy rejections are visible in generated examples.
- [x] Hosted recorded/fake and local live modes are unambiguously labeled.
- [x] Public VPS path requires no login/payment, exposes the frozen `c5817af` release, passes health
  and page smoke, and completes the exact anonymous north-star journey.

## Evaluation and artifacts

- [x] Synthetic ground truth and difficult cases are checked in.
- [x] `make evaluate` and both submission-package commands are documented.
- [x] Metrics and checksums are regenerated from the clean release commit.
- [x] Raw counts, retained failures, small-fixture limitation, and unrun live LLM are disclosed.
- [x] Generated examples include mapping, model, join, request, plan, SQL, validation, rejection,
  write-back contract, evaluation summary, and manifest.
- [x] Final screenshots are captured from the release build and privacy/rights reviewed.

## Devpost and video

- [x] Project name is SchemaBridge; category is Agents That Do Real Work.
- [x] English pitch, description, built-with tags, testing instructions, and disclosure are drafted.
- [x] Copy differentiates the semantic-governance layer from DataHub Analytics Agent without
  disparaging or misrepresenting it.
- [x] Devpost draft has the final elevator pitch, project story, public tags, and repository URL.
- [x] Devpost Project details and Additional info save the public VPS URL; Project details also
  retains the public YouTube URL.
- [x] Additional info records Spain, new project during the submission period, DataHub Core/MCP,
  the Feedback Prize opt-in, and all four specific product-feedback answers.
- [x] The owner authorized final legal acceptance and submission at action time; Devpost displayed
  `Project submitted!` and published the project URL.
- [x] YouTube reports the video public and its copyright check completed with no issues; an
  unauthenticated watch request and oEmbed lookup both resolve the expected title.
- [x] Every `PENDING` field in `docs/18_DEVPOST_SUBMISSION.md` is replaced with a tested value.
- [x] Repository, demo, video, example, and Devpost links work signed out on the operator network.
- [ ] Repeat the final link and north-star check from a second network/device.
- [x] Final local video export is below 3:00 and visibly shows the project functioning.
- [x] Captions/on-screen text make the complete story readable with sound muted.
- [x] Video has no unlicensed music, third-party footage, exposed credentials, proprietary data, or
  unapproved marks/assets.

## Operator acceptance

1. Ask a person unfamiliar with the project to use only the final README and video, then record
   concise answers:

   - What problem does SchemaBridge solve? `PENDING REVIEWER ANSWER`
   - Why is DataHub essential? `PENDING REVIEWER ANSWER`
   - How is it different from existing text-to-SQL? `PENDING REVIEWER ANSWER`
   - What prevents unsafe or wrong SQL? `PENDING REVIEWER ANSWER`
   - What concrete result did the demo produce? `PENDING REVIEWER ANSWER`

2. Clone the public release tag into a new directory and run the README judge instructions
   verbatim. Record command output and platform: `PASS 2026-08-05 — macOS Apple silicon host,
   Docker Desktop, clean public tag clone, linux/amd64 build, healthy non-root container, stdlib
   deployment smoke passed. The first rehearsal exposed and corrected the undocumented .venv
   assumption in the smoke command.`
3. Restart the VPS container during a maintenance window, record cold-start duration, then run
   public smoke and the complete north-star path again: `PENDING`.
4. Signed-out independent-browser result on the operator network: `PASS 2026-08-07 — HTTPS 200,
   health ok, release c5817af, exact 2/1/1, and all three rejections`. Repeat from another
   network/device and record date/device/result: `PENDING`.
5. Compare README, Devpost, video, examples, release manifest, and actual UI. Any mismatch requires
   a correction before the deadline.
