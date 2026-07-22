# ADR 0001: Ports and adapters

- Status: accepted
- Date: 2026-07-21

## Context

SchemaBridge depends on DataHub, PostgreSQL, an LLM, SQL parsing, Streamlit, and local persistence. These integrations will evolve independently during a short hackathon.

## Decision

Use a ports-and-adapters architecture with a pure domain, application use cases against protocols, external adapters, and one composition root.

## Consequences

- Modules can be built and tested with fakes.
- DataHub version changes stay behind an adapter.
- Initial code is more explicit than a single Streamlit script.
- Cross-layer shortcuts are rejected even when they appear faster.
