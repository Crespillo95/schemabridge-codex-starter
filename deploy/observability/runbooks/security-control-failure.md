# Runtime identity, secret, TLS, or capability failure

## Detection

Use only the closed `control`, `reason`, service, stable error code, and bounded correlation
identifier. Treat any suspected secret exposure as compromise even when source I/O was denied.

## Containment

Disable the affected route revision, stop new claims, isolate the workload identity, and preserve
sanitized audit evidence. Never print or test a credential in a shell, log, metric, browser, or
ticket.

## Recovery

Issue a new external secret version and workload authorization, verify it independently, activate
a new immutable route revision, drain the former target, and revoke the old capability.

## Evidence

Record public revision fingerprints, identity role, stable failure category, UTC decisions,
revocation proof, and a clean secret scan. Do not record secret paths, tokens, endpoints, DSNs, or
provider payloads.
