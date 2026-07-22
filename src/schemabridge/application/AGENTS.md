# Application-layer instructions

- Define use cases and `typing.Protocol` ports here.
- Depend only on `domain` and Python abstractions.
- Do not import concrete adapters, Streamlit, Typer, SQLAlchemy, psycopg, DataHub clients, or OpenAI clients.
- A use case coordinates domain behavior; it does not contain vendor-specific parsing.
- Return typed result objects that entrypoints can render.
- External failures must be translated into application-level errors with actionable context.
