# SchemaBridge

**SchemaBridge is a governed semantic query agent that reconciles inconsistent physical schemas, proposes reusable logical models and join contracts, and turns business requests into explainable, validated SQL using DataHub context.**

> Status: M16 release-candidate audit. The governed local workflow is implemented and tested, but
> this copy is not releasable until the operator creates the reviewed initial commit and reruns the
> clean-room release gate from a clean `HEAD`.

## North-star scenario

A user asks:

> Count customers by registration date when they are a secondary holder of an account.

The relevant data is split across heterogeneous tables. Customer identifiers use values such as `"00000000123"`, `123`, and `123.0`; role values use `SECONDARY`, `2`, and `CO_HOLDER`. SchemaBridge should:

1. read schemas, definitions, available lineage/query evidence, and governance context from DataHub;
2. resolve physical fields to approved logical concepts;
3. resolve an approved `Customer → AccountHolder` join contract;
4. detect one-to-many fanout and enforce the contract-approved `COUNT DISTINCT` mitigation;
5. compile a typed query plan into deterministic PostgreSQL;
6. validate and run a bounded read-only preview;
7. write approved mappings, decisions, and reusable query context back to DataHub.

## Why this is not another text-to-SQL chatbot

DataHub already has text-to-SQL capabilities. SchemaBridge focuses on the missing prerequisite: **creating and governing the semantic context required for trustworthy SQL when schemas are inconsistent**.

```text
Messy physical schemas
        ↓
Canonical concepts and normalization policies
        ↓
Approved logical models and join contracts
        ↓
Typed analytical request and query plan
        ↓
Deterministic SQL compilation and validation
        ↓
Bounded preview and context write-back
```

## Architecture

The project follows a ports-and-adapters architecture:

- `domain`: pure, typed business rules and invariants;
- `application`: use cases and protocol ports;
- `adapters`: DataHub, PostgreSQL, LLM, SQL parser/compiler, persistence;
- `entrypoints`: CLI and Streamlit;
- `bootstrap.py`: dependency composition only.

See `docs/02_ARCHITECTURE.md` and `docs/05_QUERY_PIPELINE.md`.

## Judge quick start

Prerequisites: Docker Desktop/Compose v2, GNU Make, and Python 3.11–3.13. The default UI path uses
an explicitly recorded synthetic catalog, deterministic fake intent parsing, live read-only
synthetic PostgreSQL, and fake local publication. It does not masquerade as a live DataHub flow.

```bash
bash scripts/bootstrap.sh
make demo-reset
make check
make ui
```

Open `http://localhost:8501`, select **Load demo scenario**, confirm the typed interpretation, and
approve the bounded preview. The expected result is `2`, `1`, and `1` for 2026-01-01 through
2026-01-03. The view also exposes the selected assets, one-to-many fanout mitigation, SQL-policy
status, and the three rejected keys (`127.5`, `NaN`, and `NULL`).

Integration modes are always visible and never fall back silently:

| Boundary | Default judge path | Optional live path |
|---|---|---|
| Catalog | recorded synthetic fixture | local DataHub MCP reads |
| Intent | deterministic typed fake | configured structured-output model |
| Source | live read-only demo PostgreSQL | same bounded reader |
| Publication | fake local adapter | approval-gated DataHub writer |

For the complete live DataHub setup, destructive reset scope, and verification commands, use the
[local runbook](docs/12_RUNBOOK.md). The browser checklist and screenshots are in
[browser acceptance](docs/14_BROWSER_ACCEPTANCE.md); M16 findings and limitations are in the
[release audit](reports/release-audit.md).

## Development workflow

The implementation history is preserved as milestone plans and prompts. Operator commands and
current limitations live in `docs/12_RUNBOOK.md`. Each milestone has:

- an implementation plan in `plans/`;
- a paste-ready Codex prompt in `prompts/`;
- acceptance criteria and a manual test;
- a required handoff update in `tasks/`.

## Repository map

```text
src/schemabridge/        Application code
src/.../domain/          Pure business model
src/.../application/     Use cases and ports
src/.../adapters/        External-system integrations
src/.../entrypoints/     CLI and Streamlit UI
demo/                    Synthetic PostgreSQL and ground truth
infra/datahub/            Local DataHub ingestion/configuration
docs/                    Product, architecture, security, testing, demo
plans/                   Milestone specifications
prompts/                 Paste-ready Codex tasks
tasks/                   Durable project state and handoffs
examples/                Judge-readable generated artifacts
```

## Security baseline

SchemaBridge never writes to source databases. Query execution uses a dedicated read-only PostgreSQL role, accepts only a single `SELECT`/`WITH ... SELECT`, validates an AST, applies allowlists, rejects Cartesian joins, limits the number of tables and rows, and enforces a statement timeout. DataHub mutations remain human-approved.

## License and disclosure

Apache-2.0. See `LICENSE` and `HACKATHON_DISCLOSURE.md`.
