# M18 Devpost submission copy

> **DRAFT — DO NOT SUBMIT YET.** Replace the three pending link fields only after the same clean
> release commit is tagged, deployed, pushed, and tested. Do not remove this warning while the
> M18 manifest says `release_ready: false`.

## Submission fields

- Project name: **SchemaBridge**
- Challenge category: **Agents That Do Real Work**
- Elevator pitch: **SchemaBridge turns inconsistent physical schemas into human-approved semantic
  context, then compiles business questions into explainable, independently validated read-only SQL
  and writes reusable decisions back to DataHub.**
- Repository: https://github.com/Crespillo95/schemabridge-codex-starter
- Live project URL: **PENDING — public M17 deployment not created**
- Public video URL: **PENDING — final recording/upload not performed**
- Release tag: **PENDING — do not tag an uncommitted tree**

## About the project

### The problem

The same customer key may appear as `"00000000123"`, `123`, or `123.0`; the same-looking field name
may mean something else; and a valid one-to-many join can silently duplicate a metric. Catalog
metadata helps an agent find tables, but it does not automatically establish that two fields are
semantically equivalent, which normalization is approved, or how fanout must be handled.

### What SchemaBridge does

SchemaBridge reads bounded metadata and governance context from DataHub through its MCP Server. It
proposes logical fields and join contracts with evidence, confidence, missing evidence, risks, and
closed normalization steps. A steward must explicitly approve semantic decisions and every DataHub
mutation.

An analyst then submits a guided or natural-language request. The language model, when enabled, can
return only a validated typed intent—never executable SQL, physical assets, tools, or approvals.
SchemaBridge resolves current approved mappings and join versions, detects fanout, compiles a
restricted query plan into deterministic PostgreSQL, independently reparses the final SQL AST, and
runs a bounded preview as a dedicated read-only database user.

For the request “Count customers by registration date when they are a secondary holder of an
account,” the synthetic demo returns `2`, `1`, and `1` for January 1–3, 2026. It visibly rejects
`127.5`, `NaN`, and `NULL` join keys. Approved mappings, join documents, workflow context, and a
SQL-free query recipe can be written to DataHub and retrieved by a later workflow. Reuse never
executes saved SQL; the current request is planned, compiled, guarded, and approved again.

### Why DataHub matters

DataHub is both the governed read context and the approved knowledge store. SchemaBridge uses MCP
for catalog discovery and schema inspection, and bounded approval-gated DataHub adapters for logical
model relationships, glossary/structured context, decision documents, join contracts, and reusable
query recipes. Missing lineage or query history is reported as missing evidence instead of being
fabricated.

DataHub's Analytics Agent already provides catalog-grounded plain-English analytics. SchemaBridge's
original contribution is the governed semantic reconciliation layer that creates and versions the
equivalences, normalization policies, and fanout-safe joins that a downstream analytics agent needs
when physical schemas disagree.

### How it was built

SchemaBridge uses a ports-and-adapters architecture. Pure Pydantic domain models define mappings,
approvals, transformations, joins, requests, query plans, and validation. Application use cases
depend on typed ports. Adapters integrate DataHub Core/MCP/SDK, PostgreSQL, SQLGlot, optional OpenAI
structured outputs, SQLite, and deterministic recordings/fakes. Streamlit and Typer are thin
entrypoints; `bootstrap.py` is the only composition root.

The public fallback is a secret-free Docker image with clearly labeled recorded synthetic catalog
and source observations. The complete DataHub + PostgreSQL integration remains reproducible
locally. There is no silent live-to-fake fallback.

### Verified results

The small synthetic evaluation reports raw counts only: mapping precision/recall/F1 are `3/4 =
0.750`, with one retained false positive and one retained false negative; join path is `5/5`,
cardinality and recipe reuse are `2/2`, typed intent is `1/1` with four intentional M27 skips, query
result and source rejection are `5/5`, and the independent SQL guard rejects `38/38` malicious
cases. The optional live LLM evaluation was not run and is not mixed with deterministic evidence.

### Limitations

This is a hackathon MVP, not production-ready enterprise infrastructure. PostgreSQL is the only
executable dialect; queries use at most three tables/two joins and a restricted expression set; semantic and
catalog writes require a human; the fixture is small and tuned; full planning mappings are a
clearly labeled synthetic registry; and DataHub plus the local audit ledger do not offer one
distributed transaction. Field definitions are governed and visible, but registry-wide matching
from a brief description remains M27. The hosted fallback does not claim live DataHub, PostgreSQL,
or LLM connectivity.

## Built with

`DataHub Core`, `DataHub MCP Server`, `Python`, `Pydantic`, `Streamlit`, `PostgreSQL`, `SQLGlot`,
`Typer`, `SQLite`, `Docker`, `pytest`, `OpenAI API (optional typed-intent adapter; no live evaluation)`

## Testing instructions for judges

### Public path

**PENDING.** After deployment, paste the verified public URL here. The final instruction must be:

1. Open the URL without login or payment.
2. Confirm the status panel says recorded catalog, deterministic typed fake, recorded source, and
   fake local publication.
3. Load the scenario, confirm **distinct customers**, and approve the bounded preview.
4. Verify rows `2026-01-01 → 2`, `2026-01-02 → 1`, `2026-01-03 → 1` and all three rejections.

### Docker path

```bash
git clone https://github.com/Crespillo95/schemabridge-codex-starter.git
cd schemabridge-codex-starter
make judge-build
docker run -d --name schemabridge-judge \
  -p 127.0.0.1:7860:7860 schemabridge-judge:local
make judge-smoke
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

## Final paste gate

Before copying this page into Devpost, complete every box in `docs/15_SUBMISSION_CHECKLIST.md`,
replace all `PENDING` values, regenerate `examples/final/manifest.json` from the clean release
commit, and verify the repository, demo, video, and example URLs in a signed-out browser.
