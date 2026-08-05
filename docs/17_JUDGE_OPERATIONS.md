# M17 judge deployment and operations

## Current release status

The M17 image, local container health, restart, fingerprint-bound recorded north-star path, and
service-free acceptance test are verified. **No public URL has been published or tested yet.** The
repository contains an uncommitted M18 release candidate, the strict release commit proof is
pending, `hf` CLI 1.26.0 is installed but not authenticated, and the incognito/separate-network
test has not run. Do not present the local checks below as public deployment evidence.

## Selected topology

```text
Public Hugging Face Docker Space (free CPU Basic)
    Streamlit UI
      ├─ recorded sanitized DataHub catalog/evidence
      ├─ deterministic typed fake intent parser
      ├─ typed plan → deterministic PostgreSQL → independent SQL AST guard
      ├─ exact fingerprint-bound result/rejection recording
      └─ fake local publication (ephemeral SQLite, explicit approval)

Operator workstation
    full local PostgreSQL reader + DataHub Core/MCP + approval-gated DataHub writers
```

The public runtime has no source database and performs no DataHub mutation. Selecting a live mode
without its local dependency produces a typed configuration/unavailable state; it never switches
back to a recording silently.

## Verified local release commands

Build the Linux AMD64 Docker package with `git describe --always --dirty` shown in the UI. A
development image therefore cannot masquerade as a clean release:

```bash
make judge-build
docker run -d --name schemabridge-m17-judge \
  -p 127.0.0.1:7860:7860 schemabridge-judge:local
make judge-smoke
docker inspect schemabridge-m17-judge \
  --format 'user={{.Config.User}} health={{.State.Health.Status}} image={{.Image}}'
.venv/bin/pytest -m acceptance -k deployed
```

Expected north-star evidence is `2`, `1`, `1`, with `127.5`, `NaN`, and `NULL` rejected under their
stable codes. The UI must say **Recorded synthetic PostgreSQL observation**, **Recorded catalog**,
**Deterministic fake parser**, and **Fake local publication**. `make judge-smoke` checks both the
Streamlit health endpoint and root page.

The 2026-07-22 local image used pinned Python 3.13.13. The final `linux/amd64` build ran through
Docker Desktop emulation as UID/name `user`, became healthy, and passed the smoke test. A stopped
native-development container restarted and passed smoke in 0.12 seconds; that is a local process
restart measurement, not a Hugging Face sleep/cold-start claim.

## Clean release packaging and deployment

Do not deploy the dirty working tree. After operator review and a focused commit:

```bash
SCHEMABRIDGE_RELEASE_DATAHUB_CONFIRMATION=publish-approved-registry-version \
  make release-clean
release_commit="$(git rev-parse --verify HEAD)"
staging_directory="$(mktemp -d /tmp/schemabridge-space.XXXXXX)/space"
scripts/package_huggingface_space.sh "$release_commit" "$staging_directory"
```

The confirmation authorizes only the project-owned synthetic DataHub reset and the exact
`registry-prepare` fingerprint publication recorded by the audit ledger. Missing or mismatched
confirmation fails before the clean-room changes service state.

The package script first proves the chosen commit contains every required M17 path, exports only
that commit, installs that commit's Docker Space card as the root README, and records the exact
commit in `RELEASE_COMMIT`. It fails closed on the current pre-M17 `HEAD`. Inspect the successful
bundle before upload.

Install the current `hf` CLI outside the project environment using the official
[CLI instructions](https://huggingface.co/docs/huggingface_hub/en/guides/cli), authenticate with a
fine-grained token allowed to write only the intended Space, and keep that token out of shell
history, Git, the Docker build, and Space runtime. Then:

```bash
hf auth whoami
hf repos create OWNER/schemabridge-judge --type space --space-sdk docker --exist-ok
hf upload OWNER/schemabridge-judge "$staging_directory" . \
  --type space --commit-message "Deploy SchemaBridge $release_commit"
hf spaces info OWNER/schemabridge-judge --expand sdk,runtime,sha
SCHEMABRIDGE_PUBLIC_URL='PASTE_ACTUAL_SPACE_URL'
PUBLIC_URL="$SCHEMABRIDGE_PUBLIC_URL" make judge-smoke
```

In Space settings, add the non-secret variable `SCHEMABRIDGE_RELEASE_REF=$release_commit`. Do not
add `DATABASE_URL`, DataHub tokens, or LLM keys to the recorded deployment. The deployment commands
above remain unexecuted because a scoped Hugging Face login, clean release commit, and target Space
are absent. The installed CLI itself is not a deployment credential.

## Reset, sleep, and uptime

- **Reset demo** starts a new deterministic synthetic workflow and changes no source/DataHub data.
- A rebuild/restart may erase ephemeral SQLite state. Reload and use **Reset demo**; the versioned
  catalog, planning, result, and rejection fixtures remain in the image.
- Free Hugging Face hardware sleeps after inactivity. Shortly before judging, open the URL, wait for
  the Space to become running, and run `PUBLIC_URL=... make judge-smoke`.
- Measure an actual wake with `/usr/bin/time -p .venv/bin/python scripts/smoke_deployment.py
  --url "$PUBLIC_URL" --attempts 60 --interval-seconds 5`; record platform state and elapsed time.
- No synthetic keepalive or paid upgrade is assumed.

## Dependency-failure drill

The public recorded path has no external data/LLM dependency to disconnect. To prove honest mode
handling, select **Live read-only PostgreSQL** in a runtime without `DATABASE_URL` and load the
demo. Composition fails before a workflow or query can start. Expected result:

```text
integration_configuration_unavailable
The selected integration is not configured or reachable.
```

There must be no recorded result after that live selection. For the full local drill, stop only the
project PostgreSQL container with `make demo-down`, choose live execution, verify the typed failure,
then restore with `make demo-up` and `make demo-health`.

## Troubleshooting

- `recorded execution is available only...`: compiler/query-policy drift changed the guarded query.
  Regenerate the recording only from the approved synthetic source, review the diff, and update both
  fingerprints; never loosen the comparison.
- Space build fails at the base image: confirm the pinned multi-platform Python digest still resolves
  and review upstream provenance before changing it.
- Space loops or loses workflows: free disk is ephemeral. Reset the demo; do not add hidden durable
  state or credentials to the public fallback.
- Health is `ok` but the UI errors: inspect the visible mode cards and typed error. Do not switch
  adapters automatically.
- `hf auth whoami` fails: provision/rotate the scoped deployment token outside Git. Runtime needs no
  token.

## Rollback and redeploy

Choose the last reviewed release commit, rebuild/package it, inspect `RELEASE_COMMIT`, upload it with
an explicit rollback message, update the release-ref Space variable, and rerun public smoke plus the
manual judge scenario. Do not delete the Space or rewrite the source repository history.

```bash
scripts/package_huggingface_space.sh LAST_GOOD_COMMIT "$staging_directory"
hf upload OWNER/schemabridge-judge "$staging_directory" . \
  --type space --commit-message "Rollback SchemaBridge to LAST_GOOD_COMMIT"
SCHEMABRIDGE_PUBLIC_URL='PASTE_ACTUAL_SPACE_URL'
PUBLIC_URL="$SCHEMABRIDGE_PUBLIC_URL" make judge-smoke
```

## Operator acceptance record to complete

1. Open the published URL in an incognito browser on a separate network/device and run all three
   governed actions. Record URL, release ref, device/network, time, result, rejections, and labels.
2. Allow/pause the free Space so it sleeps, wake it, record cold-start duration, and rerun smoke.
3. Run the dependency-failure drill and verify no silent fallback.
4. Confirm the Space is public, requires no judge login/payment, and its runtime settings contain no
   database, DataHub, LLM, or administrator credential.

## Full local DataHub path

The full integration remains the M04–M16 operator path in `docs/12_RUNBOOK.md`: start/reset the
pinned PostgreSQL and DataHub stacks, initialize admin locally, ingest only synthetic schemas,
provision scoped MCP/writer identities into ignored mode-0600 files, run health/catalog/MCP checks,
and execute the live acceptance path. The hosted fallback does not replace or alter those commands.
