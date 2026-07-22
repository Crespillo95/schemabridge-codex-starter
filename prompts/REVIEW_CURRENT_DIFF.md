# Review the current SchemaBridge diff

Do not implement new features. Read `AGENTS.md`, relevant nested instructions, architecture/security documents, the active milestone plan, and the current `git diff`.

Review in this priority order:

1. source-data or DataHub mutations without correct approval;
2. raw/unvalidated LLM output or SQL injection paths;
3. SQL guard gaps, fanout errors, unsafe identifier conversion;
4. ports-and-adapters dependency violations;
5. correctness, error handling, idempotency, and partial failures;
6. test omissions and misleading docs;
7. secrets, proprietary data, and license issues.

For every finding provide severity, file/line, evidence, reproduction or failing test, and the smallest fix. Do not report style-only noise. If no material finding exists, state what you checked and the residual risks.
