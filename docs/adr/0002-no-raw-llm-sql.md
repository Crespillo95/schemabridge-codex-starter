# ADR 0002: No executable raw LLM SQL

- Status: accepted
- Date: 2026-07-21

## Context

The product accepts natural-language analytical requests. Executing raw model-generated SQL would make security, reproducibility, and semantic guarantees weak.

## Decision

The LLM may produce only a typed `AnalyticalRequest` and explanations. A governed planner resolves approved context, and a deterministic compiler emits SQL from a restricted IR. The final SQL is parsed and policy-validated before a read-only preview.

## Consequences

- Some advanced SQL is outside the MVP.
- Safety and tests are materially stronger.
- The product differentiates itself through governed planning rather than unconstrained text-to-SQL.
