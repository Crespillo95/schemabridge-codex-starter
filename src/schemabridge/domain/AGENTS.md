# Domain-layer instructions

- Keep this package pure and deterministic.
- Allowed imports: Python standard library, Pydantic, and other domain modules.
- Forbidden imports: DataHub, SQLAlchemy, psycopg, SQLGlot, OpenAI, Streamlit, Typer, filesystem, network, environment, clock, and random generators unless injected as values.
- Model invariants in constructors or Pydantic validators.
- Prefer immutable/frozen value objects where practical.
- Domain failures use explicit typed exceptions or result models, never generic `Exception`.
- Every normalization and cardinality rule requires boundary-case unit tests.
