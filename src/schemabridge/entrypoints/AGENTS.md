# Entrypoint instructions

- Entrypoints contain presentation and input translation only.
- Do not duplicate domain rules, SQL policy, matching weights, or approval logic in UI callbacks.
- Show interpretation, evidence, confidence, risks, assumptions, fanout, and validation before execution.
- Never hide rejected records or silently correct ambiguous requests.
- Do not expose stack traces, tokens, credentials, or raw connection strings to users.
- Keep Streamlit session state serializable and scoped to drafts, not as the source of truth.
