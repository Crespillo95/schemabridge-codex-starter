# Hackathon evaluation alignment

The implementation plan treats the judging criteria as architectural requirements rather than submission copy added at the end.

## 1. Use of DataHub

### Product evidence

- Live DataHub context drives asset discovery, schema inspection, definitions, profile signals, and
  governance state. Missing lineage and query history remain typed missing evidence; they are not
  fabricated. The demo's complete mapping transformations are an explicitly recorded synthetic
  planning context until DataHub read-back exposes that complete shape.
- SchemaBridge proposes logical models and physical mappings rather than maintaining a disconnected catalog.
- Approved terms, descriptions, structured properties, decision documents, join contracts, and query recipes are written back.
- A later request retrieves and reuses approved DataHub documents when their versions remain
  compatible; it still replans and revalidates SQL before preview.

### Demo proof

Show the same DataHub assets before and after SchemaBridge and then restart/reload SchemaBridge to demonstrate context reuse.

### Failure condition

If the demo still works after replacing DataHub with a hard-coded list of columns, the integration is not meaningful enough.

## 2. Technical execution

### Product evidence

- ports-and-adapters separation and typed boundaries;
- deterministic transformations and SQL compiler;
- AST and database-layer safety;
- unit, integration, acceptance, and security regression tests;
- one-command local demo where practical;
- explicit error and rejection handling;
- end-to-end north-star result validated against ground truth.

### Demo proof

Show input → interpretation → mappings → join path → fanout mitigation → SQL → preview → write-back in one uninterrupted flow.

## 3. Originality

### Positioning

DataHub already supports plain-English analytics. SchemaBridge does not rebuild that. It creates and governs the semantic context needed when physical fields and joins are inconsistent.

```text
Existing capability: catalog-grounded question → SQL/results.
SchemaBridge: schema reconciliation → logical models → join contracts → governed plan → SQL/results → improved catalog.
```

### Demo proof

Start with incompatible identifiers and an undocumented one-to-many relationship. Make the semantic repair and persistent context the center of the story, not the chat box.

## 4. Real-world usefulness

### Problem evidence

- analysts repeatedly rediscover equivalent fields;
- incompatible types break joins;
- undocumented relationships produce missing or duplicated rows;
- tribal knowledge is not reusable by people or agents;
- plain text-to-SQL cannot compensate for weak context reliably.

### Measurable evidence

Report actual:

- semantic candidate metrics;
- join-path and cardinality accuracy;
- result correctness;
- unsafe-conversion detection;
- security rejection rate;
- first-run versus reuse workflow steps/time.

## 5. Submission quality

### Required assets

- public Apache-2.0 repository;
- complete setup and testing instructions;
- a free judge-accessible demo;
- public video under three minutes;
- English submission materials;
- screenshots with readable evidence;
- sample generated artifacts in `examples/`;
- limitations and reproducible evaluation results.

### Judge-first rule

Assume a judge will not install the project. The README, screenshots, description, video, and examples must independently explain the product and prove the output quality.

## Optional open-source bonus

M19 may contribute a focused DataHub Skill, documentation improvement, bug fix, or RFC around governed semantic join analysis. It is not allowed to jeopardize M00–M18.

## Scorecard maintained during development

| Criterion | Target proof | Owning milestones |
|---|---|---|
| Use of DataHub | meaningful read + write + reuse | M04–M08, M13 |
| Technical Execution | safe end-to-end flow | M02–M16 |
| Originality | governed semantic layer | all product docs, M06–M13 |
| Real-World Usefulness | north-star problem and metrics | M01, M08, M15 |
| Submission Quality | demo, examples, README, video | M14, M17, M18 |

## M32 copy-first SQL evaluation alignment

M32 evaluates the claim the user actually needs: whether a natural-language request becomes the
exact supported SQL meaning against current governed context, not whether generated text merely
looks sophisticated.

The evaluation separates:

- **retrieval coverage**: the required approved concepts are found when searching the complete
  registry, while the supplied closure remains within 3 models/12 fields/2 joins;
- **interpretation exactness**: every requested field, filter, metric, grouping, aggregate
  threshold, window, order/tie rule, and limit equals reviewed typed ground truth;
- **route correctness**: v1/v2 equals exact representability, including a long-simple and
  short-advanced counterexample to length/keyword routing;
- **SQL structural correctness**: the deterministic compiler and independent guard produce the
  reviewed direct/staged topology and no forbidden construct;
- **copy integrity**: the standalone artifact preserves typed values, contains zero placeholders,
  reparses, passes a second guard, has a stable SHA-256, and reports `executed=false`;
- **result correctness**: a separately selected synthetic read-only PostgreSQL run returns the
  exact type-aware ground truth;
- **unsupported honesty**: unsupported families receive a typed no-SQL result and are never
  approximated.

The capability matrix uses
[25 Ejemplos de Consultas SQL Avanzadas](https://learnsql.es/blog/25-ejemplos-de-consultas-sql-avanzadas/)
as a public benchmark taxonomy. Supported families are bounded rankings/top-N/`NTILE`, partition
averages/percentages, duplicates/`HAVING`, running/moving calculations, `LAG`/`LEAD`,
delta/percentage change, conditional metrics, and numeric buckets. Cross/self joins, arbitrary
subqueries, set operations, recursion, and gaps/islands are explicit negative cases. `ROLLUP`
remains negative until `GROUPING()` flags make subtotal and genuine `NULL` distinguishable.

Report raw numerators/denominators and each failed/unsupported case. Do not combine deterministic
fake results, optional live-model observations, compiler/guard results, and database result
correctness into one accuracy percentage. The current synthetic corpus cannot justify confidence
intervals, production-quality claims, “expert in every SQL dialect,” or an error-free guarantee.

For judge-facing proof, show the simple route and the advanced Spanish reference side by side:
input → bounded governed context → exact typed preview → confirmation → copyable PostgreSQL. Keep
the optional result execution visibly separate so a zero-execution primary flow cannot be
mistaken for an executed validation.
