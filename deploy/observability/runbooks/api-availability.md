# API availability and latency

## Detection

Confirm the exact alert window and inspect only `service`, `outcome`, stable `error_code`, bounded
`correlation_id`, readiness, restart, and capacity signals. Do not inspect request bodies, claims,
prompts, SQL, parameters, or result data.

## Containment

Stop promotion and drain the unhealthy revision. Keep source execution and metadata mutation
disabled if readiness or identity evidence is uncertain.

## Recovery

Restore the last verified immutable application revision or remove the exhausted dependency. Run
credential-free liveness/readiness checks, then a synthetic authenticated request before traffic
is restored.

## Evidence

Record the alert identity, revision digest, UTC timeline, safe metric snapshot, operator decision,
and the first complete healthy window. Never attach raw logs or request payloads.
