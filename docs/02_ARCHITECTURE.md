# Architecture

## Architectural style

SchemaBridge uses ports and adapters with a functional core and imperative shell.

```text
┌──────────────────────────────────────────────────────────────────┐
│ Entrypoints                                                      │
│ CLI · Streamlit                                                  │
└─────────────────────────────┬────────────────────────────────────┘
                              │ typed commands / view models
┌─────────────────────────────▼────────────────────────────────────┐
│ Application                                                      │
│ use cases · ports · orchestration · approval workflow            │
└─────────────────────────────┬────────────────────────────────────┘
                              │ domain values and policies
┌─────────────────────────────▼────────────────────────────────────┐
│ Domain                                                           │
│ concepts · mappings · transformations · joins · requests · plans │
└──────────────────────────────────────────────────────────────────┘
               ▲                    ▲                    ▲
               │ ports              │ ports              │ ports
┌──────────────┴───────┐  ┌─────────┴──────────┐  ┌─────┴─────────┐
│ DataHub adapters     │  │ PostgreSQL / SQL   │  │ LLM / storage │
│ MCP · SDK · fakes    │  │ reader · compiler  │  │ typed parser  │
└──────────────────────┘  └────────────────────┘  └───────────────┘
```

## Dependency rule

All source dependencies point inward.

```text
entrypoints → application → domain
adapters ────────────────→ application ports / domain values
bootstrap → all concrete components
```

The domain never imports an adapter. The application never constructs a concrete adapter. The UI never contains business rules.

## Modules

### Domain

Implemented focused submodules include:

- `fields.py`: physical field identity and profile signals.
- `concepts.py`: logical models and canonical fields.
- `transformations.py`: closed transformation algebra.
- `mappings.py`: candidate and approved column mappings.
- `joins.py`: join candidates, contracts, cardinality, fanout policy.
- `requests.py`: typed analytical request.
- `plans.py`: resolved semantic query plan.
- `validation.py`: validation findings and severity.
- `decisions.py`: approval and version records.

### Application

Implemented use cases include:

- scan catalog;
- generate semantic candidates;
- review and publish a canonical model;
- discover and approve join contracts;
- resolve a guided request;
- parse a natural-language request;
- plan, compile, validate, and preview a query;
- publish and retrieve reusable context;
- evaluate against ground truth.

Ports isolate:

- catalog reads;
- catalog writes;
- sample profiling;
- SQL execution;
- SQL parsing/guarding;
- natural-language parsing;
- draft/decision storage;
- clock and identifiers where needed.

### Adapters

- DataHub MCP adapter for discovery, lineage, query context, and documents.
- DataHub SDK/OpenAPI adapter for logical-model operations where MCP lacks a required primitive.
- PostgreSQL sample/preview adapter.
- SQLGlot compiler/guard adapter.
- OpenAI structured-output language adapter plus deterministic fake.
- SQLite/local YAML draft store plus DataHub as the approved source of truth.

### Entrypoints

- Typer CLI for diagnostics, evaluation, and scripted demos.
- Streamlit for catalog overview, semantic review, relationships, query studio, validation, and decision history.

## Integration seams

Each external dependency gets a fake implementation before its real adapter. This permits modules to develop independently while preserving one final composition path.

```text
FakeCatalogPort  ─┐
FakeQueryExecutor ├─ application tests
FakeIntentParser ─┘

DataHubCatalogAdapter ─┐
PostgresQueryExecutor  ├─ bootstrap / integration tests
OpenAIIntentParser ────┘
```

## Data flow

```text
DataHub metadata + bounded source profile
    → candidate evidence
    → human-approved logical context
    → AnalyticalRequest
    → semantic resolution
    → ResolvedQueryPlan
    → deterministic SQL compiler
    → SQL policy/AST validation
    → read-only preview
    → result/rejection report
    → approved context write-back
```

## State ownership

- Physical metadata source of truth: DataHub and source database.
- Draft decisions: local store during review.
- Approved semantic definitions and decision documents: DataHub.
- Evaluation fixtures: versioned YAML in `demo/ground_truth`.
- UI session state: transient navigation and current draft only.

## Failure strategy

External failures become typed application errors. The UI must distinguish:

- unavailable service;
- insufficient metadata;
- ambiguous semantic request;
- unapproved mapping or join;
- rejected SQL policy;
- source-quality rejection;
- query timeout;
- DataHub write rejection.

No failure falls back to ungoverned SQL.
