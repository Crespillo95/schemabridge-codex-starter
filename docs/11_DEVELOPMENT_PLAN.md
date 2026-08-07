# Development plan overview

The critical path is 63 estimated hours across M00–M18. Estimates are planning limits, not permission to skip quality checks.

| ID | Milestone | Hours | Primary output |
|---|---|---:|---|
| M00 | Repository baseline | 2 | reproducible quality-gated skeleton |
| M01 | Synthetic demo database | 3 | reproducible heterogeneous source data |
| M02 | Domain and normalization | 4 | typed semantic and transformation core |
| M03 | Query IR, compiler, and SQL guard | 4 | deterministic safe SQL slice |
| M04 | Local DataHub | 4 | pinned Core/MCP environment and ingestion |
| M05 | DataHub read adapter | 3 | catalog context behind application ports |
| M06 | Semantic candidate engine | 4 | explainable ranked field mappings |
| M07 | Canonical review and write-back | 3 | approved logical model context in DataHub |
| M08 | Join discovery and contracts | 4 | approved cardinality-aware relationships |
| M09 | Guided query builder | 2 | typed request without an LLM |
| M10 | Governed planning and execution | 4 | north-star guided end-to-end query |
| M11 | Natural-language intent | 3 | typed request parser with ambiguity handling |
| M12 | Agent orchestration | 2 | coordinated read/act/write workflow |
| M13 | Context write-back and reuse | 2 | persistent query recipes and second-run reuse |
| M14 | Streamlit UI | 4 | judge-ready workflow UI |
| M15 | Evaluation harness | 3 | reproducible metrics and reports |
| M16 | End-to-end hardening | 4 | Ultra multi-agent audit and fixes |
| M17 | Judge deployment | 3 | stable free-access test environment |
| M18 | Submission package | 5 | README, examples, video, Devpost content |
| M19 | Optional OSS contribution | 3 | bonus contribution, outside critical path |

## Dependency graph

```text
M00 → M01 → M02 → M03
               ↘
M04 → M05 → M06 → M07 → M08
M02 + M08 → M09 → M10 → M11 → M12 → M13 → M14 → M15 → M16 → M17 → M18
```

M03 can begin before DataHub is ready because it uses domain plans and fakes. M06 needs M02 and M05. M09 needs the request domain and approved context interfaces. M11 never bypasses M09/M10; it translates natural language into the same request model.

## Integration rule

Every milestone must leave a tested port or use case that the next module consumes. Temporary fakes are first-class and remain for unit tests. No module communicates through ad hoc dictionaries once a typed contract exists.

## Cut strategy

If schedule pressure arises, cut in this order:

1. M19 optional contribution;
2. non-north-star semantic concepts;
3. charts and visual polish;
4. three-table query example;
5. direct native logical-field linking fallback to documented DataHub artifacts;
6. natural-language follow-up conversation.

Never cut:

- DataHub read/write/reuse proof;
- deterministic SQL compilation;
- SQL/database safety;
- fanout handling;
- north-star end-to-end demo;
- tests and judge-readable examples.

## Productionization continuation

The operator extended the goal beyond the original 63-hour hackathon path. The authoritative
M20–M31 sequence and gates are maintained in `plans/MASTER_PLAN.md`. M20 establishes production
browser identity/RBAC and workflow isolation. M21 establishes the atomic scoped semantic registry
and a deterministic 11-table/eight-schema corpus while preserving the per-query 3-table/2-join
limit. M22 reconstructs that complete registry from live DataHub without recorded fallback; M23
adds the durable PostgreSQL authority; and M24 adds the authenticated API/durable worker. Those
milestones are accepted locally.

M25 dynamic tenant catalog indexing and its operated local 10/5,434-asset scale profile are
implemented and accepted locally after the final full-gate and internal-browser evidence passed.
The later sequence covers drift/change management, registry-wide guided and natural-language Query Studio
(including matching from a short field description), connector/cost controls, operational
hardening, production evaluation/security verification, and an operated pilot. Locally completed
milestones remain unreleased until the operator reviews the dirty tree and records the required
clean-commit evidence.
