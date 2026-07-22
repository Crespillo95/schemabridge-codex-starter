# Adapter-layer instructions

- Implement application ports without leaking vendor models across the boundary.
- Convert external payloads into domain/application types at the edge.
- Log identifiers and operation names, never secrets or full sampled data by default.
- PostgreSQL adapter must enforce read-only transactions and statement timeouts independently of the SQL guard.
- DataHub mutation adapters require an explicit approval token/value from the use case.
- LLM adapters return typed structures and must validate model output before returning.
- Keep fake/in-memory adapters available for unit tests and judge demos when services are unavailable.
