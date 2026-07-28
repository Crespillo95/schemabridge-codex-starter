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

## M21 registry and expanded catalog boundary

The synthetic PostgreSQL source and DataHub ingestion now cover eleven tables in eight schemas and
465 deterministic rows. The additional assets span `commerce`, `sales`, `fulfillment`, and
`support`, alongside the original `crm`, `legacy`, `bank`, and `reporting` schemas. Catalog checks
require descriptions, exact schemas, and profiles for all eleven datasets. The support table
deliberately contains familiar-looking `order_id`, `product_id`, and `customer_id` names with
case-local meanings; those names do not create semantic equivalence or an executable join.

Ingestion is not the same as complete governed-context reconstruction. M21 planning loads the
explicitly recorded `synthetic_enterprise` registry through
`GovernedSemanticRegistryPort`. Its seven models, 31 mappings, and five contracts have canonical
fingerprint
`0710148874049078f751ac96f0a18131cf01daa41f27dc034ca52a8b212dd966`; the manifest binds the registry
file with SHA-256
`4f086f8b9f0679d6405beab45d48c6601520b9b6e0c5f20d7dc0ad987485a723`.
The deterministic database manifest separately records global seed fingerprint
`487388495115265d5fac5a17675e05c236fd4eb4c430613ef02050a5f6010654`.

The recorded registry never substitutes for a requested live registry and a live catalog failure
never falls back to it. M22 reconstructs the complete atomic snapshot—logical definitions,
physical mappings, transformations, joins, decisions, risks, evidence, versions, and
provenance—from governed DataHub read-back behind the same port. M23 adds authoritative
PostgreSQL activation, migration, rollback, and projection reconciliation. The M21 bundle remains
explicitly labeled synthetic planning evidence when recorded mode is selected.

## M22 immutable registry version document

The live adapter does not assemble a registry from search results or legacy logical-parent
aspects. An explicitly prepared snapshot is published as one self-contained DataHub document whose
ID is deterministic for registry ID, version, and a SHA-256 prefix of the opaque workspace ID.
The lasting boundary and rejected alternatives are recorded in
[ADR 0008](adr/0008-live-datahub-semantic-registry.md).
For the current local-demo principal:

```text
document URN:
  urn:li:document:schemabridge-semantic-registry-synthetic_enterprise-v1-w3f92b9304d7d1f8f0cbf410c
source:
  datahub:schemabridge-semantic-registry-synthetic_enterprise-v1-w3f92b9304d7d1f8f0cbf410c
fingerprint:
  ef480eb7370924ff4c94131a2c6c4063d85652cc83aaec9059538cdc2cf9c1b9
shape:
  7 logical models / 31 mappings / 5 joins / 37 decisions / 7 related assets
```

The closed custom-property set carries the typed snapshot, registry identity and format, catalog
scope, workspace digest, canonical fingerprint, ordered decision closure, exact publication
approval, and one successful per-target audit. Related assets must equal the datasets implied by
approved mappings: `crm.customers`, `bank.account_holders`, `bank.accounts`,
`commerce.products`, `sales.orders`, `sales.order_lines`, and `fulfillment.shipments`.

Publication uses the separate SDK writer only after `registry-prepare` has exposed the exact
workspace target/fingerprint and `registry-publish` receives the same workspace, matching
fingerprint, and closed confirmation phrase. Managed actor/roles come from trusted operator
configuration rather than argv. A conflicting immutable target is rejected; a new write is read
back before success; an identical replay is `already_current`. The returned audit must match the
approval before it is appended to the selected publication-audit store: SQLite in the explicit
local profile or PostgreSQL in a managed profile.

Live read-back uses a separate HTTP client with no mutation method. It reads only the configured
URN's `documentInfo` and `status` aspects plus exact identity/privilege checks, with bounded
responses, duplicate-key rejection, no redirects, and no search. It validates the complete
snapshot, scope, version, fingerprint, approval, audit, decision closure, document state, and
related assets. Every failure is sanitized and closed; the recorded manifest is never opened.

The local reader currently inherits DataHub's stock `generatePersonalAccessTokens` platform
privilege. SchemaBridge does not call that operation and accepts the credential only when all
checked target-edit flags and exact-target mutation grants are absent. The reader and writer
credentials remain separate ignored owner-only files.

The immutable M22 document does not contain a mutable current pointer or compare-and-swap, nor can
DataHub share a transaction with either publication-audit store. Version publication remains
serialized. M23 adds a separate PostgreSQL activation transaction and reconciled projection. The
recorded M21 recipe also remains stale under the new live source and fingerprint until its
provenance is migrated. Registry-wide matching from a brief description is still M27.

## MCP mutation policy

MCP stays read-only, including after the write-back milestones. Approved mutations use separate,
bounded application ports and dedicated DataHub SDK identities; every call requires an exact typed
approval and returns per-target audit results. Neither Codex MCP nor the natural-language parser is
given broad mutation access.

Fingerprintable targets persist/read their own publication fingerprint. Structured-property and
logical-parent aspects are revalidated structurally and record a null prior fingerprint instead of
borrowing the logical-model marker. Version documents reject a pre-existing different fingerprint,
and a write is successful only after target read-back. Because GMS upsert has no compare-and-swap,
the scoped writer remains serialized.

Workflow documents additionally require exact read-back of the workflow ID, every payload
fingerprint, approval ID/actor/time, and a parsed successful per-target audit record. Matching
fingerprints without that governance evidence are not considered current, and post-write success
compares the exact audit record that was submitted. A deterministic workflow target carrying a
different valid fingerprint is an immutable conflict and is never overwritten.

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

## M23 authoritative activation and DataHub projection

M23 deliberately separates two DataHub document families:

1. immutable semantic-registry version documents remain the governed content read by planning;
2. one deterministic active-pointer document is only a projection of PostgreSQL control state.

For a scope, the projection ID is:

```text
schemabridge-semantic-registry-active-
  {registry_id}-{sha256(catalog_scope)[0:12]}-{sha256(workspace_id)[0:24]}
```

The projection has no related assets and uses a closed property set containing scope digests,
activation generation, registry version/fingerprint/target, transition ID, projection fingerprint,
the exact typed active pointer, and the exact reconciliation approval. The pointer property is
bounded to 256 KiB and the approval to 64 KiB; duplicate JSON keys, missing/extra properties,
removed documents, related assets, wrong scope, and inconsistent duplicated fields are rejected.
The raw workspace identifier is not placed in the document ID.

DataHub omits `relatedAssets` when that tuple is empty. The projection reader therefore normalizes
an absent property to the required empty set, while still rejecting explicit `null`, non-list
values, unhashable entries, duplicates, more than 256 entries, or any non-empty related-asset set.
This is transport normalization only; it does not weaken the closed projection contract.

An activation never writes this document inline. PostgreSQL first commits the authoritative
pointer, immutable transition, pending outbox, and HMAC audit event in one compare-and-swap
transaction. A DataHub failure therefore cannot undo or change which registry planning uses.
`control-plane status` exposes the pending outbox without DataHub I/O, and
`control-plane reconcile inspect` compares PostgreSQL, the exact immutable target, DataHub
projection, outbox, and audit chain without writing.

Reconciliation uses the following closed findings:

```text
in_sync
projection_missing
projection_behind
projection_ahead
projection_conflict
superseded
version_corrupt
audit_gap
pending_outbox
```

Only a freshly reconstructed report with its exact fingerprint, timestamp, actor, and
`repair-active-registry-projection` confirmation may run the repair use case. The projector checks
the exact DataHub actor and bounded `manageDocuments` privilege, refuses an observed same/newer
conflict, upserts the expected document, and requires exact read-back. Ahead, conflicting,
version-corrupt, audit-gap, or superseded states are inspection outcomes, not overwrite
authorization. An exact prior success is idempotent.

Managed planning does not read this projection. `LoadActiveGovernedSemanticRegistry` loads the
workspace-scoped PostgreSQL pointer and then retrieves its exact immutable version through the
read-only M22 version reader. Scope, version, target, source, registry fingerprint, approval
closure, and active-pointer fingerprint must all agree. The M22 v1 compatibility shim remains
read-only historical evidence and cannot be used for first managed activation.

The UI may render the pointer generation and outbox-derived projection state so an operator can
see `pending`, `delivered`, `superseded`, `blocked`, or `unknown`. It has no projection repair or
activation mutation control.

The 2026-07-23 operated sequence exercised version 6 at generation 1, version 7 at generation 2,
a durable pending projection followed by explicit exact reconciliation, and rollback to version 6
as generation 3. Generation 3 was then reconciled to `delivered`; the active projection
fingerprint read back as
`85f131dd53bb5cf2da8538cc171277c4d988b77101e0e5b37da46fed2ef24757`.
The browser independently displayed that generation/version/status and the 7/31/5 registry shape.
PostgreSQL remained the authority throughout; no planning path read the projection as a pointer.

## M25 catalog indexing from DataHub

M25 introduces a separate DataHub read path for scalable physical inventory. It is not the M22
immutable semantic-registry reader and it has no mutation method:

```text
catalog refresh claim
    → exact platform/catalog/environment dataset-URN prefix
    → scrollAcrossEntities in stable URN order
    → bounded dataset/schema metadata page
    → typed asset/field changes
    → atomic PostgreSQL page + checkpoint
    → invisible generation promotion after complete verification
```

The operated DataHub v1.6.0 endpoint returned the exact 11 synthetic datasets when filtered with
`urn START_WITH` over the PostgreSQL platform/scope prefix; the analogous dataset-name prefix
returned zero and is not used. The adapter validates response size, cursor progress, stable page
identity, scope, assets, fields, types, descriptions, tags, and terms before PostgreSQL persistence.
An asset fingerprint covers the asset's own metadata, independently of fields. Each field
fingerprint covers its complete persisted metadata, including tags and glossary terms.
PostgreSQL also includes field name, definition, native type, tags, and glossary terms in the
stored `search_document` and its GIN index. That makes DataHub context retrievable as bounded M27
candidate evidence; it does not approve equivalence or authorize a query.

DataHub discovery is labeled full reconciliation. It is never represented as delta, and a
DataHub outage never invokes the lazy synthetic source. The operated refresh stored 11 assets and
59 fields. After DataHub stopped, PostgreSQL continued to serve that completed generation and a new
refresh failed safely as `source_unavailable`. Final browser-visible stale/failure evidence remains
pending in `tasks/M25_HANDOFF.md`.

## M26 scoped query-recipe dependency inventory

M26 closes a cross-tenant identity gap in the historical query-recipe document family. A recipe
bound to an active registry now persists the canonical 64-character semantic scope fingerprint in
`RecipeRegistryBinding`. Its DataHub IDs are:

```text
schemabridge-query-recipe-
  {scope_fingerprint[0:32]}-{intent_fingerprint[0:32]}-current

schemabridge-query-recipe-
  {scope_fingerprint[0:32]}-{intent_fingerprint[0:32]}-v{version}
```

The complete fingerprint remains in the typed recipe binding; the prefix is only the deterministic
reserved namespace. Active publication refuses a recipe without that scope and `find_current`
validates both intent and exact scope. Therefore identical business intent in two workspaces cannot
share a current marker. Historical unscoped documents remain readable only through the explicit
legacy compatibility path and are not included as current dependencies for an active scope.

The semantic reconciler's DataHub reader performs a bounded stable-URN
`scrollAcrossEntities` search restricted to the exact scope-qualified prefix. It pages at most 50,
validates total/count/order/cursor progress, loads only `-current` markers, verifies their versioned
document, approval, publication audit, related assets, recipe fingerprint, and scope, then emits a
typed dependency page. A missing Document `Status` aspect is accepted only when the mandatory
document information is valid; a present Status must say the document is not removed. Every other
missing, malformed, removed, oversized, out-of-scope, permission, or outage case makes dependency
coverage incomplete.

This inventory is read-only. M26 does not publish, edit, or delete DataHub content and does not
call an LLM. Its only use of recipe documents is to prove a complete downstream blast radius and
gate stale recipe reuse.

A same-scope scope-qualified recipe from an older registry fingerprint is not discarded: its exact
logical-field and contract identities are conservatively projected to current governed
dependencies. A cross-scope recipe, a recipe without the required scope binding, or a stale recipe
whose references have no exact current correspondence makes coverage incomplete. Legacy
intent-only documents remain outside the active managed namespace and require the existing
governed republishing flow; they are never silently rewritten into another scope.
