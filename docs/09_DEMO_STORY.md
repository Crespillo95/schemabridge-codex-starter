# Demo story

## Three-minute narrative

### 0:00–0:20 — The problem

Show three physical representations:

```text
crm.customers.customer_id          VARCHAR   "00000000123"
legacy.client_master.client_no     BIGINT    123
bank.account_holders.gf_customer_id DOUBLE   123.0 / 127.5
```

State that names, formats, definitions, and joins are inconsistent, so ordinary text-to-SQL can produce a valid but wrong query.

### 0:20–0:45 — DataHub context

Show the physical assets in DataHub. Highlight schema, descriptions, profiles, lineage/query context where available, and the absence of an approved semantic model.

### 0:45–1:10 — Semantic mapping

SchemaBridge proposes `Customer.customer_key`, explains evidence, flags leading-zero and float risks, rejects unsafe values, and receives human approval.

### 1:10–1:30 — Join contract

Show `Customer → AccountHolder`, cardinality `one-to-many`, evidence, and the fanout warning. Approve the contract.

### 1:30–1:48 — Business request

Enter:

> Count customers by registration date when they are a secondary holder of an account.

Show the parsed entity, dimension, plain customer-count metric, filter, role synonym mapping, and
any assumption requiring confirmation. Contrast it with an explicit relationship count so the
automatic fanout mitigation is observable rather than merely claimed.

### 1:48–2:15 — Governed plan and SQL

Show selected physical fields, transformations, approved join, `COUNT DISTINCT` mitigation, SQL policy checks, and the generated SQL.

### 2:15–2:33 — Correct result

Show:

```text
2026-01-01 | 2
2026-01-02 | 1
2026-01-03 | 1
```

Also show the rejected `127.5`, `NaN`, and `NULL` identifiers.

### 2:33–2:50 — DataHub write-back

Publish the approved descriptions/properties, decision document, join contract, and query recipe. Show the updated DataHub page.

### 2:50–3:00 — Reuse

Start a new request/session and show that SchemaBridge retrieves the approved model and skips rediscovery.

## Visual priorities

- Avoid long terminal sequences.
- Keep text and SQL large enough to read.
- Use one consistent demo dataset and no hidden manual edits.
- Show evidence and safety, not just a final chart.
- Record a clean fallback video after the live deployment is stable.

## Demo failure fallback

Prepare:

- a local fully seeded environment;
- a prerecorded video;
- screenshots of before/after DataHub;
- checked-in examples of mappings, query plan, SQL, validation report, and write-back;
- a deterministic fake LLM option so the demo does not depend on API latency.
