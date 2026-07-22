# M16 independent audit: SQL and database security

Mode: bounded read-only review; the subagent made no edits.
Snapshot: pre-fix M16 working tree, 2026-07-21.
Owner of dispositions: main M16 agent.

## Ranked findings

### SQL-001 — High — Rejected-source reporting materialized an unbounded result

- Evidence: `src/schemabridge/adapters/postgres/rejections.py` used an unrestricted fetch for
  invalid source records.
- Reproduction: seed many rejected values, run the reporter, and observe memory/row materialization.
- Risk: a bounded preview could still be paired with an unbounded diagnostic read.
- Proposed regression: require SQL `LIMIT`, `fetchmany(max + 1)`, exact total, and a truncation flag.
- Disposition: accepted and fixed at `rejections.py:45-141`; regression:
  `tests/unit/test_postgres_rejections.py:80-93`.

### SQL-002 — High — Negative identifiers were accepted inconsistently

- Evidence: the pure normalizer rejected negatives, while compiler/rejection/join SQL paths could
  treat an integral negative as valid.
- Reproduction: pass `-123`, `-123.0`, and `'-123'` through all four paths and compare codes/counts.
- Risk: execution evidence could disagree with the approved transformation contract.
- Proposed regression: assert the same stable rejection at every interpreter boundary.
- Disposition: accepted and fixed. `NEGATIVE_IDENTIFIER` is defined at
  `src/schemabridge/domain/resolution.py:86`; compiler, relationship adapter, and rejection reporter
  now share the rule and focused tests cover it.

### SQL-003 — Medium — Large exact integers lack physical-type provenance

- Evidence: the compiler applies the float exactness cap while compiling a cast step, even when the
  physical value could be an exact integer/decimal.
- Reproduction: compile and execute an integral identifier above the safe IEEE-754 exactness range.
- Risk: fail-closed false rejection/undercount, not unsafe truncation.
- Proposed regression: add typed physical provenance before conditionally widening acceptance.
- Disposition: deferred as an explicit fail-closed limitation; no unsafe value is accepted.

### SQL-004 — Medium — Forged validated-query limits could bypass configuration

- Evidence: the preview adapter trusted `ValidatedQuery.max_rows` and timeout values without its own
  configured ceiling.
- Reproduction: directly construct a validated object with 501 rows or 5001 ms and call the adapter.
- Risk: a compiler/guard bug could widen a preview.
- Proposed regression: fail before opening a connection when either adapter ceiling is exceeded.
- Disposition: accepted and fixed at `src/schemabridge/adapters/postgres/preview.py:25-34`; regression:
  `tests/unit/test_postgres_preview.py:22-30`.

### SQL-005 — Medium — Guard policy is narrower than all possible PostgreSQL AST shapes

- Evidence: the guard validates the current compiler surface but does not encode every unsupported
  node or exact approved join edge as a general SQL firewall.
- Reproduction: mutate parsed ASTs outside the current compiler grammar and compare guard findings.
- Risk: relevant only if the compiler surface expands without the guard expanding first.
- Proposed regression: add an explicit allowed-node/edge model before adding compiler features.
- Disposition: retained; current compiler cannot emit those shapes and the guard/security matrix
  remains fail-closed for the milestone surface.

### SQL-006 — Medium — Misconfigured expected identity can bless an admin DSN

- Evidence: identity verification compares with configuration; deliberately configuring an admin
  username can make the equality check pass.
- Reproduction: point the adapter at an admin DSN and set the expected name to that admin.
- Risk: operator/configuration error could defeat the identity assertion.
- Proposed regression: add an independent database capability/ACL probe.
- Disposition: retained. Default/CI/live tests use `schemabridge_reader`, transaction read-only, and
  denied DML/DDL; the residual configuration risk is explicit.

### SQL-007 — Medium — Relationship evidence did not verify the observed timeout

- Evidence: it configured a timeout but did not reject a different server-observed value.
- Reproduction: return a mismatched `current_setting('statement_timeout')` from a fake cursor.
- Risk: expensive evidence queries could execute outside the promised bound.
- Proposed regression: compare normalized observed milliseconds with the requested value.
- Disposition: accepted and fixed at `relationships.py:57-76`; live integration passed.

## Conclusion

The high findings are fixed with regressions. Source operations remain read-only and parameterized;
retained medium items are fail-closed limitations or deliberate misconfiguration risks.
