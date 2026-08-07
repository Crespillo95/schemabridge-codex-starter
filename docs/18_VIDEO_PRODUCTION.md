# M18 video production package

Status: **final export verified and public on YouTube**.

Final local export:

- Source release: `c5817af6d01b8a98cd7f1950d57e1be667614696`
- Duration: `175.000000` seconds (2:55)
- Video: H.264, 1920 × 1080, 30 fps
- SHA-256: `9fa7b47a35169144917911a7a8390d3114d61e0015a12d1ec6b80d8aeacdb1cb`
- Audio: AI-generated English narration using OpenAI text-to-speech voice `cedar`; no music
- Captions: English, burned into the video from `docs/18_CAPTIONS.srt`
- Mode labels: `LIVE LOCAL DATAHUB | SYNTHETIC` and
  `RECORDED HOSTED FALLBACK | RELEASE c5817af`

Frames at 0:05, 1:20, 2:05, 2:25, and 2:53 were visually reviewed. The export contains genuine
captured release UI and local DataHub states, no changed functional values, and no credential,
personal data, local path, notification, proprietary data, music, or third-party footage.

## Public upload metadata

- Public URL: https://youtu.be/6Bw7yGhl24o

- Title: `SchemaBridge — Narrated Governed Semantic Query Demo | DataHub Hackathon`
- Description discloses the synthetic-data boundary, public demo/source/Devpost links, governed
  DataHub/SQL behavior, and that the narration uses OpenAI text-to-speech voice `cedar`.
- Audience: not made for children; this is a technical software demonstration.
- Visibility: public.
- Music/audio: AI-generated English narration; no music; English captions remain burned in.
- Upload state: published on 2026-08-07. YouTube reports the SD/HD processing complete and the
  copyright check complete with no issues. An unauthenticated HTTP request and YouTube oEmbed
  lookup both resolve the expected public title. The prior silent upload at
  `https://youtu.be/R8PPBJ5ot84` remains historical evidence and is not the Devpost video.

The final video must show the release functioning, remain below 3:00, be understandable with audio
off, and use no music. Record from the clean release commit after the public deployment is stable.
Use only synthetic SchemaBridge screens and output. Crop unrelated browser chrome, accounts,
notifications, local paths, tokens, and third-party marks not necessary to demonstrate the
integration.

## Recording setup

- Canvas: 1920 × 1080, 30 fps; browser content at a readable zoom.
- Target duration: 2:55; hard reject any export at or above 3:00.
- Audio: optional original narration only; no music or sound library asset.
- Captions: burn in or upload `docs/18_CAPTIONS.srt`; keep equivalent on-screen headings.
- Modes: say **live local DataHub** or **recorded hosted fallback** exactly when each appears.
- Evidence: use the release UI and DataHub state; do not substitute mockups or manually edited rows.

## Timed shot list and narration

| Time | Picture | Exact narration/on-screen point |
|---|---|---|
| 0:00–0:16 | Three physical customer-key representations | “A customer key is a padded string here, an integer there, and an unsafe float elsewhere. Valid SQL can still join the wrong meaning or overcount.” |
| 0:16–0:34 | Local DataHub physical assets and explicit missing-evidence state | “SchemaBridge reads bounded schemas and governance context from DataHub. Missing lineage or query history stays missing—it is never invented.” |
| 0:34–0:56 | Customer logical model and mapping evidence | “It proposes one Customer key with evidence, confidence, normalization, and risk. Confidence prioritizes review; a human still approves the mapping.” |
| 0:56–1:12 | Customer → AccountHolder contract | “The approved join is one-to-many. Customer 123 has two holder links, so entity counts require the contract’s exact distinct-key mitigation.” |
| 1:12–1:30 | Query Studio request and explicit interpretation | “The request counts customers by registration date when the holder role is secondary. SchemaBridge distinguishes customers from holder relationships before planning.” |
| 1:30–1:55 | Resolved plan, SQL, policy checks | “A typed plan selects approved versions. A deterministic compiler produces parameterized PostgreSQL, and an independent AST guard validates the final statement.” |
| 1:55–2:15 | Result and rejection view | “The read-only preview returns two, one, and one. Fractional, non-finite, and null-valued identifiers are rejected visibly instead of truncated or hidden.” |
| 2:15–2:39 | DataHub after state and approval audit | “With explicit approval, SchemaBridge writes the model, join contract, decision context, and SQL-free recipe back to DataHub, with a target-level audit record.” |
| 2:39–2:51 | New workflow reusing context | “A later workflow retrieves that context but still replans, recompiles, validates, and asks for execution approval. Saved SQL is never executed.” |
| 2:51–2:55 | Project name and verified links | “SchemaBridge: govern meaning before SQL.” |

## Capture order

1. From the release commit, start the full local stack and verify DataHub plus PostgreSQL health.
2. Capture DataHub **before** approved context, then run the exact approval-gated write path.
3. Capture the uninterrupted Streamlit scenario, result, and rejections.
4. Capture DataHub **after** and a fresh workflow's recipe reuse state.
5. If hosted footage is included, keep its recorded/fake labels on screen and never imply it is the
   live local DataHub segment.
6. Export once, add captions/on-screen headings, and make only timing/privacy edits—never edit
   values or status labels.

## Verification commands

Replace `submission-video.mp4` only with the local final export:

```bash
ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 submission-video.mp4
ffprobe -v error -show_entries stream=codec_type,codec_name,width,height -of json submission-video.mp4
shasum -a 256 submission-video.mp4
```

Record the duration and checksum in `docs/15_SUBMISSION_CHECKLIST.md`. Upload to YouTube or Vimeo
with public visibility, enable captions, then open the link signed out on a second network. Watch
from 0:00 to the end with sound muted and confirm all text, values, modes, and safety evidence are
readable. Verify no copyrighted music, third-party footage, personal data, credentials, or
unapproved marks appear.

## Stop conditions

Do not publish or link the video if the release ref differs, it reaches 3:00, any mode label is
missing, DataHub before/after is not genuine, captions disagree with the UI, or a credential or
private notification is visible. Re-record rather than concealing a functional discrepancy in the
edit.
