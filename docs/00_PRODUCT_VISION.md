# Product vision

## Product statement

SchemaBridge is a DataHub-native governed semantic query agent. It discovers how inconsistent
physical fields represent the same business concepts, proposes reusable logical models and join
contracts, and uses that approved context to compile business questions into explainable,
validated SQL. Its output-first query experience produces standalone PostgreSQL that an analyst
can inspect, copy, and run in another PostgreSQL client connected to the governed database context;
execution inside SchemaBridge is optional. Other SQL engines require their own compiler and guard.

## Problem

Large data estates routinely contain:

- synonyms such as `contract_id`, `gf_contract_id`, and `agreement_no`;
- homonyms where the same column name means different things;
- identifiers represented as padded strings, integers, decimals, or unsafe floats;
- incomplete definitions and undocumented join rules;
- one-to-many joins that silently duplicate metrics;
- repeated analyst work to rediscover the same extraction logic.

A catalog can expose metadata, but the remaining semantic reconciliation is often tribal knowledge. Generic text-to-SQL amplifies the risk because syntactically valid SQL can still be semantically wrong.

## Product outcome

After an approved workflow, DataHub and SchemaBridge should know:

```text
Customer.customer_key is represented by these physical fields.
These normalization rules are approved.
Customer joins AccountHolder through this contract.
The relationship is one-to-many.
Customer metrics require a fanout mitigation.
This validated typed request answers this business question.
This deterministic, guarded PostgreSQL artifact can be copied without hidden driver parameters.
```

## North-star journey

1. Scan DataHub context for the demo assets.
2. Propose `Customer`, `AccountHolder`, and `Account` logical models.
3. Explain and approve physical-to-logical field mappings.
4. Propose and approve `Customer → AccountHolder` and `AccountHolder → Account` join contracts.
5. Accept a simple or advanced guided/natural-language analytical request.
6. Search the complete current governed registry and build a bounded approved context closure.
7. Show the interpreted dimensions, metrics, filters, grouping, windows, selected datasets, join
   path, assumptions, confidence, risks, and unsupported requirements.
8. Require exact confirmation, then compile and independently validate deterministic PostgreSQL.
9. Render and independently validate a standalone copy/download artifact with
   `executed=false`.
10. Optionally execute the separate bounded read-only preview and display results/rejected source
    values.
11. Save approved definitions, decisions, and SQL-free query recipes back to DataHub.
12. Reuse that context in a later request.

## Users

### Primary: analyst or analytics engineer

Needs a reliable extraction without knowing every physical schema convention.

### Secondary: data steward

Approves semantic concepts, normalization policies, relationships, and metadata changes.

### Tertiary: platform engineer

Operates DataHub, evaluates safety, and integrates additional sources later.

## Positioning

SchemaBridge is not a replacement for DataHub, a generic chat interface, or an unrestricted SQL
generator. It extends DataHub by improving the context graph before asking it to support
analytics.

```text
DataHub provides metadata context and governance primitives.
SchemaBridge proposes and operationalizes semantic decisions.
The governed context makes subsequent agent-generated queries safer.
```

The product promise is bounded exactness, not infallibility. SchemaBridge generates SQL only when
the complete requested meaning fits its typed language and current approved context. Ambiguity,
unapproved physical fields, unsupported SQL families, or stale semantic evidence remain visible
and produce no query rather than a plausible approximation.

## Success definition

The hackathon release succeeds when the complete north-star request works end-to-end on synthetic data, all safety invariants hold, approved context is written to DataHub and then reused, and a judge can understand and test the result without private infrastructure.
