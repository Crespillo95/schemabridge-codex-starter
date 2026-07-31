# Governed query pipeline

## Principle

Natural language never jumps directly to executable SQL.

```text
user input
  → AnalyticalRequest
  → ambiguity check
  → semantic resolution
  → ResolvedQueryPlan
  → deterministic SQL AST/compiler
  → SQL policy validation
  → read-only preview
  → result validation
  → optional approved context publication
```

## Input modes

### Guided mode

The user selects approved logical fields, operators, aggregates, and grains. This mode is
implemented first and is the reference behavior.

M09 implements the logical boundary through `request-demo`. Its available model/field choices come
from an explicitly labeled approved-context port, while operations and grains come from closed
enums. Raw SQL, physical assets, arbitrary expressions, and natural-language parsing are not input
fields. Validation completes before the request can cross the planner port:

```text
guided primitive selections
  → closed-enum parsing
  → immutable AnalyticalRequest
  → approved field/type/role validation
  → unique shortest approved logical path or typed rejection
  → ValidatedAnalyticalRequest
  → governed physical resolution (M10)
  → restricted QueryPlan
  → deterministic compile and independent SQL guard
  → optional bounded read-only preview and rejected-source report
```

`request-demo` retains the M09 fake acknowledgement for a logical-only demonstration.
`governed-demo` invokes M10's separate planning/execution use cases. The recorded planning context
is visible in output and is never a hidden fallback for a failed live catalog. In current M21
composition, both logical validation and physical resolution obtain their context from the same
scoped, atomic governed semantic registry; the pre-M21 split fixtures remain compatibility/test
evidence only.

### Natural-language mode

M11 converts a question into the same typed `AnalyticalRequest`. The parser port receives only the
current language and a bounded logical vocabulary; it never receives credentials, raw samples,
physical assets, write tools, or an SQL/execution capability. The key-free deterministic adapter is
the default for CI and demos. The explicitly selected live adapter uses provider structured output,
stores no response, and exposes no tools.

Provider output is reparsed through the immutable domain schema and checked again against the exact
vocabulary and current approved logical context. Hallucinated fields/models, unsupported enums,
unknown role values, SQL-like filter values, and language mismatches fail before confirmation. The
entrypoint previews interpretations and alternatives; it invokes semantic planning only after a
fingerprinted `IntentConfirmation`, and it does not compile or execute SQL in the M11 command.

M21 does not turn that bounded parser into a registry-wide language agent. It still understands
only the approved Customer/AccountHolder vocabulary required by the north-star journey. The
Product, SalesOrder, SaleLine, and Shipment evaluation scenarios begin from typed requests.
Selecting fields across the active registry from a short free-form request or field description is
M27 scope. A definition can contribute evidence to a later matcher, but it cannot approve a
mapping or join by itself.

## Ambiguity handling

The north-star phrase “group all customers” can mean a distinct customer count, relationship count, or row listing. The parser should produce:

```yaml
interpretation: count_distinct_customers_by_registration_date
requires_confirmation: true
alternatives:
  - count_holder_relationships
  - list_individual_customers
```

Execution requires a confirmed interpretation.

For guided M09 input, ambiguity is structural rather than linguistic: zero approved paths yields
`missing_approved_join_path`, and multiple equally short paths yield
`ambiguous_approved_join_path`. Both fail before the planner port is called. Natural-language
interpretation adds closed codes for count-versus-list, customer-distinct-versus-holder-relationship
count, date meaning, unknown role values, and unresolved instruction-like text. The latter is
treated only as untrusted business text and cannot change tools, approval, or safety policy.

## Semantic resolution

For every referenced logical field, the planner finds:

- an approved physical mapping;
- a supported transformation plan;
- a dataset that satisfies the request;
- approved join contracts connecting required models;
- cardinality and fanout implications;
- any version mismatch or stale validation.

The planner should choose the shortest approved path that satisfies all concepts, then explain its choice. It must not invent joins from an LLM response.

M21 supplies that context as one `ScopedSemanticRegistrySnapshot`, not as independently loaded
logical, mapping, and join files. Registry construction verifies approvals, versions, decision
references, provenance, logical/physical join agreement, and exact join-key transformations before
planning. The verified `synthetic_enterprise` snapshot has seven logical models, 31 mappings, and
five join contracts, but an individual request remains limited to three physical tables and two
joins.

Before an approved workflow executes or retries a preview, the application reloads the current
registry and resolves the validated request again. A changed/revoked mapping, join, registry
fingerprint, or scope produces a stable stale failure before SQL compilation or PostgreSQL I/O and
requires a new confirmation. Rejected-source checks are constructed only from the exact approved
join keys in that same snapshot.

## Query IR

The SQL compiler consumes a restricted IR, not strings. Expected nodes include:

- dataset scan;
- transformed expression;
- projection;
- typed predicate;
- approved join;
- aggregation;
- date grain;
- group by;
- order by;
- limit.

No raw expression node exists in the MVP.

### M03 implemented boundary

The M03 compiler accepts only the typed `QueryPlan`. It builds a SQLGlot expression tree and emits
PostgreSQL with psycopg `%s` placeholders; filter values and approved mapping values are carried in a
separate parameter tuple. Requested limits are capped by the preview policy, and a missing request
limit becomes the policy limit.

The final rendered SQL is then reparsed by a separate SQLGlot guard. Only a guarded
`ValidatedQuery` reaches the PostgreSQL preview port. The guard checks statement count and root
type, nested destructive nodes, assets, columns, functions, joins, table count, placeholders, and
the literal preview limit independently of compiler metadata. The psycopg adapter additionally sets
the transaction read-only flag, applies a transaction-local statement timeout, caps fetched rows,
and reports the observed database identity and safety settings.

The compiler now implements the closed transformation forms used by both domains, including
identifier validation/normalization, timestamp-to-date conversion, fixed-format date parsing,
decimal-scale normalization, and mapped categorical values. Unsupported operations still fail
with `unsupported_transformation`; no fallback expression or SQL path exists.

## Fanout

If a left entity metric crosses a one-to-many join, the planner must:

1. detect the risk;
2. choose an approved mitigation, commonly `COUNT DISTINCT` or pre-aggregation;
3. expose the mitigation to the user;
4. fail when no valid mitigation is defined.

M09 identifies the approved logical fanout path. M10 repeats the control using the complete
versioned contract: an explicit Customer `count_distinct` remains unchanged, while a Customer
`count` of the exact approved one-side key is changed to the contract-defined `count_distinct`
mitigation and records the requested operation, applied operation, contract, and reason. A
`count` of an attribute fails closed because distinct substitution would change its meaning. A relationship count on
`AccountHolder.customer_key` remains a relationship count. Other unsupported left-entity
aggregates fail closed.

The same policy is generic in M21. A SalesOrder count traversing the approved one-to-many shipment
contract becomes `COUNT DISTINCT` only for the exact approved SalesOrder key. The three-table
commerce revenue query traverses two approved many-to-one paths and does not gain an unrelated
distinct mitigation. Unsafe downstream aggregates, many-to-many execution, or a forged
cross-domain path fail before execution.

## Validation layers

1. Pydantic/domain validation of intent and plan.
2. Compiler allowlist of supported operations.
3. SQL parser validation of one statement and root statement type.
4. Asset and column allowlists.
5. Join predicate and Cartesian-product checks.
6. Table-count and result-limit checks.
7. Database read-only role, read-only transaction, and timeout.
8. Result-shape and rejection validation.

## North-star compiled behavior

The M10 planner normalizes padded CRM IDs and finite integral account-holder floats, rejects unsafe
float ranges, maps role synonyms to `SECONDARY`, joins through the approved one-to-many contract,
and counts distinct customer keys by registration date.

## M21 multi-domain proof

The deterministic corpus contains eleven tables across eight schemas and 465 rows. Five tracked
requests cover the unchanged north-star case, a Customer no-join control, a Product no-join
control, a SalesOrder-to-Shipment fanout case, and a Product/SaleLine/SalesOrder three-table
commerce case. Their planning context comes from registry fingerprint
`0710148874049078f751ac96f0a18131cf01daa41f27dc034ca52a8b212dd966`; the complete seed fingerprint
is `487388495115265d5fac5a17675e05c236fd4eb4c430613ef02050a5f6010654`.

These scenarios demonstrate deterministic typed planning and execution over more varied schemas.
They do not demonstrate production scale, live DataHub registry reconstruction, registry
activation, or free-description matching. Those boundaries remain M22, M23, and M27 respectively.

## M24 asynchronous governed-preview boundary

M24 moves only an already reviewed execution approval onto a durable worker. It does not accept a
business description, field name, physical asset, query plan, SQL, or arbitrary task through HTTP:

```text
existing workflow at execution_approval
  + exact revision
  + exact validated-plan fingerprint
  + exact confirmation
  + verified principal and idempotency identity
    → queued execute_workflow_preview authorization
    → fenced worker claim
    → reload owner grant, workflow, and current active registry
    → resolve the stored typed request again
    → deterministic compile and independent final-SQL guard
    → bounded source read
    → durable summary without rows or SQL
```

The submitter cannot replace the reviewed plan or choose an operation. The API's only mutating
command reserves `execute_workflow_preview`; actor, workspace, workflow owner, authorization
window, and job identity are derived or verified server-side. Exact replay is inert. A changed
payload under the same idempotency key fails before another job or workflow action is created.

The worker checks cancellation and authorization immediately after claim and again before
protected source I/O; cancellation is also checked before preview, before rejection inspection,
and before each governed source statement. A separate supervisor renews the current fenced lease
periodically while the synchronous workflow execution is running and performs a final heartbeat
before a terminal transition; losing ownership fails closed. Final audit found that schema v2 did
not independently reject every success after authorization expiry. Schema v3 now applies
`0003_reject_expired_job_success.sql`; the fresh M24 unit, PostgreSQL, process, socket, and browser
regressions accepted that corrected terminal path locally. The workflow orchestrator
reloads the PostgreSQL-selected active semantic registry, re-resolves the request,
deterministically compiles, independently guards, and executes through the existing read-only
preview adapter. Neither the API nor the job contains model-produced SQL; M24 performs no OpenAI
request.

Because PostgreSQL queue state and an external read cannot share one commit, delivery is at least
once. A completed exact workflow is summarized without repeating the preview. A trace that shows a
started but not conclusively completed preview or rejection read is moved to the explicit
human-retry workflow boundary and dead-lettered; it is not automatically replayed. Retriable
registry/source outages and timeouts use finite deterministic backoff.
Workflow-access store failure is preserved as unexpected external state and dead-lettered rather
than relabeled as a valid authorization mismatch. For rotated identities, the worker validates the
persisted workspace+submitter pair through verified lineage for owner and workspace-wide grants
before any workflow/source I/O.

The asynchronous response intentionally omits preview rows and source values. It exposes only job
status/attempts, expected revision/plan fingerprint, timestamps, a closed failure code, and—on
success—the workflow revision/stage, row count, preview fingerprint, exact rejected-row total,
bounded grouped rejection counts, an exact unclassified residual, completeness/truncation flags,
and completion time. Interactive rows remain available only through the separately authenticated,
session-bound Streamlit surface.

Catalog inventory capacity is not part of this job payload or the three-table query plan. M25 now
loads each tenant's connection/table inventory dynamically and page/indexes operated local 10 and
5,434-table fixtures without a hardcoded total or full-memory page. M27 then matches short
descriptions against that tenant's governed catalog. Both retain the independent maximum of three
physical tables in one executable query.

## M25 inventory-to-query boundary

M25 adds a scalable metadata index, not a new SQL input path:

```text
DataHub/synthetic metadata page
    → typed catalog source changes
    → invisible PostgreSQL generation
    → atomic active-generation promotion
    → tenant-scoped keyset pages
    → later bounded candidate retrieval

approved semantic registry + typed AnalyticalRequest
    → unchanged governed resolver
    → one connection, <= 3 tables, <= 2 joins
    → deterministic compiler + independent AST guard
    → bounded read-only execution
```

The two flows deliberately meet only through future governed selection. A catalog asset or field
description is not an approved logical mapping, join contract, or executable identifier. M25 may
store and page names, native types, definitions, glossary terms, tags, key/nullability flags, and
metadata fingerprints. Its field `search_document` indexes names, definitions, native types,
tags, and glossary terms for bounded retrieval. It does not infer semantic equivalence from those
values and makes no OpenAI request. M27 may search a bounded candidate subset using a short
description, then expose evidence and ambiguity for confirmation.

Interactive reads always target PostgreSQL, not a live DataHub scan. Connections are tenant
scoped; assets and fields are additionally bound to one exact connection and active or explicitly
retained generation. The API decodes an HMAC-authenticated keyset cursor, reads at most 51 rows,
returns at most 50 items, and emits another opaque cursor only when more data exists. A stale,
tampered, cross-scope, cross-filter, or unavailable-generation cursor fails before a protected
result is returned.

Refresh execution is outside the query pipeline and outside the API request:

```text
refresh request
    → catalog-indexer claim
    → begin invisible staging
    → read exactly one bounded source page
    → commit typed changes + checkpoint
    → heartbeat and request the next page
    → verify completion/quota/fingerprint/base generation
    → promote pointer atomically
```

Until promotion, every query/catalog reader sees the previous complete generation. A restart
resumes from the durable source checkpoint. If the final page committed before the crash, the
indexer completes it without rereading the source. DataHub uses a stable-URN full scroll; only a
source with a real change feed may use typed delta upserts/deletes. No Python-side full generation
clone is allowed.

The reference 10- and 5,434-table tenants prove inventory variability only. Even after the large
catalog is indexed, the semantic resolver must select approved mappings/contracts from one
governed connection. Cross-connection plans, four tables, three joins, Cartesian joins, unsafe
fanout, unknown assets, DDL/DML, excess rows, or timeout bypass still fail in their existing
layers. Inventory cardinality cannot change `SCHEMABRIDGE_MAX_QUERY_TABLES=3`.

The operated local M25 report now proves exact 10/5,434 PostgreSQL inventory traversal at page
sizes 1, 17, and 50, at-most-51-row reads, 75 small-tenant and 41,028 large-tenant fields, bounded
memory, and one 29.685331-second large full refresh. Sparse-wide assets exercise 64-field nested,
Unicode, and heterogeneous-type metadata. Asset and field probes use their expected indexes under
the default PostgreSQL planner without an override. Focused unchanged-query-policy cuts reject
fourth-table, third-join, cross-connection, Cartesian, DDL/DML, fanout, limit, and timeout bypasses.

The catalog-to-query selector was intentionally absent in M25. M27 subsequently added bounded
semantic retrieval and confirmation, and M28 now binds one explicit governed connection before
connector work while continuing to forbid federation. M25 and M26 remain accepted locally; these
local regression facts still are not a production-SLO claim.

## M26 pre-I/O semantic-evidence gate

Managed active-registry planning adds one deterministic stage ahead of the existing compiler:

```text
ValidatedAnalyticalRequest
    → resolve exact active registry
    → derive only the selected mapping/join dependencies
    → assert current approved semantic evidence
       ├── pointer/version/scope exact
       ├── live dependency generation/version/transition/watermark exact and complete
       ├── one current binding per mapping
       ├── current catalog fingerprints equal baseline
       ├── current join safety profile equal approved policy
       ├── no affected review/blocking impact
       └── exactly one common connection_id
    → deterministic compiler
    → independent AST guard
    → repeat semantic assertion before preview
    → bounded read-only preview
    → repeat semantic assertion before rejection inspection
```

The projection is dependency-aware. A report concerning another mapping or join does not reject
the request, but the latest affected review/blocking impact, missing live profile, incomplete
or replaced live dependency index, removed field, changed fingerprint, or cross-connection
dependency does. Live state must match the report/head generation, version, registry fingerprint,
pointer transition, completeness, watermark, and index fingerprint exactly. The external error is
the single sanitized `semantic_context_stale` boundary; it exposes no protected catalog identity
or source detail.

Recipe compatibility includes the same semantic-evidence boundary. Current recipe lookup is
qualified by the registry scope fingerprint, and reuse cannot proceed when its exact mapped/join
dependencies are stale. No stored recipe SQL is ever executed: an eligible recipe still passes
the current planner, deterministic compiler, independent guard, and read-only execution path.

Catalog-generation changes do not synchronously scan inside a query request. They enqueue durable
scope-qualified work. The semantic reconciler pages complete workflow/recipe dependencies and
observes only the active registry's bounded governed fields; aggregate join work is queued for the
matching source connection. This keeps a 5,434-table tenant from becoming a 5,434-table request.

M26 did not select the source DSN from the catalog. M28 supersedes that static deployment binding
with exact target-bearing execution/profile jobs and lease-bound dynamic connector routes.
Cross-workspace/cross-connection jobs and cross-connection joins still fail before source access.

## M28 target-bound compilation, cost admission, and source reads

M27's guided or short-description path still yields only the existing
`ValidatedAnalyticalRequest`. M28 adds no model authority and does not change `QueryPlan`; it binds
the executable target after current semantic evidence proves one connection:

```text
ValidatedAnalyticalRequest
    → ResolvedSemanticPlan without credentials
    → M26 gate: eligible + one exact CatalogConnectionId
    → resolve current GovernedExecutionTarget
    → target-bearing ResolvedSemanticPlan fingerprint
    → PostgreSQL compiler
         emits dialect=postgresql + target_fingerprint
    → independent PostgreSQL SQLGlot guard
         verifies dialect + target_fingerprint + existing AST policy
    → first bounded cost preflight
    → execution-approval checkpoint
    → exact plan/route/semantic reload
    → second bounded cost preflight
    → bounded read-only preview
    → exact semantic/target reload
    → bounded rejected-source inspection
```

The public target covers workspace, connection, connector/dialect, route revision/fingerprint,
expected reader, source/catalog/type-contract identities, and the complete cost budget. A change
in any field changes the target-bearing plan fingerprint. The old checkpoint, job, or recipe
cannot be moved to the new target; the user must prepare and approve a new plan.

Managed background jobs additionally persist the minimal public
`workspace_id`/`connection_id`/`route_revision`/`route_fingerprint`/`target_fingerprint`
reference. The job's top-level workspace remains the current verified submitter scope, while that
target workspace remains the immutable historical workflow/connector scope after identity
rotation. The worker compares the reference with the workflow's complete target, then presents
both scopes plus its current lease owner, transient capability, fence, expiry, connection,
contract, route, and target to the execution-route loader. Neither the job nor the workflow stores
a DSN or private binding, and the loader cannot replace the historical scope with the current one.

The same separation applies to catalog and aggregate profiles:

- a catalog refresh resolves only its `catalog` binding and binds the refresh/generation to the
  exact source, catalog, and type-contract identity trio;
- the cost preflight resolves only `preflight`;
- the preview/rejection worker resolves only `execution`; and
- the aggregate profile worker resolves only `profile`.

The public catalog route has no credential-binding field. An adapter-private
`ManagedCatalogConnectorRoute` pairs it with the opaque binding for the duration of source
resolution only. A new active generation is visible to executable readers only when its immutable
identity trio matches the current contract. This prevents a route rotated from source A to source
B from serving source-A metadata under a source-B target.

The first cost preflight is presentation/admission evidence and cannot authorize a later query by
itself. Immediately before preview, the application reloads the semantic gate and public target,
resolves the private preflight route again, and repeats the same bounded `EXPLAIN`. The preview
then reloads its execution binding independently. A rejected, timed-out, unavailable, malformed,
over-budget, stale-route, wrong-reader, or wrong-source-identity preflight performs zero preview
statement.

The PostgreSQL adapter receives `FORMAT JSON` as raw bytes through a connection-local bounded
loader. It rejects an oversized response before JSON decoding, treats an empty response as
invalid, rejects duplicate keys and non-finite constants, and parses decimal tokens directly as
`Decimal`. This prevents Python `float` rounding from turning a just-over-budget cost or
fractional estimated row count into an accepted value.

Application preflight always uses `ANALYZE FALSE`; it never executes the query to estimate cost.
`EXPLAIN ANALYZE` may appear only in isolated engineering benchmarks against synthetic data to
verify index/buffer behavior. Such a benchmark is not invoked by Query Studio, is not cost
admission evidence, and cannot substitute for the application preflight.

Only PostgreSQL is executable. An unsupported dialect produces no compiler, guard, route,
preflight, preview, or rejection call. The existing one-statement, allowlist, fanout, one
connection, three-table, two-join, result-limit, timeout, expected-reader, read-only transaction,
fetch-cap, and rollback controls remain independent.

The five governed query cases use this same target-bound compiler/guard and two-preflight path.
The dynamic inventory remains 10/75 or 5,434/41,028 according to tenant state; neither size changes
the width or authority of one query.

The reviewed PostgreSQL native-type contract now verifies its exact canonical SHA once when the
module is imported and then reuses that immutable value in O(1) for each field normalization. FULL
catalog pages keep their asset and field writes in the same transaction and lease; extreme pages
split field inserts into batches of at most 500 instead of raising the statement timeout.

M28 is accepted locally on synthetic evidence. Integration passed 164 tests with one known skip in
662.06 seconds and acceptance passed 47 tests in 133.50 seconds. The post-fix internal-browser
matrix passed 9/9 scenarios at both 1280x720 and 390x844: the accepted tenant paths returned
`approved_rows=2` and `approved_rows=3` through distinct readers, while all seven blocked paths
showed neither an action nor a result. M29 subsequently established its separately recorded local
operations/supply-chain baseline under D123; neither milestone is a production or release GO.
M32 has its own recorded final-byte gates and does not inherit either milestone's test evidence.

## M32 natural language to standalone PostgreSQL — locally accepted bounded scope

M32 adds a copy-first branch without changing the optional executor boundary:

```text
prepare natural-SQL preview
    → validate untrusted text/language
    → extract ≤ 12 exact source-grounded mentions
    → search the complete current approved registry
    → build relevant closure ≤ 3 models / 12 fields / 2 joins
    → typed v1/v2 request or closed ambiguity
    → validate fields, values, types, stages, shapes, and context fingerprints
    → resolve approved datasets/mappings/joins/fanout deterministically (no SQL)
    → signed preview with resolved-plan fingerprint and review evidence

confirm preview / generate copy artifact
    → reload current registry and reconstruct the signed logical-field closure
    → verify exact preview/request/context fingerprints
    → revalidate and re-resolve approved mappings, transformations, joins, fanout, and target
    → select v1 or v2 by exact representability
    → compile parameterized PostgreSQL
    → independent AST guard
    → render typed literals by textual parameter index
    → independent zero-binding AST guard
    → standalone SQL artifact (`executed=false`)
```

Preparation resolves the typed request only to bind approved datasets, mappings, joins, fanout,
evidence/risks, and the resolved-plan fingerprint into the human preview; it has no compiler,
guard, renderer, cost-preflight, or executor dependency and produces no SQL. Confirmation
re-resolves the same signed request against reloaded current context and has no executor
dependency. Optional validation/execution is a separate operation and accepts only the
parameterized guarded form; standalone SQL is never fed back into execution.

Retrieval traverses every current approved logical model and field in scope. Lexical scoring uses
model/field names, definitions, and governed values, with role compatibility only as a bonus after
a lexical hit. Canonical types, roles, and value constraints are exposed and validated in the
bounded closure; types are not free-text search tokens. Closure construction and semantic
resolution then validate the relevant approved mappings, transformation plans, join
contracts/cardinality/fanout, and freshness bindings. Confirmation does not rerun retrieval or
either language stage: it reconstructs the closure from the signed logical-field set against the
reloaded registry. Only the relevant bounded closure is visible to interpretation and planning.
M32 retrieval does not consume physical discovery; catalog-only results stay
`needs_mapping_review` in the separate M27 lane.

### Representability route

Version 1 remains the route for requests that fit the historical flat request exactly: supported
aggregate-mode dimensions/date grains, ordinary field aggregates, one flat `AND` filter list,
field ordering, and limit. Version 2 owns row mode, fieldless row count, `OR`/`NOT`, conditional
aggregates, numeric buckets, `HAVING`, windows, output-stage predicates/order, advanced aliases,
and advanced grouping.

This is a semantic test after structured interpretation. Prompt length, requested SQL line count,
keywords, language, and model confidence are ignored. A verbose flat aggregate remains v1; a short
ranking request becomes v2.

### Version-2 evaluation stages

The compiler chooses the smallest topology:

1. direct `SELECT` for a simple version-2 shape that needs no output-stage boundary;
2. compiler-owned `aggregated` for scans/joins, `WHERE`, projection, grouping, aggregates, and
   optional `HAVING`;
3. compiler-owned `windowed` only when closed window expressions are requested;
4. final explicit `SELECT` for output filtering, deterministic ordering, and the literal limit.

The closed operations cover row/aggregate mode, boolean trees, standard/conditional aggregates,
numeric buckets, rankings/`NTILE`, partition averages and percentages, running/moving sums and
averages, `LAG`/`LEAD`, delta/percentage change, and top-N/output predicates. There is no raw
expression, subquery, set-operation, user CTE, or dynamic identifier node.

PostgreSQL requires a new query level to filter a window result. The compiler-owned CTE topology
therefore expresses evaluation order without admitting arbitrary subqueries. Every selected
column is explicit and every alias reference is validated against the stage that defines it.

`ROLLUP` is not part of the M32 language. A future subtotal contract must first expose
`GROUPING()` flags so a subtotal `NULL` cannot be confused with a genuine governed `NULL`.

### Parameterized and standalone forms

The compiler produces one parameterized executor form first. After its independent guard passes,
the copy renderer scans the SQL with single/double/dollar-quote awareness and converts textual
`%s` positions into numbered parameters. It reparses that statement and replaces `$n` with the
corresponding typed literal AST node. It does not infer association from SQLGlot traversal order.

The renderer accepts only supported finite typed values and must leave zero parameters. The
normalized PostgreSQL then passes the complete independent guard again. The returned artifact
contains dialect, plan version, request/plan/target fingerprints, SQL SHA-256, and
`executed=false`; SQL and literal values are transient output, not workflow/recipe/audit data.

### Unsupported capability outcome

The benchmark boundary is explicit:

- supported: bounded ranking/top-N/`NTILE`, partition averages, duplicates and `HAVING`,
  running/moving calculations, `LAG`/`LEAD`, delta/percent change, conditional metrics, and
  numeric buckets;
- rejected without approximation: cross/self joins, arbitrary subqueries, `UNION`/`INTERSECT`/
  `EXCEPT`, recursion, gaps/islands, and `ROLLUP` without safe grouping flags.

An ambiguous, stale, unsupported, unapproved, or over-limit request stops before compilation.
