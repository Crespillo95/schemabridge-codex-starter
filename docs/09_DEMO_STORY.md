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

## M21 extended proof

The three-minute narrative may remain centered on the understandable north-star request, but the
current application should first make clear that planning is no longer backed by a
Customer-specific registry:

1. On Overview, show `synthetic_enterprise` version 1, all 64 characters of registry fingerprint
   `0710148874049078f751ac96f0a18131cf01daa41f27dc034ca52a8b212dd966`, seven logical models,
   31 approved mappings, and five governed joins.
2. In Semantic Models, show both Customer/AccountHolder and
   Product/SalesOrder/SaleLine/Shipment mappings, transformations, versions, and decisions.
3. In Relationships, show the five contracts without a fixed Customer-only diagram. Point out that
   the registry can contain five contracts while one query remains limited to two joins and three
   physical tables.
4. Complete the unchanged north-star workflow and verify `2, 1, 1`, exact fanout mitigation, three
   rejected identifiers, `schemabridge_reader`, read-only mode, and the 5000 ms timeout.
5. Cite the deterministic evaluation for the Product no-join, SalesOrder/Shipment, and three-table
   commerce scenarios. The 465-row, eleven-table, eight-schema corpus has global fingerprint
   `487388495115265d5fac5a17675e05c236fd4eb4c430613ef02050a5f6010654`.

Do not demonstrate the commerce cases by entering an invented free-form request into the current
Query Studio. M21 exercises those scenarios through typed requests; registry-wide matching from a
short request or field description is M27. Likewise, the recorded registry is not complete live
DataHub reconstruction (M22) or a published/activated registry revision (M23).

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
