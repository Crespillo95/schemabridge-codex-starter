# Audit, backup, or release integrity failure

## Detection

Use the closed integrity control and release-policy outcome. Treat a mismatch as a failed build or
verification, not as a warning that can be overridden silently.
Missing integrity or release-policy series block production GO until an authenticated producer
and a complete successful observation window are proven.

## Containment

Quarantine the candidate artifact, stop publication, preserve immutable digests, and deny cutover.
Do not rebuild from a dirty or non-exact revision.

## Recovery

Correct the reviewed source or dependency input, create a fresh clean artifact once, regenerate
its bound SBOM and provenance, and rerun the complete policy. For backup integrity, create and
verify a new pair rather than editing evidence.

## Evidence

Record source revision, artifact/SBOM/provenance digests, stable policy code, workflow identity,
approval, UTC timeline, and the subsequent clean policy result.
