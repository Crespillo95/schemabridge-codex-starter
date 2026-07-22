# ADR 0004: PostgreSQL-first executable MVP

- Status: accepted
- Date: 2026-07-21

## Context

Supporting many dialects would dilute the safety and demo quality of a solo hackathon project.

## Decision

Compile and execute PostgreSQL only in the MVP. Keep the query IR vendor-neutral enough to add dialect adapters later.

## Consequences

- The compiler and guard can be thoroughly tested.
- Cross-dialect claims are explicitly excluded from the submission.
