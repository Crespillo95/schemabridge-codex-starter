# DataHub integration

## Why DataHub is foundational

SchemaBridge uses DataHub as both:

1. the context graph used to understand physical assets; and
2. the governed knowledge layer where approved semantic decisions persist.

The application must not remain useful if DataHub is replaced by a static list of table names; that would indicate superficial integration.

## Read path

The DataHub adapter should expose application-level capabilities backed by MCP or SDK operations:

| Need | DataHub capability |
|---|---|
| Find candidate assets | search |
| Inspect datasets | entity metadata |
| Inspect large schemas | schema-field listing |
| Understand connections | table and column lineage |
| Explain paths | exact lineage paths |
| Learn established joins | dataset query history |
| Find query context | SQL-context search |
| Reuse decisions | document and glossary search |
| Read definitions | descriptions, terms, structured properties |
| Read quality evidence | profiling and quality signals |

The application should record which signals were available and avoid fabricating missing evidence.

## Write path

After explicit human approval, SchemaBridge should write an appropriate subset of:

- logical models and physical links;
- glossary terms and versions;
- field descriptions;
- tags;
- structured properties such as canonical name, mapping status, confidence, and version;
- decision documents containing rationale, risks, transformations, and approver;
- validated query recipes.

Prefer proposal workflows where the environment supports them. Direct mutations remain an adapter capability guarded by an explicit approval value.

## Logical models

Logical models represent an entity independently of a physical implementation. SchemaBridge should propose them and link physical datasets/fields only after review. The local DataHub GMS must enable logical models, and required privileges must be documented.

Because support can differ across DataHub versions, the adapter must:

- hide SDK/OpenAPI details behind a port;
- detect unsupported operations;
- provide a document/structured-property fallback for the demo without claiming a native link exists;
- record the exact tested API and DataHub version.

## MCP mutation policy

MCP stays read-only, including after the write-back milestones. Approved mutations use separate,
bounded application ports and dedicated DataHub SDK identities; every call requires an exact typed
approval and returns per-target audit results. Neither Codex MCP nor the natural-language parser is
given broad mutation access.

### Verified M04 local boundary

The tested local environment uses DataHub Core `v1.6.0`, CLI/connector `1.6.0.15`, and MCP server
`0.6.0`. Authentication and logical-model UI support are enabled, while MCP mutation, document-save,
and document-search tools are forced off by the repository wrapper. The dedicated MCP service
account has a one-month local token and was verified only for catalog search and schema-field reads.

M04 itself added no write adapter. M05–M13 subsequently added catalog reads, approval-gated
canonical/join/workflow/recipe writers, and read-back. The native UI flag remains enabled; every
current mutation carries typed approval and immutable decision/audit context while MCP remains
read-only.

## Durable artifacts

Suggested DataHub artifacts:

```text
Logical model: Customer
Glossary term: Customer Identifier
Structured properties:
  schemabridge.canonical_name
  schemabridge.canonical_type
  schemabridge.mapping_status
  schemabridge.mapping_confidence
  schemabridge.mapping_version
Document: Join Contract — Customer to AccountHolder
Document: Query Recipe — Secondary holders by registration date
```

## Acceptance proof

The demo must visibly compare DataHub before and after:

```text
Before: physical fields, inconsistent names, incomplete semantic context.
After: approved logical concepts, documented mappings, join contract, and reusable recipe.
```

A fresh SchemaBridge session must retrieve the approved context and avoid repeating the initial proposal workflow.
