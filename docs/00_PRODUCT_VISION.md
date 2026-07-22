# Product vision

## Product statement

SchemaBridge is a DataHub-native governed semantic query agent. It discovers how inconsistent physical fields represent the same business concepts, proposes reusable logical models and join contracts, and uses that approved context to compile business questions into explainable, validated SQL.

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
This validated query recipe answers this business question.
```

## North-star journey

1. Scan DataHub context for the demo assets.
2. Propose `Customer`, `AccountHolder`, and `Account` logical models.
3. Explain and approve physical-to-logical field mappings.
4. Propose and approve `Customer → AccountHolder` and `AccountHolder → Account` join contracts.
5. Accept a guided or natural-language analytical request.
6. Show the interpreted dimensions, metrics, filters, selected datasets, join path, assumptions, confidence, and risks.
7. Compile deterministic PostgreSQL.
8. Validate the AST and execute a bounded read-only preview.
9. Display results and rejected source values.
10. Save approved definitions, decisions, and query recipes back to DataHub.
11. Reuse that context in a later request.

## Users

### Primary: analyst or analytics engineer

Needs a reliable extraction without knowing every physical schema convention.

### Secondary: data steward

Approves semantic concepts, normalization policies, relationships, and metadata changes.

### Tertiary: platform engineer

Operates DataHub, evaluates safety, and integrates additional sources later.

## Positioning

SchemaBridge is not a replacement for DataHub and not a generic chat interface. It extends DataHub by improving the context graph before asking it to support analytics.

```text
DataHub provides metadata context and governance primitives.
SchemaBridge proposes and operationalizes semantic decisions.
The governed context makes subsequent agent-generated queries safer.
```

## Success definition

The hackathon release succeeds when the complete north-star request works end-to-end on synthetic data, all safety invariants hold, approved context is written to DataHub and then reused, and a judge can understand and test the result without private infrastructure.
