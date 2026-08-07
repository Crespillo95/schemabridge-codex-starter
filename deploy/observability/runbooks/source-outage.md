# Source outage or timeout

## Detection

Confirm the closed capability and stable `timeout` or `error` outcome. Use safe counts and
correlation identifiers only; source hostnames, SQL, parameters, and sampled values are private.

## Containment

Stop new work for the affected route while leaving unrelated capabilities isolated. Preserve
read-only enforcement and bounded retries; do not widen timeouts or bypass cost admission.

## Recovery

Verify TLS and the exact immutable route through preflight, then resume with a bounded canary.
Require a fresh authorization when the governed contract says prior confirmation is stale.

## Evidence

Record the capability, route revision fingerprint, UTC outage window, safe outcome counts, canary
result, and operator decision without source topology or credentials.
