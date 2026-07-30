# Capacity, readiness, and restart loop

## Detection

Identify the closed service or resource category and its bounded utilization/readiness sample.
Never add pod, tenant, actor, table, field, or arbitrary exception labels to diagnose cardinality.

## Containment

Stop promotion, retain configured admission limits, and drain the failing revision. Do not bypass
resource, pool, queue, or result bounds.

## Recovery

Remove the failed dependency or deploy the last verified digest. Scale only within reviewed
database and queue budgets, then prove liveness/readiness and a complete stable window.

## Evidence

Record service/resource category, immutable revision, safe utilization trend, restart count,
operator action, UTC timeline, and the complete resolved window.
