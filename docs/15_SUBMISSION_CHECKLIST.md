# Final submission checklist

Current sign-off: **NOT READY**. Repository visibility/license are verified and the development
artifact package exists. Release commit/tag, public demo, final video, clean-checkout proof, and
external reviewer evidence remain pending. Do not submit until every final checkbox is complete.

## Release identity

- Release commit: `PENDING`
- Release tag: `PENDING`
- `examples/final/manifest.json` package SHA-256:
  `41e2681d536edc20a8af78b5b27d272927c59eec6212f7a427525b395717328e` (development only;
  regenerate after commit)
- Video SHA-256 and duration: `PENDING`
- Public demo URL: `PENDING`
- Public video URL: `PENDING`
- Devpost project URL: `PENDING`

## Repository

- [x] Public repository verified signed out on 2026-07-22.
- [x] GitHub detects Apache-2.0 and `LICENSE` contains the full text.
- [x] AI assistance and third-party dependencies are disclosed.
- [ ] Strict release scan proves no secrets or employer data in the release tree and Git history.
- [x] Setup, run, reset, modes, limitations, and troubleshooting are documented.
- [ ] `examples/final/` is regenerated from the clean release commit with `release_ready: true`.
- [ ] Release tag and commit hash are recorded above and in the Devpost copy.

## Functionality

- [ ] Strict clean-room run proves the complete north-star flow from the release commit.
- [ ] Live local DataHub read, approval-gated write, read-back, restart, and reuse are recorded.
- [x] Logical concepts, mapping risks, join cardinality, and fanout mitigation are inspectable.
- [x] Guided and deterministic natural-language paths have equivalent typed north-star plans.
- [x] Unsafe identifiers and SQL-policy rejections are visible in generated examples.
- [x] Hosted recorded/fake and local live modes are unambiguously labeled.
- [ ] Free public deployment is reachable without login, payment, or operator intervention.

## Evaluation and artifacts

- [x] Synthetic ground truth and difficult cases are checked in.
- [x] `make evaluate` and both submission-package commands are documented.
- [ ] Metrics and checksums are regenerated from the clean release commit.
- [x] Raw counts, retained failures, small-fixture limitation, and unrun live LLM are disclosed.
- [x] Generated examples include mapping, model, join, request, plan, SQL, validation, rejection,
  write-back contract, evaluation summary, and manifest.
- [ ] Final screenshots are captured from the release build and privacy/rights reviewed.

## Devpost and video

- [x] Project name is SchemaBridge; category is Agents That Do Real Work.
- [x] English pitch, description, built-with tags, testing instructions, and disclosure are drafted.
- [x] Copy differentiates the semantic-governance layer from DataHub Analytics Agent without
  disparaging or misrepresenting it.
- [ ] Every `PENDING` field in `docs/18_DEVPOST_SUBMISSION.md` is replaced with a tested value.
- [ ] Repository, demo, video, and example links work signed out and from a second network/device.
- [ ] Public video is below 3:00 and visibly shows the project functioning.
- [ ] Captions/on-screen text make the complete story readable with sound muted.
- [ ] Video has no unlicensed music, third-party footage, exposed credentials, proprietary data, or
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
   verbatim. Record command output and platform: `PENDING`.
3. Let the hosted service sleep/restart if applicable, record cold-start duration, then disconnect
   one dependency and confirm the real status appears without silent fallback: `PENDING`.
4. Open every final link in an incognito/signed-out browser, repeat the north-star path on another
   network/device, and record date/device/result: `PENDING`.
5. Compare README, Devpost, video, examples, release manifest, and actual UI. Any mismatch blocks
   submission.
