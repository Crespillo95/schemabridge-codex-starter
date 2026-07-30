# Backup freshness and recovery drill

## Detection

Confirm backup or restore-drill age and the stable verification outcome. Do not place archive
paths, database names, storage endpoints, keys, or manifest content in telemetry.
Missing backup-age or restore-drill-age series are incidents and block production GO; absence is
never evidence of freshness.

## Containment

Block destructive expiry and release cutover when the newest verified pair is stale or invalid.
Quarantine any mismatched archive/manifest pair; never restore over the active database.

## Recovery

Create a new signed transaction-consistent backup with the dedicated identity, restore only into
a distinct empty target, verify hashes/schema/state/audit chain, and require external cutover.

## Evidence

Record RPO/RTO result, sanitized durations and ages, artifact digests, source revision, target
emptiness proof, verification result, and explicit cutover or rollback decision.
