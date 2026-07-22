# Hackathon disclosure

SchemaBridge is a new project created during the Build with DataHub submission period. All material
repository work recorded here occurred on 2026-07-21 and 2026-07-22.

## AI-assisted development

- OpenAI ChatGPT assisted with the initial repository scaffold, planning documents, synthetic
  dataset, and starter code. The originating ChatGPT model identifier was not retained, so no more
  specific model claim is made.
- OpenAI Codex desktop was used for M00–M16 implementation, tests, documentation, and review. The
  repository configuration selects `gpt-5.6-sol`; milestone prompts requested High or Extra High
  reasoning. The five independent M16 audit subagents ran as `gpt-5.6-sol` with Ultra reasoning.
  The desktop application did not expose a shell-verifiable build/version or per-turn model audit,
  and this limitation is recorded rather than guessed.
- AI output was reviewed through typed contracts, tests, static checks, live local integrations,
  and the operator handoff. No model output is executed directly as SQL or treated as approval.

## Source and asset provenance

- No proprietary employer source code, SQL, screenshots, credentials, table names, customer data,
  or production samples are included. All demo values and screenshots come from the synthetic
  SchemaBridge scenario.
- No external starter template or pre-existing application snippet was incorporated beyond the
  AI-assisted scaffold described above. The official DataHub quickstart Compose file is downloaded
  at runtime from its pinned URL and verified checksum; it is not vendored into the repository.
- The repository contains no third-party stock image, font, audio, or video asset. UI screenshots
  were captured from this implementation.

## Software licenses

SchemaBridge source is licensed under Apache-2.0 in `LICENSE`. Direct Python dependencies are used
unmodified under their published licenses. The M16 release scanner inventories the installed
versions and metadata; the direct set is predominantly MIT or Apache-2.0, with psycopg under
LGPL-3.0-only and NumPy under BSD-compatible terms plus its documented bundled-runtime notices.
Dependencies are installed from package indexes and are not copied into this source tree.
