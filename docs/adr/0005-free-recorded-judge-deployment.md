# ADR 0005: Free recorded-mode judge deployment on Hugging Face Spaces

- Status: accepted for M17 packaging; public rollout pending the release gate
- Date: 2026-07-22

## Context

The judge path must be public, free, understandable without private credentials, and honest about
which integrations are live. The full local path runs PostgreSQL plus the multi-service DataHub
quickstart, while free application platforms provide one constrained, ephemeral application
runtime.

Official platform documentation was reviewed on 2026-07-22:

| Candidate | Free resources and public access | Sleep/persistence | M17 assessment |
|---|---|---|---|
| [Hugging Face Docker Spaces](https://huggingface.co/docs/hub/main/spaces-overview) | Public app and source; CPU Basic is 2 vCPU, 16 GB RAM, and 50 GB ephemeral disk; settings support variables and secrets | Free hardware sleeps after inactivity; disk is not persistent | Selected: most headroom, Docker-native packaging, and no runtime secret is required for recorded mode |
| [Streamlit Community Cloud](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app) | GitHub-backed public Streamlit deployment; approximate shared limits range up to 2 CPU, 2.7 GB RAM, and 50 GB storage | Sleeps after 12 hours without traffic and presents a wake page | Viable fallback, but lower/variable memory and platform-specific wake UX are less predictable |
| [Render Free](https://render.com/docs/free) | Public TLS web service; 750 free instance hours per workspace/month | Sleeps after 15 minutes, approximately one-minute wake, ephemeral filesystem; free Postgres expires after 30 days | Rejected for judge stability and durable demo-state expectations |

The official [DataHub quickstart](https://docs.datahub.com/) is a Docker-based local stack rather
than a single application process. Running that multi-service stack inside an unprivileged,
ephemeral Space would add a nested-container architecture, slow recovery, and mutable metadata
state that this milestone cannot honestly call stable. This is an inference from the two official
deployment models and the repository's verified local stack, not a claim that DataHub cannot run on
cloud infrastructure.

## Decision

Package a public Streamlit application as a pinned Docker image for Hugging Face CPU Basic. The
hosted default uses:

- recorded, sanitized DataHub catalog and semantic evidence;
- deterministic typed fake intent parsing;
- live deterministic SQL compilation plus independent AST guarding;
- a fingerprint-bound recording of the north-star PostgreSQL result and rejected-source evidence;
- fake local publication with no DataHub mutation.

The recorded execution adapter fails closed for any other query or rejection-check fingerprint.
The UI labels each boundary and displays a release identifier. The full live DataHub and read-only
PostgreSQL integration remains the documented local path.

The hosted runtime needs no DataHub, database, or LLM secret. A Hugging Face write token is an
operator deployment credential only; it must remain in the operator credential store or CI secret
and must never enter the image or Space runtime.

## Consequences

- Judges can run the canonical scenario after a free-Space wake without payment or private service
  credentials once the operator publishes the release.
- The public result is evidence from a versioned synthetic recording, not a live database query.
- SQLite workflow state is ephemeral across Space rebuilds/restarts; **Reset demo** creates a fresh
  synthetic workflow and never mutates source or DataHub data.
- Free-Space sleep and cold-start duration must be measured after deployment and disclosed.
- Public deployment remains blocked until the M16 remediation is committed, strict clean-room proof
  passes for that commit, Hugging Face authentication is available, and the external browser/device
  test is recorded.
