# UI specification

SchemaBridge should look like a professional semantic-governance and query-planning tool, not primarily like a chatbot.

## Navigation

```text
Overview | Semantic Models | Relationships | Query Studio | Decisions
```

## Overview

Show:

- datasets and fields scanned;
- candidate concepts;
- unresolved conflicts;
- high-risk type mismatches;
- approved models and joins;
- recent decisions;
- DataHub and source connection health.
- active semantic-registry identity, version, catalog scope, full fingerprint, and model/mapping/join
  counts.

Primary action: **Scan catalog**.

## Semantic Models

Three-column review layout:

```text
Physical fields | Canonical definition | Evidence and risks
```

For each field show asset, type, description, profile summary, samples only when safe, transformations, confidence, and evidence. Actions: approve, edit, reject, mark as different concept, validate, publish.

In M21, the current read-only registry view shows all approved logical/physical mapping identities,
types, closed transformations, status, versions, and decision IDs from `synthetic_enterprise`.
Candidate scoring remains the bounded Customer candidate workflow; it is not a registry-wide field
search. The registry contains seven models and 31 mappings, but matching an arbitrary short field
description across those mappings is M27 scope.

## Relationships

Graph/table of proposed model relationships. A detail panel shows:

- left/right model and fields;
- physical join expressions after normalization;
- evidence sources;
- value overlap;
- cardinality;
- duplicate/null rates;
- fanout policy;
- status and version.

M21 renders all five approved contracts from the active registry and replaces the old fixed
Customer-only relationship diagram with registry-derived rows and details. Evidence, risks,
key transformations, status/version, cardinality, and fanout remain visible; approval decision IDs
remain visible in the approved mapping registry and decision workspace. This registry-wide display
does not raise the executable limit of two joins and three physical tables per request.

## Query Studio

> **Historical M14/M21 demo baseline.** This section through the original validation/decision/UX
> rules predates M32. Its generated-SQL-before-execution, preview/export, `publish context`, and
> `Load demo scenario` actions are not the current managed contract. For M32, SQL/download appear
> only after exact fingerprint confirmation; managed execution/publication/demo controls remain
> absent. The normative current surface starts at **M32 copy-first natural SQL surface** below.

Two synchronized modes:

1. guided fields and operators;
2. natural-language request.

At M21 these modes remain synchronized only for the bounded Customer/AccountHolder workflow
implemented in M09–M14. Product, order, sale-line, and shipment scenarios are verified through
typed requests and evaluation. Catalog-driven controls and natural-language matching that select
logical fields from a slight free-form description are M27; the UI must not imply that capability
before then.

Both render an editable interpreted request. Before execution show:

- selected logical concepts;
- selected physical assets;
- approved mapping versions;
- join path and confidence;
- cardinality and fanout mitigation;
- assumptions/ambiguities;
- generated SQL;
- policy-check results.

Actions: edit interpretation, validate, preview, export SQL, save recipe, publish context.

## Validation result

Show:

- returned row count;
- scan/timeout information available to the adapter;
- validation findings;
- rejected source values and reasons;
- result table;
- downloadable SQL, query plan, and report.

## Decisions

Version history for models, field mappings, joins, and recipes. Show actor, time, evidence, changes, known risks, DataHub publication result, and linked assets.

## UX rules

- Do not hide ambiguity behind a confidence number.
- Red/yellow/green status must also have text/icons for accessibility.
- Disable execution until required approvals are satisfied.
- Keep the demo path reachable in fewer than six primary interactions after seed data exists.
- Provide a deterministic “Load demo scenario” action.
- Errors must explain the next corrective action without exposing secrets.

## M21 registry presentation

The verified default presentation is bound to:

```text
registry: synthetic_enterprise v1
catalog scope: synthetic-demo
logical models: 7
approved mappings: 31
governed joins: 5
fingerprint: 0710148874049078f751ac96f0a18131cf01daa41f27dc034ca52a8b212dd966
```

Overview displays that identity and full fingerprint. Semantic Models and Relationships expose
both the Customer/account domain and the Product/order/fulfillment domain without claiming that
the snapshot was reconstructed live from DataHub. The recorded snapshot remains M21 synthetic
evidence; M22 adds complete DataHub read-back and M23 owns activation/migration.

Before rendering governed reference data, the UI must pass the M20 `VIEW` authorization boundary.
A denied/no-role principal receives only the safe permission boundary and causes no registry,
candidate, or workflow-store I/O.

## M22 live-registry presentation

When deployment configuration selects the live registry, the UI uses the same view models but must
label the boundary `live:datahub`; it must not retain the recorded-bundle label or silently recover
with manifest data. For the current local-demo workspace, the visible identity is:

```text
registry: synthetic_enterprise v1
catalog scope: synthetic-demo
source: datahub:schemabridge-semantic-registry-synthetic_enterprise-v1-w3f92b9304d7d1f8f0cbf410c
logical models / approved mappings / governed joins: 7 / 31 / 5
fingerprint: ef480eb7370924ff4c94131a2c6c4063d85652cc83aaec9059538cdc2cf9c1b9
```

Overview shows the source label, full fingerprint, version, and counts. Semantic Models and
Relationships render only the reconstructed live snapshot. Missing, forbidden, corrupt,
out-of-scope, or stale live context produces a typed safe state and no execute action; the UI does
not offer a fallback control.

Managed profiles do not render registry/catalog mode selectors. The workspace is derived from the
authenticated principal before registry composition, so a target prepared for one workspace cannot
be displayed in another. The public `hosted-demo` profile remains explicitly recorded.

This changes the registry source, not Query Studio's language coverage. Names and field definitions
remain governed evidence, while matching a brief description across all fields and presenting
dynamic catalog-driven controls remains M27. The M22 browser record verifies that fixed
live-version presentation. The distinct M23 record below verifies the PostgreSQL-selected active
generation and projection states; the two evidence sets must not be conflated.

## M23 active-generation and projection presentation

When `SCHEMABRIDGE_SEMANTIC_REGISTRY_SELECTION=active`, the authenticated Overview must display
two distinct layers:

```text
Governed content
  registry ID · catalog scope · immutable version · registry fingerprint · 7/31/5 counts

Control state
  authoritative activation generation · active-pointer fingerprint · projection status
```

The registry fingerprint identifies the immutable governed content. The active-pointer fingerprint
also binds workspace scope, generation, target, transition, and activation facts; neither may be
shortened into an ambiguous label. The DataHub projection status uses the closed vocabulary
`pending`, `delivered`, `superseded`, `blocked`, or `unknown`. Fixed local/hosted-demo mode instead
shows `fixed version (no managed activation pointer)` and does not invent a generation.

Presentation semantics are:

- `delivered`: green success with text, after the outbox records exact projection delivery;
- `pending`: yellow warning with text; PostgreSQL remains authoritative and the query path may
  continue against its exact active version;
- `blocked`: red error with text and no claim that projection repair succeeded;
- `superseded` or `unknown`: neutral information requiring operator inspection;
- a pointer/generation/fingerprint mismatch: fail composition safely rather than render partial or
  guessed status.

The UI obtains these fields from the active registry loader and a runtime-role read of the exact
transition outbox. It does not infer state from the DataHub projection document. Managed workflow,
access, review, and request stores use the same authenticated workspace but raw OIDC claims,
tokens, email, and result-row payloads do not enter this reference model.

Exact preview rows may be held only in a session-local `TransientExecutionResult`. Before merging
them into a rendered durable workflow, the UI must match actor, workspace, workflow, revision,
registry fingerprint, activation generation, active-pointer fingerprint, and recompute the
preview fingerprint from the row payload. Mismatch, registry drift, tampering, or principal change
purges the envelope. A durable record with `rows=[]` and a positive `row_count` renders the count
and states that transient rows are unavailable; it does not render a false zero-row preview.

Activation, rollback, migration, reconciliation repair, legacy import, identity rotation,
backup, and restore remain operator actions. Streamlit must not expose buttons for them or receive
migrator, reconciler, restore, or DataHub writer credentials. Errors remain typed and sanitized.

The 2026-07-23 operated M23 browser acceptance captured the full state sequence: generation 2
remained authoritative while its projection was pending, exact reconciliation changed delivery to
`delivered`, a stale generation-1 workflow was blocked, and rollback created generation 3 rather
than rewriting history. A fresh workflow returned `2026-01-01=2`, `2026-01-02=1`, and
`2026-01-03=1` plus the expected `non_integral_identifier`, `non_finite_identifier`, and
`null_join_key` rejections. Its durable PostgreSQL execution record retained row count 3 and a
preview fingerprint with `rows=[]`.

The final fresh-tab view at 1280x720 displayed generation 3, registry version 6, projection
`delivered`, pointer fingerprint
`85f131dd53bb5cf2da8538cc171277c4d988b77101e0e5b37da46fed2ef24757`, and 7/31/5
counts. Browser warning/error output was `[]`. At 390x844, document and body scroll/client widths
were all 390; no horizontal overflow occurred. The reset 1280x720 view also ended with console
`[]`. This closes local M23 UI acceptance, not the production deployment gate.

## M24 execution-job presentation

M24 does not replace Streamlit's review surface and does not add a generic task console.
Streamlit remains responsible for interpretation, ambiguity resolution, governed plan inspection,
and the human execution-approval checkpoint. The authenticated HTTP API may reserve only that
already reviewed preview.

Any operator/developer presentation of an M24 job must show:

- job ID and the exact workflow ID;
- `queued`, `leased`, `cancel_requested`, `retry_wait`, `succeeded`, `failed`, `cancelled`, or
  `dead_lettered` as text, not color alone;
- attempt count/max attempts and bounded timestamps;
- expected workflow revision and full plan fingerprint;
- a closed failure code when terminal/retrying;
- on success, workflow revision/stage, row count, full preview fingerprint, exact rejected-row
  total, bounded grouped rejection-code counts, exact unclassified residual,
  completeness/truncation flags, and completion time;
- `replayed=true` when an exact idempotent submission returns the existing job.

It must never render actor/tenant claims, authorization internals, an idempotency digest, lease
owner/capability/fence, bearer/JWKS material, DSN, SQL, parameters, prompt, source values, or
preview rows. The summary must not imply that rows are downloadable: exact rows remain restricted
to the separately authenticated, context-bound Streamlit session.

Cancellation is an explicit destructive-intent-style action with confirmation
`CANCEL EXECUTION JOB`. A queued cancellation may display immediate terminal success. A leased
request must say that cancellation is cooperative and remain `cancel_requested` until worker
acknowledgement; it must not claim rollback of a completed source read.

The only unauthenticated browser-visible API state is bounded liveness/readiness. Cross-tenant,
unknown-resource, missing-role, and expired authorization use one indistinguishable safe boundary.
No traceback or dependency topology is rendered.

The final M24 internal-browser record verified readiness, exact replay, safe collision, queued
cancellation, retry, real worker crash/reclaim, bounded summary-only success, indistinguishable
wrong-role/cross-tenant denial, clean protected-data/console scans, and no overflow at desktop or
390x844. This is local acceptance evidence, not a production deployment claim.

## M25 tenant catalog handoff

M25 defines the professional inventory-browser handoff before M27 adds semantic description
matching. Its implemented HTTP surface and acceptance panel expose a PostgreSQL-indexed view of
one authenticated workspace, not a live DataHub search and not an executable-query builder. The
acceptance panel is instrumentation; product Query Studio integration remains M27.

### Information architecture

Add a tenant catalog area reachable from Overview:

```text
Connections
  → one connection
      → active generation and refresh history
      → assets
          → one asset
              → fields
```

The connection list shows display name, closed source kind, environment, public catalog scope,
enabled/disabled state, active generation, asset/field counts, last successful refresh, observed
time, and freshness/staleness. Never show the opaque credential-binding reference, route, token,
DSN, checkpoint, lease owner/capability/fence, cursor content, or source sample.

The asset table shows qualified/display name, platform/environment/schema, field count,
description, and observation time. The field table shows path, native type, definition,
nullable/key flags, governed terms, and observation time. These values are inventory context, not
approved equivalence. Duplicate names in another connection must remain visibly qualified by their
connection.

### Pagination and large inventories

The small fixture has 10 tables and the large fixture has 5,434 tables across multiple connections.
The UI must render only the current bounded page:

- default and maximum page size no greater than 50;
- server-side keyset pagination, never a full download or client-side slice of all items;
- current page virtualization is allowed, but it must not preload later pages;
- next-page navigation uses the opaque server cursor; prior-page navigation may retain only a
  bounded session history of already received cursors;
- search/filter changes clear cursor history and start a new bound traversal;
- unavailable totals render “count unavailable” rather than an inferred value;
- asset/field pages display their exact generation, and a stale/pruned generation cursor returns
  one safe unavailable state with a restart-navigation action.

First, middle, and final page states need distinct empty/loading/error affordances. Loading must not
replace the current page with a misleading empty catalog. At 390x844, use a stacked card/detail
layout or horizontally scroll the bounded table inside its own labeled region; the document itself
must not overflow.

### Refresh and administrative actions

Only a current `platform_admin` sees:

- **Register connection**;
- **Disable connection**;
- **Request full refresh**;
- **Request delta refresh** only when the source explicitly supports real delta pages.

Each action shows the exact tenant/connection target, requires its closed confirmation, and uses an
idempotency identity. The form accepts public metadata and an opaque binding reference only; it
must reject credential/DSN/token fields. Disable is logical and must not imply source deletion.

Refresh status uses text plus icon/color for:

```text
requested · leased · staging · completed · failed
```

Show target/base generation, bounded page/asset/field counts, requested/updated/completed times,
full catalog fingerprint when terminal, and a closed safe failure reason. During staging, keep the
previous active generation visible and explicitly state that partial metadata is not queryable.
After DataHub becomes unavailable, a completed indexed generation remains browsable but is labeled
stale; a failed new refresh does not switch to recorded data.

Rate denial renders bounded `Retry-After`; connection/asset/field/job capacity denial names the
policy dimension and corrective operator action without exposing another tenant's usage. Missing
policy, pool saturation, cursor failure, and unavailable generation use typed sanitized states and
no stack trace.

### Query Studio boundary

Inventory cardinality and query width are separate visual facts. Every catalog page must retain a
short safety note:

```text
5,434 indexed tables available to browse.
One governed query may use at most 3 physical tables and 2 approved joins from one connection.
```

Selecting or viewing an inventory field does not create an approved mapping, join, or executable
plan. M25 makes no OpenAI request. M27 may add bounded candidate retrieval from a slight field
description, evidence ranking, and explicit ambiguity confirmation. Until then, no button or copy
may imply automatic semantic match or free-form multi-domain query generation.

### M25 browser evidence status

`scripts/m25_catalog_browser_panel.py` now provides bounded acceptance instrumentation for small,
large, and live-DataHub workspaces while retaining each bearer only server-side. Its focused unit
suite passed during implementation. The panel is not the production Query Studio and does not
perform semantic matching.

The final internal-browser record covers both tenants, first/middle/final pages, changed filters,
genuinely expired/tampered/cross-scope cursors, rate denial, DataHub-offline staleness,
protected-data scanning, a clean console, desktop layout, and 390x844 overflow. M25 is accepted
locally; the exact observations remain in `tasks/M25_HANDOFF.md`. This panel is still acceptance
instrumentation and does not establish a production-UI claim.

## M26 semantic-change acceptance surface

M26 adds read-only acceptance instrumentation, not a mutation workflow inside Query Studio.
Authenticated users may view bounded report, finding, and impact projections. Baseline,
revalidation, rejection, registry publication, and activation remain separate trusted operator
flows.

The report list/detail must display:

- `current`, `review_required`, `blocked`, `revalidated`, `rejected`, or `superseded`;
- pointer generation/fingerprint, registry version/fingerprint, observation fingerprint, and
  optional baseline revision;
- finding and mapping/join/workflow/recipe impact counts;
- dependency completeness, watermark, and impact-set fingerprint;
- inspection time and a clear stale/unavailable state.

Finding rows show only the closed kind/severity, governed target identity/version, old/new
fingerprints, bounded risks, and finding fingerprint. Impact rows show kind, artifact
identity/version, finding IDs, and impact fingerprint. Raw catalog definitions, source values,
SQL, parameters, credentials, actor claims, audit keys, and prompts are never rendered.

Every list is a signed keyset page of at most 50. The surface must support first/middle/final-page
navigation without keeping a full impact list in browser state. Unknown/cross-tenant reports and
stale/tampered cursors use the same unavailable presentation. There is no approve, reject,
revalidate, activate, reconcile, publish, or source-read button.

Status copy must explain the exact safety outcome:

```text
Current: approved evidence matches this plan's exact dependencies.
Review required: a compatible governed field changed; execution remains blocked until revalidated.
Blocked: structural or join-safety evidence changed; publish and activate a corrected registry.
Coverage incomplete: the blast radius is not known completely; approval and execution are blocked.
Unaffected: this report does not reference the mappings or joins selected by the current plan.
```

Source failure, invalid/stale scope, or the 10,000-artifact/100,000-edge safety cap must use
`Coverage incomplete`; the panel must never relabel a capped set as a complete total.

Catalog scale and query scale stay visible as separate facts. A 5,434-table workspace may have
many pages, but one governed plan still uses one connection, at most three tables, and two joins.
The panel must never imply that a similarly named field in another connection is an automatic
replacement.

`scripts/m26_semantic_change_browser_panel.py` is the loopback-only same-origin reader used for
browser acceptance. It keeps the bearer server-side, calls only the four allowlisted GET routes,
and can render current/review/blocked/remediated fixtures for layout tests. The final local record
used the real upstream API, passed at 1440x900 and 390x844 with no overflow, protected-data hit,
console warning/error, or mutation/approval control, and is preserved in
`docs/14_BROWSER_ACCEPTANCE.md` and `tasks/M26_HANDOFF.md`.

## M32 copy-first natural SQL surface

The M32 surface prepares a text-free typed preview, displays exact datasets/mappings/joins,
assumptions, fanout/`NULL` policy and route v1/v2, and requires confirmation of the same
fingerprint. SQL and download controls are absent before confirmation. The final artifact is
PostgreSQL standalone, independently reparsed/guarded, contains no placeholders and always shows
`executed=false`.

Ambiguous, stale, physical-only and unsupported requests show a closed reason and no SQL. The
current UI asks the user to rewrite; interactive slot resolution and a help/feature-request path
remain gaps. M30 hardening now makes the managed staging/production path resolve one registry-v2
PostgreSQL target before interpretation, but only after the complete registry passes the M26
semantic-current gate. It signs connection ID, route revision, target fingerprint and type-contract
fingerprint in a `qsp3` preview token and resolved-plan fingerprint; selected dependencies pass M26
again before each later target resolution at confirmation and SQL generation. Any absence,
revocation, cross-connection substitution or drift removes SQL/download. The UI shows the target
before the confirmation checkbox and again in artifact integrity.

An artifact cached in Streamlit session state is not display authority. Every later rerun
provider-free revalidates the retained confirmed typed request and regenerated artifact before
rendering SQL/download; semantic/target drift or a changed result purges both controls and shows a
sanitized closed error.

Development and recorded acceptance may still produce `target_fingerprint=None`, but the UI must
label that path `unbound-local-recorded` and explicitly non-commercial. That lane cannot be exposed
to a tenant or used as M30 evidence. The implementation addresses the former local P0 contract gap
and remains under final exact-byte verification; operated target evidence, the remaining browser
matrix and M30/M31 certification remain open.

## M33 onboarding acceptance surface

M33 is a tenant-bound authoring/review/preparation surface. Analyst creates a draft from server-
derived catalog preflight; steward records append-only model/mapping decisions; a different
publisher confirms the exact fingerprint and produces `ready_for_publication`. It displays zero
source/DataHub writes and no SQL. The scenario app is acceptance instrumentation, not the managed
customer console.

## M34 publication acceptance surface

M34 displays `queued → leased → awaiting_approval → approved → leased → activation_ready`, exact
target/base/candidate fingerprints and read-back outcome. Authorization appears only after the
complete candidate is assembled. No control may delete/overwrite a conflicting target or activate
the registry. The dedicated publisher is a separate workload and its DataHub credential never
enters browser/API. Local scenario writes target a synthetic adapter unless a separately labelled
live-DataHub gate runs.

## M35 change acceptance surface

M35 presents one governed join or one-model replacement/remediation proposal, complete incident
join actions, aggregate-only profile evidence, impacts/risks and separate steward/publisher stages.
Self join, cross connection, many-to-many without mitigation and stale authority remain no-write
outcomes. The accepted candidate reaches the same M34 `activation_ready` handoff; M23 activation
and M26 reinspection remain external operations.

M32–M35 are not yet one integrated production navigation. Commercial onboarding, daily use,
incident handling and offboarding follow [`docs/commercial/README.md`](commercial/README.md) and
retain NO-GO until M30/M31 evidence exists.
