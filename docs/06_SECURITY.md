# Security and safety model

## Threats in scope

- prompt injection attempting to bypass query policy;
- LLM hallucination of assets, columns, joins, or transformations;
- SQL injection through filter values or identifiers;
- statement smuggling and multiple statements;
- DDL/DML or utility commands disguised in a CTE;
- Cartesian joins and unbounded scans;
- fanout-induced metric corruption;
- identifier precision loss;
- accidental source writes;
- DataHub mutations without approval;
- secrets or proprietary data in repository/logs;
- denial of service through expensive queries.

## Defense in depth

### Source database

- dedicated `schemabridge_reader` role;
- `default_transaction_read_only=on`;
- explicit `SELECT` grants only;
- statement and lock timeouts;
- application opens read-only transactions;
- no admin credentials in runtime configuration.

### Query construction

- no executable raw SQL from the user or LLM;
- identifiers selected from an allowlist;
- values bound as parameters;
- restricted typed IR;
- deterministic compiler;
- unsupported operations fail closed.

### Natural-language boundary

- `IntentParserPort` returns only a Pydantic-validated typed intent schema with extra fields
  forbidden; no SQL, tool-call, approval, physical-asset, or execution output field exists;
- prompts contain only four approved logical fields, two model definitions, one governed join
  summary, closed operations, and bounded logical role values—never credentials or raw samples;
- the live Responses adapter sets `store=false`, supplies a structured-output type, and registers no
  tools; the deterministic fake requires no API key;
- application validation independently checks language, vocabulary membership, current approved
  context, allowed role values, and SQL-like filter payloads before confirmation;
- instruction-like user text remains untrusted business input and produces a typed unresolved/error
  preview; it cannot modify policy or reach planning;
- explicit confirmation is fingerprint-bound. A changed interpretation, invalid output, unavailable
  alternative, or unresolved ambiguity fails before the semantic planner;
- M11 planning after confirmation is pure fingerprint comparison only; its CLI never compiles or
  executes SQL and cannot mutate DataHub or source databases.

### SQL guard

Required rejection cases:

```text
INSERT / UPDATE / DELETE / MERGE
CREATE / ALTER / DROP / TRUNCATE
COPY / CALL / DO / SET / GRANT / REVOKE
multiple statements
SELECT INTO
unsafe functions or extensions if introduced
unknown schemas, tables, or columns
CROSS JOIN or join without a predicate
more than three tables
missing result limit for preview
comments or encodings used to conceal a second statement
```

The guard must parse the final compiled SQL again; trusting the compiler alone is insufficient.

### Verified M03 SQL boundary

M03 uses SQLGlot only inside the SQL adapters. The compiler constructs a typed AST, and an
independent guard reparses the rendered PostgreSQL before producing a `ValidatedQuery`. Stable
rejection codes cover parse failures, comments, multiple/non-read-only/forbidden statements,
destructive CTEs, `SELECT INTO`, recursive CTEs, wildcard projection, unknown/repeated assets,
duplicate aliases, unknown columns, unsafe functions, Cartesian or insufficient joins, fourth
tables, missing/excessive limits, and parameter-count mismatches.

Join validation is scope-aware: each explicit equality predicate must connect the newly joined
relation to a relation already available in that `SELECT`. Locking reads such as `FOR UPDATE` are
also rejected as forbidden statements.

Filter and approved mapping values use bound `%s` parameters. Identifiers can originate only from
validated domain references and are checked again against the final-SQL allowlist. The preview
adapter independently enforces a read-only transaction, transaction-local timeout, and fetched-row
cap even if another layer regresses.

### Semantic safety

- evidence and confidence do not equal approval;
- join contracts are versioned and approved;
- cardinality/fanout policy is mandatory;
- unsafe floats are rejected rather than truncated;
- leading-zero semantics require an explicit policy;
- ambiguous requests require confirmation;
- rejected records are reported.

### DataHub mutations

- read-only by default;
- mutation capability toggled explicitly;
- runtime approval object passed to the write use case;
- MCP/client configured to prompt on writes;
- every write records actor, target, old/new summary, and result;
- no destructive delete operation in the MVP.

### Secrets and privacy

- `.env` is ignored;
- logs redact tokens and connection passwords;
- raw samples are displayed only in bounded synthetic demo contexts;
- public examples use synthetic names and values;
- screenshots are reviewed before publication.

## Security test matrix

At minimum test:

- semicolon + second statement;
- destructive CTE;
- quoted malicious identifier;
- injected filter value;
- Cartesian join;
- unknown table;
- fourth table;
- timeout path;
- database write attempt with reader role;
- unapproved DataHub mutation;
- non-integral float, NaN, Infinity, padded ID, empty string, and NULL;
- one-to-many overcount regression.
- hallucinated logical field and unsupported intent enum;
- model-output extra SQL field and SQL-like filter value;
- prompt-injection requests in Spanish and English;
- changed interpretation fingerprint and unconfirmed ambiguity.

## Incident rule

When a safety control fails, stop feature work, add a regression test first, then fix the narrowest responsible layer and document the decision.
