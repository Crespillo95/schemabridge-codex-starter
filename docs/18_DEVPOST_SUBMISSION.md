# M18 Devpost submission copy

> **SUBMITTED 2026-08-07.** Devpost confirmed `Project submitted!`; the public entry is active at
> https://devpost.com/software/schemabridge and remains editable until the hackathon deadline.

## Submission fields

- Project name: **SchemaBridge**
- Challenge category: **Agents That Do Real Work**
- Elevator pitch: **SchemaBridge turns inconsistent schemas into approved semantic context,
  compiles business questions into safe SQL, and writes reusable decisions back to DataHub.**
- Repository: https://github.com/Crespillo95/schemabridge-codex-starter
- Configured live project URL: https://rcr-ia.eu/schemabridge/
- Public narrated video URL: https://youtu.be/6Bw7yGhl24o
- Public Devpost project URL: https://devpost.com/software/schemabridge
- Release commit: `c5817af6d01b8a98cd7f1950d57e1be667614696`
- Release tag: `devpost-m18-c5817af`

Devpost submission `1109948` is submitted and public. The elevator pitch, project story,
repository URL, public demo/video, and public tags `postgresql`, `python`, `datahub`, `streamlit`,
and `sqlglot` are saved. Additional info records category **Agents That Do Real Work**, Spain,
newly created during the submission period, the repository/demo/examples URLs, and DataHub Core
plus MCP. The owner opted into the optional Feedback Prize and authorized legal acceptance and
Submit at action time. That opt-in is prize eligibility only; no purchase, payment, monetization,
or promotion was performed.

The four submitted feedback answers describe: the reproducible Core 1.6.0/MCP read-only and
approval-gated write/restart path; the initially missing semantic registry after physical ingest
and the explicit prepare/publish/read-back fix; a requested native versioned semantic-contract
model; and the observed `REGISTRY_NOT_FOUND` boundary after reset and physical ingest.

## About the project

### Inspiration

A customer key can be `"00000000123"` in one system, `123` in another, and `123.0` in a third.
Similar names can still mean different things, while a valid one-to-many join can silently
overcount a metric. Catalog discovery helps an agent find tables, but it does not by itself prove
semantic equivalence, approved normalization, or safe fanout handling.

SchemaBridge was built to govern that missing semantic layer before an agent generates SQL.

### What it does

SchemaBridge is a DataHub-native governed semantic query agent. It reads bounded catalog and
governance context through DataHub MCP, then proposes logical fields and join contracts with
evidence, confidence, missing evidence, risks, and closed normalization steps. A steward explicitly
approves every semantic decision and every DataHub mutation.

An analyst can then submit a guided or natural-language request. When an LLM is enabled, it may
return only a validated typed intent—never executable SQL, physical assets, tools, or approvals.
SchemaBridge resolves current approved mappings and join versions, detects fanout, compiles a
restricted query plan into deterministic parameterized PostgreSQL, independently reparses the
final SQL AST, and runs only a bounded preview as a dedicated read-only database user.

For the request “Count customers by registration date when they are a secondary holder of an
account,” the synthetic demo returns `2`, `1`, and `1` for January 1–3, 2026. It visibly rejects
`127.5`, `NaN`, and `NULL` join keys rather than truncating, repairing, or hiding them.

With explicit approval, mappings, joins, decision context, and a SQL-free query recipe can be
written back to DataHub and retrieved by a later workflow. Reuse never executes saved SQL: the
current request is planned, compiled, guarded, and approved again.

### How we built it

SchemaBridge uses a ports-and-adapters architecture:

- Pure Pydantic domain models define mappings, approvals, transformations, joins, requests, query
  plans, and validation.
- Application use cases depend only on typed ports.
- Adapters integrate DataHub Core/MCP/SDK, PostgreSQL, SQLGlot, optional OpenAI structured outputs,
  SQLite, and deterministic recordings/fakes.
- Streamlit and Typer are thin entrypoints; `bootstrap.py` is the only composition root.
- A deterministic compiler produces SQL from a typed plan, and an independent AST policy allows
  exactly one read-only `SELECT` or `WITH … SELECT` over approved assets.

The public Docker fallback is secret-free and clearly labels recorded synthetic catalog/source
observations, deterministic typed intent, and fake local publication. The complete DataHub Core
v1.6.0 plus PostgreSQL integration remains reproducible locally; there is no silent live-to-fake
fallback.

### DataHub integration

DataHub is essential in three places:

1. **Read:** MCP catalog discovery and bounded schema/governance retrieval. Missing lineage or
   query history remains explicit missing evidence.
2. **Act and write:** approval-gated logical-model context, structured properties, decision
   documents, join contracts, and query recipes with target-level audit results.
3. **Reuse:** a fresh workflow retrieves governed context from DataHub, then replans and revalidates
   instead of trusting saved SQL.

DataHub's Analytics Agent already provides catalog-grounded plain-English analytics. SchemaBridge's
contribution is the governed semantic reconciliation layer that creates and versions the
equivalences, normalization policies, and fanout-safe joins that a downstream analytics agent needs
when physical schemas disagree.

### Challenges

The hardest parts were keeping semantic confidence separate from approval, preventing the LLM from
producing executable SQL, making fanout mitigation exact rather than heuristic, preserving unsafe
identifiers as visible rejections, and proving a real approval-gated DataHub write/read-back/restart
path without putting credentials in the public demo.

A strict clean-room run also found two valuable release issues: the semantic registry had to be
published explicitly after a fresh DataHub reset, and a fixed-minute rate-limit integration test
had to accept the two legitimate outcomes when CI crossed a minute boundary.

### Accomplishments

Frozen source `c5817af` passes:

- 4,094 unit tests;
- 184 integration tests and 66 acceptance tests;
- 4,340 coverage tests at 81.32%;
- fresh PostgreSQL 16.13 and DataHub Core v1.6.0 bootstrap;
- 11 synthetic datasets and 121 metadata events;
- read-only MCP checks with mutation tools absent;
- approval-gated registry publication/read-back as 7 models, 31 mappings, 5 joins, and 37 decisions;
- DataHub restart persistence and context reuse;
- hosted supply-chain, quality, integration, acceptance, and coverage CI; and
- a human Safari journey through interpretation, planning, SQL safety, exact results, rejections,
  publication, decisions, and relationships.

The release tree/history scan covers 1,071 files, 23 direct dependency licenses, 41 external links,
and 43 Git revisions.

### What we learned

Governed agentic analytics needs more than text-to-SQL quality. It needs explicit semantic
authority, deterministic compilation, independent enforcement at the final SQL boundary, auditable
human decisions, and a reusable context store. Missing evidence is useful information and should
never be silently invented.

### What's next

This hackathon MVP is PostgreSQL-only, limited to three tables/two joins and a restricted expression
set, and evaluated on a small tuned synthetic fixture. Production still requires protected release
controls, an independent blind evaluation and security review, operated infrastructure/IAM/
observability evidence, and a pilot. Those boundaries are documented rather than presented as
completed work.

## Built with

`DataHub Core`, `DataHub MCP Server`, `Python`, `Pydantic`, `Streamlit`, `PostgreSQL`, `SQLGlot`,
`Typer`, `SQLite`, `Docker`, `pytest`, `OpenAI API (optional typed-intent adapter; no live evaluation)`

## Testing instructions for judges

### Public path

Open https://rcr-ia.eu/schemabridge/ and:

1. Open the URL without login or payment.
2. Confirm the status panel says recorded catalog, deterministic typed fake, recorded source, and
   fake local publication.
3. Load the scenario, confirm **distinct customers**, and approve the bounded preview.
4. Verify rows `2026-01-01 → 2`, `2026-01-02 → 1`, `2026-01-03 → 1` and all three rejections.

### Docker path

```bash
git clone --branch devpost-m18-c5817af \
  https://github.com/Crespillo95/schemabridge-codex-starter.git
cd schemabridge-codex-starter
make judge-build
docker run -d --name schemabridge-judge \
  -p 127.0.0.1:7860:7860 schemabridge-judge:local
python3 scripts/smoke_deployment.py --url http://127.0.0.1:7860
```

### Full local DataHub path

Use the README's **Full local DataHub path** and `docs/12_RUNBOOK.md`. It ingests only synthetic
assets, keeps tokens in ignored mode-0600 files, and runs the live DataHub/PostgreSQL integration
and acceptance suites. No admin credential is used by the application runtime.

## Disclosure

SchemaBridge was created during the submission period. OpenAI ChatGPT and Codex assisted with
planning, implementation, tests, documentation, and review; human decisions and automated gates
validated the output. No model output is executed directly as SQL or treated as approval. The
repository contains only synthetic data and screenshots, no employer/proprietary material, and no
third-party stock image, font, audio, or video asset. Source is Apache-2.0; direct dependencies and
their licenses are inventoried by the release audit. Full details are in
`HACKATHON_DISCLOSURE.md`.

## Post-submit verification

The entry is active. On 2026-08-07 Devpost saved the replacement VPS URL in both public Project
details and judge-only Additional info, retained `Submitted` and `5/5 steps done`, and displayed
`Project submitted!`. The public project page exposes the new URL under **Try it out**. Record the
remaining unfamiliar-reviewer and second-network/device evidence in
`docs/15_SUBMISSION_CHECKLIST.md`; correct any mismatch before the deadline without changing the
frozen executable release.
