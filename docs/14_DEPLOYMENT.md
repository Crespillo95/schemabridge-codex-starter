# Deployment strategy

## Judge requirements

The final project needs an easy, free path for judges to test. A judge may also rely only on the written submission and video, so deployment is one layer of evidence, not the only one.

## Recommended topology

```text
Public Streamlit application
        │
        ├── packaged deterministic demo context / fake LLM fallback
        ├── hosted or reachable PostgreSQL read-only demo
        └── DataHub Core endpoint or recorded read adapter when hosting Core is impractical
```

The strongest deployment keeps real DataHub in the live path. If free-host resource limits make that unreliable, provide:

1. a stable deterministic live demo that preserves the same ports and clearly labels recorded DataHub context;
2. a reproducible local full-DataHub path;
3. video evidence of the real full integration;
4. examples of actual write-back artifacts.

Do not misrepresent a fake as a live DataHub integration.

## Deployment candidates

M17 must evaluate current free/low-cost capabilities, resource limits, sleep behavior, secrets management, and public access before choosing. Do not lock the repository to a vendor earlier.

## Health and demo mode

Final application should expose:

- source connection health;
- DataHub connection health;
- LLM adapter mode: live or deterministic;
- write-back mode: disabled, proposal, or direct-approved;
- build/version identifier;
- a one-click demo reset that changes only synthetic/draft state.

## Secrets

Use the hosting platform secret store. Never commit tokens. Service accounts should be scoped to demo assets. Rotate credentials after recording or public testing when appropriate.

## Release gate

- cold start tested;
- mobile is optional, desktop browser required;
- no login preferred; otherwise clear test credentials;
- no paid user action;
- deployment remains available through judging;
- demo data reset is reliable;
- application degrades honestly when an integration is unavailable.
