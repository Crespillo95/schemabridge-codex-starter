# M32 deterministic natural-language to copyable PostgreSQL evaluation

- Status: **PASS_LOCAL_DETERMINISTIC**
- Date: 2026-07-30
- Registry fingerprint:
  `0710148874049078f751ac96f0a18131cf01daa41f27dc034ca52a8b212dd966`
- Registry scope: synthetic governed context with 7 logical models, 31 approved fields, and
  5 approved logical join summaries.
- Language mode: exact deterministic fake fixtures; no provider call, token use, or live-model
  accuracy claim.
- Primary output mode: copy-only; no source executor call.

This report measures the bounded M32 claim: exact reviewed typed meaning becomes deterministic,
standalone, twice-guarded PostgreSQL after explicit confirmation. It does not measure arbitrary
natural-language generalization, every SQL construct, another SQL dialect, or portability to an
unrelated database.

## Raw results

| Evaluated contract | Result | Evidence |
|---|---:|---|
| M32 targeted unit/acceptance/read-only-integration checks | `304/304` | 22 M32-relevant test modules |
| Spanish governed retrieval cases | `14/14` | simple and advanced lexical retrieval corpus |
| Simple/route-control Spanish end-to-end cases | `4/4` | count by country, balance sum, long-simple v1, short-ranking v2 |
| Window-operation compiler plus independent guard cases | `14/14` | every closed `WindowOperation` value |
| Conditional aggregate compiler plus guard cases | `7/7` | every closed `AdvancedMetricOperation` value |
| Numeric bucket compiler plus guard case | `1/1` | bounded typed bucket plan |
| Reference advanced typed/copy acceptance | `1/1` | 3 datasets, 2 joins, no SQL before confirmation |
| Primary reference executor calls | `0` | artifact reports `executed=false` |
| Optional read-only PostgreSQL reference integration | `1/1` | exact 5 reviewed synthetic rows |
| Automated Streamlit copy-first acceptance | `3/3` | no-SQL preview, simple copy path, typed ambiguity |

The reference standalone SQL SHA-256 is
`ec589a1527d0d641f4f7f7eb7e052ca1ace5b092013b437da72542ce4145d4f3`.
The assertion binds the exact final normalized SQL; the SQL and its literal values are not copied
into this durable report.

## Capability interpretation

The positive compiler matrix covers the bounded families used as the public comparison taxonomy:
ranking/top-N/`NTILE`, partition averages/percentages, duplicate/group thresholds, running and
moving calculations, `LAG`/`LEAD`, delta/percentage change, conditional metrics, and numeric
buckets. The independent negative suite rejects self/Cross joins, set operations, arbitrary
subqueries, recursive or forged CTEs, unsupported grouping/window shapes, statement smuggling,
unknown assets/columns, and literal/placeholder tampering.

The comparison source is
[25 Ejemplos de Consultas SQL Avanzadas](https://learnsql.es/blog/25-ejemplos-de-consultas-sql-avanzadas/).
It is a taxonomy reference only. M32 does not cover all 25 examples literally: cross/self joins,
set operations, recursion, gaps-and-islands, arbitrary subqueries, and `ROLLUP` remain deliberate
no-SQL outcomes.

## Separated evidence and limitations

- The four Spanish end-to-end cases and the advanced reference are exact deterministic fixtures,
  not a live-provider or holdout-quality campaign.
- The optional PostgreSQL integration executed only the separately prepared parameterized guarded
  form through the synthetic read-only role. The standalone copy artifact was never executed.
- Browser automation was unavailable because the in-app browser list was empty. Streamlit health
  and automated acceptance passed, but viewport, focus, overflow, console, and real download
  interaction remain operator manual-test items.
- PostgreSQL is the only output dialect. The artifact targets the same governed PostgreSQL
  database/context displayed in its preview.
- One request remains bounded to 3 physical tables, 2 approved joins, 4 derived window outputs,
  and 8 window AST nodes.
- M32 retrieves only approved logical context. Physical catalog-only candidates remain in the
  separate M27 `needs_mapping_review` lane and cannot become SQL without semantic approval.
