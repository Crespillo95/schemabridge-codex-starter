# M17 judge deployment and operations

## Current release status

The frozen M18 image is publicly deployed at https://rcr-ia.eu/schemabridge/. Public HTTPS and
health smoke pass, and an anonymous independent browser completed the exact north-star journey with
release `c5817af`, result `2/1/1`, and the three visible identifier rejections. The executable source
remains the annotated release tag; VPS/Nginx operations and documentation do not change it. A
second-network/device repetition and unfamiliar-reviewer answers remain operator evidence tasks.

## Selected topology

```text
Existing rcr-ia.eu Nginx TLS virtual host
    /schemabridge/ → 127.0.0.1:7860
        resource-bounded read-only Docker container
        Streamlit UI
      ├─ recorded sanitized DataHub catalog/evidence
      ├─ deterministic typed fake intent parser
      ├─ typed plan → deterministic PostgreSQL → independent SQL AST guard
      ├─ exact fingerprint-bound result/rejection recording
      └─ fake local publication (ephemeral SQLite, explicit approval)

Operator workstation
    full local PostgreSQL reader + DataHub Core/MCP + approval-gated DataHub writers
```

The public runtime has no source database and performs no DataHub mutation. Its port is bound only
to loopback; Nginx exposes the path through the existing certificate and preserves the root site.
Selecting a live mode
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

## Verified VPS deployment

The deployed checkout is detached at exact source commit
`c5817af6d01b8a98cd7f1950d57e1be667614696` in `/opt/schemabridge/releases/c5817af`. Build only that
commit and keep the public container isolated from the other VPS services:

```bash
cd /opt/schemabridge/releases/c5817af
test "$(git rev-parse HEAD)" = c5817af6d01b8a98cd7f1950d57e1be667614696
test -z "$(git status --short)"
sudo docker build \
  --build-arg SCHEMABRIDGE_RELEASE_REF=c5817af \
  -t schemabridge-judge:c5817af .
sudo docker run -d --name schemabridge-judge \
  --restart unless-stopped \
  --cpus 1.5 --memory 2g --memory-swap 2g --pids-limit 256 \
  --read-only --tmpfs /tmp:rw,nosuid,size=256m \
  -e STREAMLIT_SERVER_BASE_URL_PATH=schemabridge \
  -p 127.0.0.1:7860:7860 \
  schemabridge-judge:c5817af
```

The Nginx virtual host adds only the exact redirect `/schemabridge` to `/schemabridge/` and a
`^~ /schemabridge/` reverse proxy to `http://127.0.0.1:7860`, including WebSocket upgrade headers.
The pre-change configuration backup is
`/etc/nginx/sites-available/01-rcr-ia.eu-final.pre-schemabridge-20260807T1543Z`. Validate before
every reload:

```bash
sudo nginx -t
sudo systemctl reload nginx
curl --fail --show-error --silent https://rcr-ia.eu/schemabridge/_stcore/health
.venv/bin/python scripts/smoke_deployment.py \
  --url https://rcr-ia.eu/schemabridge/ --attempts 5 --interval-seconds 2
```

Do not add `DATABASE_URL`, DataHub tokens, LLM keys, or administrator credentials. Docker reports
the deployed image as `sha256:fbd970e8a3618769e35ee8df166f267810c08ca2e597e45d52ab2fd02780558b`;
the running container is non-root user `user`, healthy, root-filesystem read-only, and restartable
without rebuilding.

## Live PostgreSQL judge promotion

`docker-compose.judge-live.yml` is the reviewed promotion path from recorded source evidence to a
real synthetic PostgreSQL preview. It deliberately keeps the catalog/registry recorded, intent
deterministic, and publication disabled. PostgreSQL has no published host port; only the app can
reach its internal network, and the app receives only the `schemabridge_reader` credential.

On the existing VPS, create ignored mode-0600 secret files with independent random values and a
mode-0600 environment file that contains only their paths, then validate the rendered configuration
before changing the current container:

```bash
cd /opt/schemabridge/releases/<reviewed-commit>
umask 077
mkdir -p .secrets/judge-live
openssl rand -hex 32 > .secrets/judge-live/admin
openssl rand -hex 32 > .secrets/judge-live/reader
printf 'SCHEMABRIDGE_JUDGE_DB_ADMIN_SECRET_FILE=.secrets/judge-live/admin\n' \
  > .env.judge-live
printf 'SCHEMABRIDGE_JUDGE_DB_READER_SECRET_FILE=.secrets/judge-live/reader\n' \
  >> .env.judge-live
printf 'SCHEMABRIDGE_RELEASE_REF=%s\n' "$(git rev-parse --short=12 HEAD)" \
  >> .env.judge-live
sudo docker compose --env-file .env.judge-live \
  -f docker-compose.judge-live.yml config --quiet
```

The operator must review the exact commit and create a recoverable backup of the current
`schemabridge-judge` container configuration before cutover. Start the isolated stack, confirm the
database role from inside the database container, and smoke the loopback path before reloading or
changing Nginx:

```bash
sudo docker compose --env-file .env.judge-live \
  -f docker-compose.judge-live.yml up -d --build --wait
sudo docker compose --env-file .env.judge-live \
  -f docker-compose.judge-live.yml exec -T postgres \
  psql -U schemabridge_reader -d schemabridge -Atc \
  'SHOW transaction_read_only; SELECT current_user;'
curl --fail --show-error --silent http://127.0.0.1:7860/schemabridge/_stcore/health
```

The expected database output is `on` and `schemabridge_reader`. The UI integration panel must show
**Live read-only PostgreSQL**, **Recorded catalog**, and **Publisher submission unavailable**. A
failed database health check or any different label is a failed promotion; do not fall back to the
recorded result under the live label.

The reader secret is mounted read-only into the two containers. `run_app.sh` validates it, builds
`DATABASE_URL` only inside the app process, and unsets the temporary shell value before starting
Streamlit. The rendered Compose configuration and Docker container configuration therefore contain
secret-file paths, not the credential or a credential-bearing DSN.

## Reset, restart, and uptime

- **Reset demo** starts a new deterministic synthetic workflow and changes no source/DataHub data.
- A rebuild/restart may erase ephemeral SQLite state. Reload and use **Reset demo**; the versioned
  catalog, planning, result, and rejection fixtures remain in the image.
- Docker uses `--restart unless-stopped`; the VPS does not intentionally sleep the demo.
- Before judging, run the public health and smoke commands above and inspect `docker ps` plus the
  last bounded container logs.
- Measure any actual restart with `/usr/bin/time -p .venv/bin/python scripts/smoke_deployment.py
  --url https://rcr-ia.eu/schemabridge/ --attempts 60 --interval-seconds 5`; record elapsed time.

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
- Image rebuild fails at the base image: confirm the pinned multi-platform Python digest still resolves
  and review upstream provenance before changing it.
- A workflow is lost after restart: the public state is intentionally ephemeral. Reset the demo;
  do not add hidden durable state or credentials to the public fallback.
- Health is `ok` but the UI errors: inspect the visible mode cards and typed error. Do not switch
  adapters automatically.
- Public path returns `502`: verify the container is healthy on loopback, then check the isolated
  Nginx location and bounded logs; do not alter unrelated virtual-host routes.

## Rollback and redeploy

Stop and remove only the `schemabridge-judge` container, restore the timestamped Nginx backup, test
and reload Nginx, then rebuild a reviewed frozen commit if a replacement is required. Do not delete
unrelated containers/services or rewrite repository history.

```bash
sudo docker stop schemabridge-judge
sudo docker rm schemabridge-judge
sudo cp /etc/nginx/sites-available/01-rcr-ia.eu-final.pre-schemabridge-20260807T1543Z \
  /etc/nginx/sites-available/01-rcr-ia.eu-final
sudo nginx -t
sudo systemctl reload nginx
```

## Operator acceptance record to complete

1. Operator-network independent-browser check passes on 2026-08-07. Repeat the published URL on a
   separate network/device and record URL, release ref, device/network, result, rejections, and labels.
2. Restart only the SchemaBridge container during a maintenance window, record cold-start duration,
   and rerun smoke.
3. Run the dependency-failure drill and verify no silent fallback.
4. Confirm the VPS path is public, requires no judge login/payment, and the container environment
   contains no database, DataHub, LLM, or administrator credential.

## Full local DataHub path

The full integration remains the M04–M16 operator path in `docs/12_RUNBOOK.md`: start/reset the
pinned PostgreSQL and DataHub stacks, initialize admin locally, ingest only synthetic schemas,
provision scoped MCP/writer identities into ignored mode-0600 files, run health/catalog/MCP checks,
and execute the live acceptance path. The hosted fallback does not replace or alter those commands.
