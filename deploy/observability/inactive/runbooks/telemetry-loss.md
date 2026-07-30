# Telemetry export loss

## Detection

Confirm `error` or `dropped` on the closed telemetry export metric and inspect bounded buffer
utilization. Absence of telemetry is an incident signal, never evidence that the runtime is safe.
Production GO remains blocked until the export series and its authenticated scrape target are
present through a complete successful observation window.

## Containment

Preserve the bounded local buffer and stop promotion. Do not widen payload fields, remove TLS
verification, disable authentication, or retry an event rejected by the public schema.

## Recovery

Restore the authenticated TLS export path, send only validated sanitized events, and verify a
complete window with no failed or dropped batch. Discard any unvalidated payload rather than
replaying it.

## Evidence

Record safe batch counts, buffer utilization category, observer revision, UTC loss window,
recovery action, and the first complete loss-free window. Never attach buffered payloads.
