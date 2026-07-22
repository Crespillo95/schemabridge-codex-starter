# Test instructions

- Unit tests run without Docker, network, DataHub, PostgreSQL, or an LLM.
- Integration tests use local containers/fakes and carry the `integration` marker.
- Acceptance tests prove an observable user journey and carry the `acceptance` marker.
- Test behavior and invariants, not implementation details.
- Security regression tests are mandatory for every rejected SQL or unsafe identifier case.
- Never reduce a test expectation simply to match a broken implementation.
- Synthetic fixtures must be small, deterministic, and free of employer information.
