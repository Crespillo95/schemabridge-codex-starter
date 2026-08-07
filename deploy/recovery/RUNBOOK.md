# Control-plane backup, retention, recovery, and rollback

This runbook operates only the SchemaBridge control plane. It never connects to or writes a source
database. The production floor is an hourly backup, RPO no greater than 60 minutes, RTO no greater
than four hours, and retention of 24 hourly, 35 daily, and 12 monthly recovery points.

## Operator commands

Validate the checked-in policy before scheduling or operating recovery:

```bash
make m29-recovery-policy-check
```

The retention command accepts only a key file. The JSON file must have schema version
`schemabridge.recovery-audit-key.v1`, a version such as `v1`, and the M23 audit-signing key under
`key`. It must be a regular file owned by the current operator, readable only by that owner
(`chmod 600` or stricter), and must not be a symlink. Never put key material in an argument,
environment variable, log, or evidence record.

Planning is the default and does not delete files:

```bash
.venv/bin/python scripts/m29_recovery.py retention-plan \
  --root /secured/control-plane-backups \
  --key-file /secured/recovery-audit-key.json
```

Record and independently review the returned `policy_fingerprint` and `plan_fingerprint`. A later
execution must present both exact values, `--execute`, and the exact non-secret confirmation. The
operator re-verifies the complete current set; any policy, decision, freshness, signature, or
artifact change rejects execution:

```bash
.venv/bin/python scripts/m29_recovery.py retention-plan \
  --root /secured/control-plane-backups \
  --key-file /secured/recovery-audit-key.json \
  --execute \
  --confirmation 'QUARANTINE EXPIRED VERIFIED BACKUPS' \
  --reviewed-policy-fingerprint '<64-lowercase-hex-from-reviewed-plan>' \
  --reviewed-plan-fingerprint '<64-lowercase-hex-from-reviewed-plan>'
```

Success and failure output is bounded JSON. It includes counts, fingerprints, timings, and stable
outcomes only; it excludes paths, artifact names, manifest content, keys, endpoints, and database
connection details. Expired pairs are atomically moved into an owner-only recoverable quarantine
under the backup root; they are never unlinked by this command. Do not purge that quarantine until
the reviewed recovery window and independent immutable remote-copy verification are complete.
These commands do not implement remote transfer, remote object-lock expiry, quarantine purge,
restore, or cutover.

## Backup

1. Use the dedicated backup workload identity and the existing M23 transaction-consistent backup.
2. Write the custom archive and signed manifest into owner-only temporary storage.
3. Verify archive hash, manifest HMAC, migration identity, state digest, and table counts.
4. Encrypt and transfer the pair to immutable remote retention. Application workloads must not
   receive backup-store credentials.
5. Record only UTC timing, age, policy/artifact fingerprints, sizes, counts, and stable outcomes.
   Never record paths, database URLs, endpoints, keys, commands, or manifest payloads.

## Retention

1. Run the planner in its default dry-run mode.
2. Stop if the directory is not owner-only, contains an unknown file, orphan, symlink, changed
   permission, invalid signature, archive mismatch, duplicate identity, or future timestamp.
3. Confirm the newest verified pair satisfies the 60-minute RPO.
4. Review the union of hourly, daily, and monthly retained identities and record the exact policy
   and plan fingerprints.
5. A quarantine run requires a new complete verification, both reviewed fingerprints, and the
   exact confirmation `QUARANTINE EXPIRED VERIFIED BACKUPS`. A changed set invalidates the plan.
6. Treat the owner-only quarantine as recoverable deletion. A partial move is rolled back; a
   changed or malformed quarantine blocks every later retention plan. Purge is a separate,
   independently approved operation after remote-copy and recovery-window verification.
7. Remote object-lock expiry remains a dedicated recovery-workload operation; never grant delete
   authority to a web, API, worker, catalog, profile, or reconciler workload.

## Restore drill

1. Provision a distinct empty PostgreSQL database with a dedicated restore identity. It must not
   be the active control database or any source database.
2. Verify the signed pair before invoking restore.
3. Restore with the existing M23 single-transaction primitive.
4. Verify exact migration version/checksum, state digest, table counts, every workspace audit
   chain, pointers, transitions, pending outbox, and quarantine counts.
5. Measure backup, restore, total duration, backup age, RPO, and RTO.
6. Do not cut over automatically. Preserve the active database until an external cutover authority
   reviews the complete sanitized evidence.

## Release rollback

- If the release is healthy, retain it.
- If application bytes fail and the schema remains forward compatible, roll back only the
  application/image digest.
- If the schema is not forward compatible, restore a separately verified pre-migration backup to a
  fresh target and require explicit cutover.
- If neither safe option is proven, block rollback. Automatic down-migration is forbidden.

## Evidence

Retain policy fingerprint, source revision, archive/SBOM-independent artifact fingerprint,
schema/state digests, verified counts, UTC start/completion, bounded durations, backup age,
RPO/RTO result, selected rollback decision, and external cutover decision. The evidence must not
contain a path, DSN, credential, secret, endpoint, SQL, source value, result row, or raw manifest.
