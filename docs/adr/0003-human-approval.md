# ADR 0003: Human approval for semantic and metadata changes

- Status: accepted
- Date: 2026-07-21

## Context

Names and value patterns cannot prove business equivalence. Leading zeros, identifiers, and joins may have hidden semantics.

## Decision

Candidate mappings and joins remain proposals until a human approves them. DataHub mutations require a separate explicit approval. Confidence thresholds control prioritization, not authorization.

## Consequences

- The workflow is governed rather than fully autonomous.
- Decisions are explainable and auditable.
- The UI must make review fast and show evidence, alternatives, and risks.
