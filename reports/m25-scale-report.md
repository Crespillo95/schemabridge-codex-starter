# M25 PostgreSQL catalog scale acceptance report

> Local regression evidence only. This is not a production SLO or availability claim.

- Measured at: `2026-07-23T22:13:57.935067+00:00`
- Evidence profile: `postgres-acceptance`
- Synthetic preflight passed: `None`
- PostgreSQL acceptance: `passed` (passed: `True`).
- Backend: `postgres-catalog-v4` (`postgresql-keyset`)
- Indexed PostgreSQL read: `true`
- Cache context: `warm-after-correctness`
- Pool max / replicas / deployment capacity: `16` / `1` / `16`
- Platform: `Darwin 25.5.0 arm64`; Python `3.13.13`; logical CPUs `10`

## Correctness

| Case | Assets | Page size | Pages | Max rows read | Max materialized | Passed |
|---|---:|---:|---:|---:|---:|---|
| small | 10 | 1 | 10 | 2 | 1 | True |
| small | 10 | 17 | 1 | 10 | 10 | True |
| small | 10 | 50 | 1 | 10 | 10 | True |
| large | 5434 | 1 | 5434 | 2 | 1 | True |
| large | 5434 | 17 | 320 | 18 | 17 | True |
| large | 5434 | 50 | 109 | 51 | 50 | True |

## Memory

- Heap delta, large minus small bounded page: `126601` bytes (budget `16777216`).
- Peak RSS growth after the large bounded page: `0` bytes (budget `67108864`).
- Memory regression result: `True`.

## Load

- Reads / concurrency / page size: `5000` / `16` / `17`.
- Unexpected errors: `0`.
- Latency p50 / p95 / p99 / max: `26.5` / `41.762` / `54.951` / `81.658` ms.
- Pool wait: `measured`; observations `5000`.
- Load regression result: `True`.

## Reviewed index plan

- Status: `reviewed_postgres_index_plan`.
- Passed: `True`.
- Planner control: `default_postgres_planner` (no planner overrides).
- Field probe cardinality: `64`.
- SQL recorded: `false`; parameters recorded: `false`.

## Operated PostgreSQL evidence

- Status: `measured`.
- Measurement basis: `durable-active-generation-aggregate`.
- PostgreSQL version: `16.13`.
- Active connections / multi-connection observed: `2` / `True`.
- Assets / fields / minimum fields: `5434` / `41028` / `40000`.
- Full refresh pages / maximum request-to-completion / budget: `110` / `29.685331` s / `60.0` s.
- Operated evidence result: `True`.
- Identifiers, SQL, parameters, and timestamps recorded: `false`.

## Limitations

- Local regression evidence only; this report is not a production SLO.
- PostgreSQL acceptance remains local regression evidence, not a production SLO.
- Peak RSS is process-level and allocator/platform dependent.
- No availability, autoscaling, multi-region, or production-traffic claim is made.

Overall required result for this evidence profile: **PASS**
