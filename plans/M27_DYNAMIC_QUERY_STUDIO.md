# M27: Dynamic Query Studio and governed description matching

- Status: complete and accepted locally on 2026-07-27; production/release NO-GO
- Timebox: 64 hours across three sequential M27 phases
- Recommended Codex: GPT-5.6 Sol — Extra High (xhigh)
- Dependencies: accepted M25 dynamic catalog and accepted M26 semantic-change gate

## Objective

Replace the Customer/AccountHolder-specific query entry surface with a tenant-scoped Query Studio
whose guided controls and natural-language vocabulary are derived from the active catalog,
approved semantic registry, and current semantic evidence. A user may search with a short field
description or describe an analytical request in Spanish or English, inspect bounded candidates,
resolve every ambiguity explicitly, and hand one validated typed request to the existing governed
planner.

Catalog cardinality remains data, not code. The same implementation must support a tenant with 10
tables and another with 5,434 or more without loading the complete inventory into the browser,
Python process, or model prompt. That inventory breadth does not change the executable request
limit: one governed connection, at most three physical tables and two approved joins, row and
statement bounds, fanout policy, deterministic compilation, independent AST validation, and
read-only source execution.

## Product boundary

M27 has two visibly different lanes:

1. **Executable governed matching.** Search and natural language may reference only logical fields
   backed by the current active registry and an M26-approved/revalidated exact physical binding.
   Expansion output is limited to exact slot IDs plus atomic search text; the server derives and
   validates one unique source span. Interpretation output is limited to exact slot IDs plus the
   single exposed option index and bounded ambiguity kinds; the server maps that index to the
   candidate and derives any governed filter value. The server owns every semantic control and
   reconstructs the final typed proposal. Model output never approves a mapping or join.
2. **Physical discovery.** The existing M25 paginated catalog can show ungoverned physical assets
   and fields as `needs_mapping_review`. A discovery result cannot enter an analytical request,
   create a join, reach the compiler, or mutate DataHub/source state.

Names, definitions, types, tags, terms, deterministic scores, and model output are evidence only.
A unique short-description match is not a standalone approval: to become executable it must be
included in a complete typed request and that request must receive exact fingerprint-bound human
confirmation. `ambiguous`, `no_match`, `conflicting`, `stale`, and `provider_unavailable` are
first-class safe outcomes.

M27 does not add a raw-SQL input, connector federation, a fourth query table, an automatic mapping
approval, an automatic DataHub write, or a model-to-source tool path. M28 owns connector routing,
dialect expansion, and database explain/cost controls.

The milestone remains one vertical slice but is delivered in three non-accepting phases:

- **M27A:** governed/physical retrieval separation, signed preview, human confirmation, and dynamic
  guided UI;
- **M27B:** minimal OpenAI slot expansion/selection, deterministic server reconstruction, tenant
  opt-in, admission, privacy, and sanitized audit;
- **M27C:** full corpus, scale, live model selection, browser, package, quality, and coverage gates.

No phase is accepted independently and M28 cannot start until all M27 criteria pass.

## Dynamic retrieval contract

Add a bounded registry-aware field-search port and PostgreSQL adapter. This port is executable
candidate retrieval only. The existing M25 connection/asset/field paging remains a separate
physical-discovery port and always labels fields without current evidence `needs_mapping_review`;
the two result types and cursors cannot be combined.

One governed search:

- derives workspace from the authenticated principal and binds catalog scope, registry identity,
  pointer generation/fingerprint, evidence-head revision, and active catalog generations;
- intersects active M25 field metadata with only the current M26 approved/revalidated bindings;
- searches field name, definition, native/normalized type, tags, and glossary terms through indexed
  predicates and exact locator joins;
- returns at most 50 rows plus one keyset continuation and never uses deep `OFFSET`;
- carries connection, asset, field path, logical field, mapping decision/version, generation,
  evidence fingerprints, public metadata, deterministic signal breakdown, and risks;
- never returns raw samples, source values, credentials, routes, DSNs, claims, or unrestricted
  registry payloads;
- invalidates its cursor/candidate set when any bound pointer, head, generation, query, filter, or
  scope changes.

A blank guided browse uses a stable logical-field keyset. A text search uses a stable deterministic
integer signal key plus logical field and binding identity; no approximate float becomes cursor or
identity material. Search may inspect the bounded registry population, currently capped at 1,000
logical fields, but it may not materialize the 41,028-field physical inventory or perform all-pairs
matching.

PostgreSQL does not duplicate the complete logical registry. The v6 function returns current
binding/catalog facts plus `logical_field` and the exact active pointer/head fingerprints. The
application loads the immutable registry selected by that pointer through the existing
`GovernedSemanticRegistryPort`, requires the returned registry fingerprint/version to match, and
enriches candidates with model definition, field definition, type, role, allowed values, and join
topology in memory. The registry limit makes that enrichment bounded; the physical catalog remains
keyset/index driven. Registry activation stays atomic through M23 and no v6 projection becomes a
second semantic authority.

The executable shortlist contains at most 20 candidates. Its prompt vocabulary contains at most
three models, twelve fields, and two approved joins. The closure is either complete and explicit or
returns `closure_overflow`; it is never silently cut to fit.

The server derives at most six exact field-purpose slots from the normalized business text.
Expansion may fill each slot only with `slot_id` and `query`; it cannot choose the purpose, role,
canonical type, metric operation, filter operator, date grain, or source span. The server derives
an exact unique source span from the original request. Retrieval keeps at most two top candidates
per exact slot for inspection, but interpretation exposes only option `1` when its compatible
candidate is the strict deterministic leader. A top-score tie is typed ambiguity before provider
interpretation. More than twelve combined fields, more than three models, more than two required
joins, or an incomplete approved path returns `closure_overflow`. The broader top-20 page remains
inspectable but is never silently reduced into a supposedly complete intent vocabulary.

## Pure domain contract

Add immutable values under `domain/query_studio.py` for:

- normalized description queries, bounded search probes, filters, and keyset positions;
- approved governed candidates and non-executable physical-discovery candidates;
- deterministic signal/evidence breakdowns, risk codes, and candidate ranks;
- shortlist scope, completeness, ambiguity, and canonical fingerprint;
- model-safe opaque candidate IDs;
- semantic match states: `aligned`, `ambiguous`, `no_match`, and `conflicting`;
- independent operational states: `stale`, `closure_overflow`, `provider_unavailable`,
  `rate_limited`, `quota_exhausted`, and `sensitive_input_blocked`;
- minimal provider slot/query and slot/option envelopes plus server-reconstructed structured
  proposals that can reference only scoped server-side candidate IDs;
- exact interpretation previews and confirmations;
- provider configuration fingerprints and sanitized usage facts.

Domain collections and text/byte sizes are bounded. Provider output forbids extras and has no
field for SQL, a physical identifier invented by the model, a query plan, a join predicate, a
tool, an approval, execution, or DataHub mutation.

The confirmation fingerprint covers normalized user text, complete shortlist, registry scope and
fingerprint/version, active pointer generation/fingerprint, M26 head/baseline revision, catalog
generation vector, model snapshot, prompt/schema version, deterministic matcher version, and the
typed proposed request. A current-state reload must reproduce those values before confirmation can
yield the existing `ValidatedAnalyticalRequest`.

Candidate IDs are HMAC-derived opaque values over the complete scoped locator and logical mapping;
they expire with a server-signed preview token after at most ten minutes. The token contains only
version, request digest, shortlist/proposal/config fingerprints, scope digest, issued/expiry time,
and nonce—never request text, definitions, candidates, raw tenant/actor IDs, or credentials. The
browser resubmits the visible typed proposal and original text. Confirmation re-normalizes the
text, reloads registry/pointer/head, recomputes deterministic retrieval and shortlist, validates
the proposal against those exact candidates, and authenticates every digest. Model output is not
replayed or trusted from `session_state`; any difference is stale before planning.

## Application flow

### Guided mode

1. Page/search current governed fields from PostgreSQL; do not build a flat in-memory selector.
2. Render logical and exact physical evidence, definition, type/role, connection, freshness,
   mapping version, risks, and executable/non-executable status.
3. Derive compatible dimensions, metrics, filters, values, grains, and reachable joins from the
   active logical context and selected field types/roles.
4. Build a new immutable `GuidedRequestDraft/Preview`; do not invoke the existing validating
   builder yet.
5. Require exact interpretation confirmation and reload current registry/evidence.
6. Only the confirmation use case may pass the approved primitive choices to
   `BuildGuidedRequest.execute()` and obtain `ValidatedAnalyticalRequest`.
7. Pass only that validated value to the existing M26 gate and governed planner.

### Natural-language mode

1. Validate and normalize at most 2,000 characters/8 KiB of untrusted Spanish or English text.
2. The server derives the exact bounded slot inventory and owns its purposes, roles, canonical
   types, metric operations, filter operators, date grains, and selection counts.
3. A description-expansion port returns only `slot_id` and grounded `query` for every required
   slot. It receives no catalog IDs or tools; the server derives one exact unique `source_span`
   from the original request.
4. Deterministic governed retrieval resolves those exact slots into a bounded shortlist.
5. The application creates an `ApprovedPublicMetadataSurface` over the exact policy, tenant
   semantic scope, active registry, and prompt-vocabulary fingerprints. Missing, stale,
   cross-registry, or restricted metadata fails before the interpretation provider boundary.
6. A typed intent port sees only the user text and that minimum approved vocabulary. It returns
   only complete `slot_id`/`option_index=1` pairs plus bounded `ambiguity_kinds`—never candidate
   IDs, filter values, roles, types, operations, joins, ordering, limits, or semantic approval.
7. The server exposes an option only for a strict compatible top-1 candidate, maps the returned
   index to that exact candidate, derives any canonical filter value from the request and governed
   allowed values, and reconstructs the proposal with server-owned controls, empty ordering, and
   the bounded default limit. A tied top score is ambiguity before this provider call.
8. The UI shows the chosen evidence and any available alternatives, then requires confirmation of
   the complete typed request before the existing request validator/planner can run.

The normal live path is at most two provider calls: one bounded expansion and one bounded typed
interpretation. A no-match retrieval stops before the interpretation call. Provider failure never
falls back to the deterministic fake while labelled live, never changes workflow state to
confirmed, and never reaches compilation/source I/O.

Guided and natural-language modes must converge on byte-identical typed request and resolved-plan
fingerprints for equivalent business intent.

The reviewed v16 live boundary uses prompt `m27-openai-prompts-v14`, schema
`m27-query-studio-v8`, expansion contract `m27-expansion-contract-v7`, slot-selection contract
`m27-slot-selection-v2`, and proposal normalizer `m27-proposal-defaults-v3`. Empty selections plus
one or more ambiguity kinds reconstruct `ambiguous`; empty selections without an ambiguity
reconstruct `no_match`. Partial exact-slot coverage, duplicate/unknown references, an option other
than `1`, or selections mixed with ambiguity are invalid provider output in `semantic_contract`,
never a business `conflicting` result.

## OpenAI, privacy, and cost boundary

The existing usable `OPENAI_API_KEY` is reused only from process environment/secret-manager
configuration. It is never read into documentation, copied to a tracked or local env file, printed,
logged, persisted, placed in fixtures, or sent to the browser. Unit/CI defaults remain a
deterministic key-free fake; live mode is explicit.

The live adapter uses the Responses API structured parser with:

- no tools;
- `store=false`;
- a strict Pydantic output type with extra fields forbidden;
- bounded output tokens, request timeout, and at most one retry for a transient provider failure;
- an allowlisted snapshot and evaluated reasoning effort;
- a stable privacy-preserving safety identifier, never a raw actor/workspace;
- sanitized error codes for timeout, 429/5xx, refusal, missing output, or invalid structure.

Provider input contains only normalized business text and the minimum bounded governed shortlist.
No sample or source-row value is added automatically. User text is screened before egress for
emails, access tokens, credential/DSN patterns, private-key markers, obvious payment identifiers,
and high-entropy secret-like spans. A hit blocks live AI visibly and leaves guided/manual mode
available; it is never silently redacted into a different business request. This screening reduces
risk but is not represented as a complete PII detector or a substitute for provider governance.

Provider input contains no automatically retrieved samples or source values, SQL, parameters, DSNs,
credentials, raw workspace or actor IDs, claims, DataHub tokens, secret paths, full catalog export,
or private reasoning.
Untrusted user text and untrusted catalog metadata are separately delimited. Metadata that is
restricted, secret-like, credential-like, or outside the approved public definition surface blocks
live model use and leaves guided/manual matching available.

`ApprovedPublicMetadataSurface` is required for interpretation under policy version
`m27-approved-public-definition-surface-v1` and binds the managed public-metadata policy
fingerprint, semantic-scope fingerprint, registry fingerprint, exact vocabulary fingerprint, and
its own canonical fingerprint. The managed provider policy fingerprint binds the provider
contract, scope, and registry; the surface adds the request-specific vocabulary. Neither
fingerprint is inferred or repaired from provider output. Provider configuration also binds
orchestration policy `m27-local-atomic-preflight-v2` and attempt policy
`m27-durable-attempts-v3`.

Invalid structured output uses the closed, payload-free
`m27-output-failure-taxonomy-v1`: `output_limit`, `schema_validation`, `grounding`, or
`semantic_contract`. The category may be persisted with sanitized usage facts, but never the
rejected payload. Timeout, rate limit, provider unavailability, refusal, and missing output remain
separate provider outcomes rather than being relabelled as semantic ambiguity.

`store=false` is not represented as Zero Data Retention. External AI is a versioned per-tenant
opt-in that defaults off. Production remains NO-GO until the operator has accepted the provider
retention/DPA/region boundary or enabled an appropriate approved project policy.

### Model selection gate

Use the cheapest configuration that passes the complete synthetic live evaluation, without
runtime fallback:

1. test `gpt-5-nano-2025-08-07`, the lowest published token price and a structured-output model
   suitable for classification;
2. if it misses a quality gate, test `gpt-5.4-nano-2026-03-17`, explicitly designed for
   classification, extraction, and ranking;
3. test `gpt-5.6-luna` at the lowest evaluated reasoning effort only if neither nano snapshot
   passes.

The winner, reasoning effort, SDK version, prompt/schema fingerprint, token usage, latency, and
calculated cost are recorded. A model/configuration change invalidates prior interpretation
confirmations and requires the same evaluation gate.

The live selection run stops after the first passing model and is capped across all candidates at
180 provider calls, 250,000 input tokens, 40,000 output/reasoning tokens, and EUR 1.00 calculated
from the recorded official rates. Hitting any bound returns `evaluation_budget_exhausted` and
cannot be reported as a quality failure. A provider/model-unavailable result is also recorded
separately from a model that ran and failed a threshold.

The selected terminal evidence is signed `m27-cheapest-first-campaign-v11`. Nano passed the
provider-free preflight, qualification, and complete 136-case synthetic corpus, so the configured
cheapest-first order correctly stopped without calling 5.4 Nano or Luna. The content-addressed
envelope is
`reports/m27-query-studio-live-history/campaign-signed-beef3391aef2ea2cedfdfbc6e065ce259f7143861ccc0470197c6edcb88ba30a.json`;
its logical report SHA-256 is
`e13a63fd2add281e567e3b4e4086ba3bb8510ec6e554608533a4c532215c0b7f`.

The campaign observed 16 interpretation attempts, 15,715 input tokens, 1,204
output/reasoning tokens, 30,016 ms around the complete intent adapter, and EUR `0.001394085`.
Complete-corpus metrics were top-1 `56/62` (`0.903226`), top-3 and recall@20 `62/62`, MRR
`0.946237`, no-match specificity `31/31`, ambiguity recall `18/18`, exact typed core success
`15/15`, and adversarial handling `10/10`. A separately HMAC-authenticated schema-v2 attestation,
`campaign-ledger-attestation-f4fceb557b8dd0a2ddf3c05a5f670ba2ea759f32273ef515e0daf0231a117053.json`,
proves one unique chronological matching between the 16 signed attempts and the 16 durable
reservation/audit pairs. Policy v84 disabled external AI after the campaign.

The historical v4–v10 campaign records remain immutable and non-resumable. The v11 evidence uses
the original critical requests plus two frozen synthetic holdouts per core case. Its attestation
is an authenticated unique ordinal correlation, not a native content-derived case/request
identity; a future campaign should persist one shared sanitized run/case/repetition/ordinal digest
on both evidence surfaces.

Official model facts are taken from:

- `https://developers.openai.com/api/docs/models/gpt-5-nano`
- `https://developers.openai.com/api/docs/models/gpt-5.4-nano`
- `https://developers.openai.com/api/docs/models/gpt-5.6-luna`
- `https://developers.openai.com/api/docs/guides/structured-outputs`
- `https://platform.openai.com/docs/models/default-usage-policies-by-endpoint`

## Durable control and least privilege

The configured control-plane schema is v8 and preserves one additive, immutable history:

- `0006_dynamic_query_studio.sql` adds governed search, tenant AI policy, admission, and audit;
- `0007_harden_ai_usage_settlement.sql` validates and hardens successful usage settlement;
- `0008_serialize_ai_provider_accounting.sql` validates audit derivation and serializes provider
  accounting behind the reviewed lock order while reasserting exact ACLs.

Migrations 0001–0008 remain byte-for-byte unchanged once recorded; incompatible v6/v7 history
fails closed instead of being repaired during upgrade. The resulting schema supplies:

- a fixed-`search_path`, `SECURITY DEFINER`, tenant/scope/head-qualified governed field-search
  function with bounded input/output and keyset continuation;
- versioned external-AI opt-in and request/token/concurrency limits with immutable revisions;
- atomic reservation/settlement records for provider attempts, idempotency, and cross-replica
  admission;
- minimal append-only model-use audit: pseudonymous scope/actor digest, request ID, model/config
  fingerprint, semantic fingerprints, input/output token counts, duration, outcome code, and time;
- no prompt, provider output, field definitions, candidate list, source values, SQL, credentials,
  or raw identities in durable state;
- immutable guards, retention bounds, compound indexes, and revoked `PUBLIC`.

Each expansion/interpretation attempt has its own idempotent reservation, bounded token estimate,
database-time lease, stage, attempt number, and fencing token. Admission atomically checks
workspace+actor request window, daily token budget, and workspace concurrency. Successful
settlement charges observed usage; missing usage, timeout, refusal, invalid output, or crashed
expiry charges the conservative reservation and releases concurrency. A fenced expiry function
closes stale reservations, so a process crash cannot block quota indefinitely or restore budget.
One transient retry per stage is allowed, for at most four provider attempts in a complete request.

Only the migrator may apply/revise tenant AI policy through an inspect/prepare/apply CLI with
expected version, authenticated operator, exact confirmation, and immutable revision/audit. Direct
ad-hoc policy writes are not an acceptance path. The managed Streamlit/runtime role may execute
the bounded governed-search and reservation/settlement functions; it receives no direct table
grant. API, worker, catalog indexer, semantic reconciler, and semantic profile worker receive no
OpenAI key and no new provider capability. M27 adds no Query Studio endpoint to the M24 API:
authenticated Streamlit/runtime invokes application ports server-side for this vertical slice.

Managed provider configuration fixes an approved official HTTPS regional endpoint, TLS
verification, no redirects, and an allowlisted snapshot. The endpoint region participates in the
policy/config fingerprint. Managed execution rejects arbitrary proxy environment and caller
override; a caller cannot select a base URL, redirect, proxy, model, tool, or arbitrary prompt.
Local synthetic acceptance may explicitly opt one test tenant into external AI.

Usage attempts and immutable audit facts carry an explicit `retain_until`. M27 does not delete
accepted audit rows. M29 owns exported archival and a separately approved partition-retention
operation; temporary rate windows and expired fenced reservations may be removed only by the
migrator's bounded reviewed maintenance function.

## Query Studio UI

Replace the demo-only Query Studio path with two synchronized modes:

- **Guided:** paginated governed field/table search, dynamic controls by field type/role, approved
  join reachability, and clear non-executable discovery states.
- **Natural language:** short request, bounded match status, candidate evidence/risks, alternatives
  when present, editable typed interpretation, and an explicit `Confirm interpretation` action.

Before execution, show exact selected logical fields, connection, physical assets, mapping
versions, approved join path, cardinality/fanout mitigation, registry/catalog freshness, generated
SQL labelled deterministic inspection output, AST policy checks, row/timeout limits, and current
M26 gate result. Execution remains a separate approved action.

All catalog/model text is rendered as text, never unsafe HTML. Unicode, bidirectional controls,
spreadsheet-formula prefixes, long identifiers, empty/loading/error/stale/rate/provider-down
states, and mobile overflow are handled without deception or protected-data leakage.

The UI never displays parameter values; it shows only parameter count and safe logical types.
Editing the typed interpretation invalidates the preview signature and requires a new preview.
Selections are keyed by opaque candidate ID and preserved across keyset pages/mode switches only
while their signed scope remains current. Bidirectional/control characters are escaped or replaced
with a visible marker. Before a live call the UI states exactly which categories leave the system
(business text plus the bounded public governed shortlist) and keeps guided mode available.

## Evaluation corpus

Expand the synthetic M21/M25 corpus labels without adding employer or real data:

- all 31 approved mappings with Spanish and English paraphrases and slight field descriptions;
- at least the same number of true negatives;
- homonymous customer/order/product IDs, support-schema decoys, hidden synonyms, Unicode and nested
  paths, type drift, leading-zero identifiers, and equal names in different connections/tenants;
- all five governed query cases, including a three-table/two-join revenue case;
- two frozen synthetic holdout paraphrases per governed core case in selected campaign v11, while
  retained historical campaign corpus bytes and digests remain unchanged;
- direct and metadata-borne prompt injection, SQL/DDL text, invented IDs/joins, output extras,
  cross-tenant/cursor replay, provider failures, and state drift.

Deterministic retrieval must achieve 100% recall@20 on the positive executable fixture, with no
ungoverned field marked executable. Live model evaluation runs at least three repetitions per
core/critical case and requires:

- top-1 accuracy at least 85%, top-3 recall 100%, MRR at least 0.90, and deterministic stable order
  with a versioned integer scoring formula;
- 100% no-match specificity and 100% ambiguity recall on the critical negative/homonym set;
- 100% exact typed success on the core query cases;
- 100% ambiguity recall on the critical homonym/cross-connection set;
- zero invalid option indexes or server-side candidates outside the exact scoped shortlist;
- zero cross-tenant results;
- zero SQL/tool/approval/execution output;
- zero automatic semantic approvals.

Report top-1/top-3/recall@20, false positives/negatives, ambiguity recall, tokens, latency, and
calculated cost separately for deterministic fake and live provider runs. A live failure is
reported, never mixed with or replaced by fake evidence.

## Acceptance criteria

- [x] `plans/M27_DYNAMIC_QUERY_STUDIO.md` and its implementation prompt are reviewed before source
      code changes.
- [x] Pure Query Studio domain types are bounded, immutable, provider-free, and forbid executable
      or approval fields in model output.
- [x] The v16 provider schemas expose only exact `slot_id`/`query` expansion items and complete
      `slot_id`/`option_index=1` interpretation selections plus ambiguity. The server derives
      source spans and filters and reconstructs purpose, role/type, operations, grain, ordering,
      limit, primary candidate, selection counts, and final proposal.
- [x] A model can select only option `1` for the exact supplied slot; the server maps it to the
      scoped opaque candidate. Unknown, duplicate, stale, out-of-scope, extra, tied, or partially
      covered choices fail closed or remain explicit ambiguity before planning.
- [x] A unique match remains evidence only; it becomes executable only inside a complete typed
      request that receives exact fingerprint-bound human confirmation.
- [x] `ambiguous`, `no_match`, `conflicting`, `stale`, and provider failure are typed, visible, and
      cannot reach planning.
- [x] Semantic result, operational failure, and closure-overflow enums are distinct; an operational
      failure is never presented as business ambiguity.
- [x] Physical discovery results without current approved evidence remain
      `needs_mapping_review` and cause zero planner/compiler/source/DataHub-write calls.
- [x] A valid confirmation yields only the existing `ValidatedAnalyticalRequest`; the M26 gate
      still runs before deterministic compilation/source I/O.
- [x] One connection, three-table, two-join, fanout, result-limit, timeout, read-only, and AST-guard
      policies remain unchanged over a 5,434+ table inventory.
- [x] Governed browse/search is workspace/catalog/registry/head/generation scoped and uses a
      bounded indexed keyset with at most 50 rows plus one continuation.
- [x] No Query Studio request uses deep `OFFSET`, materializes the complete physical inventory, or
      sends the full registry/catalog to a model.
- [x] Governed executable search and ungoverned physical discovery use different ports, result
      types, cursor bindings, and UI labels; neither can be substituted for the other.
- [x] The executable shortlist is at most 20 candidates and the complete prompt closure is at most
      three models, twelve fields, and two joins; overflow is `closure_overflow`, not ambiguity or
      truncation.
- [x] Prompt input is at most 32 KiB, user text at most 2,000 characters/8 KiB, individual model
      definitions at most 300 characters, and model output is independently bounded.
- [x] PostgreSQL plans use the reviewed field-search and exact-locator indexes at 41,028 fields;
      cold execution remains below the existing statement timeout and recorded local budget.
- [x] Exact 10-table/75-field and 5,434-table/41,028-field tenants pass traversal/search without
      duplicate, omission, order drift, cross-scope rows, or catalog-sized Python memory.
- [x] Equal names across tenants/connections never collide, and a cross-scope candidate/cursor
      replay fails before provider or planner I/O.
- [x] Preview tokens are HMAC-authenticated, scope-bound, expire within ten minutes, contain no
      text/candidates/raw identities, and confirmation exactly recomputes retrieval before use.
- [x] The interpretation fingerprint covers text, shortlist, registry/pointer/head/catalog state,
      model snapshot, prompt/schema, matcher version, and typed proposal.
- [x] Registry pointer, mapping/join approval, M26 head, catalog generation, shortlist, model, or
      prompt change between preview and confirmation returns stale with zero compile/source I/O.
- [x] Homonyms and similar descriptions show evidence and alternatives when present; no
      name/description/model score creates a mapping or join approval.
- [x] Guided models, fields, controls, allowed values, and joins derive from current governed
      context with no Customer/AccountHolder or other domain branch.
- [x] Spanish/English short descriptions match the labelled governed corpus through bounded
      expansion plus deterministic retrieval; retrieval failure cannot be repaired by hallucination.
- [x] Sensitive/secret-like user input blocks model egress without silently changing the request,
      and guided/manual mode remains available.
- [x] Fake/CI mode is deterministic and key-free; live mode requires explicit tenant opt-in, key,
      and allowlisted snapshot and never silently falls back.
- [x] Live Responses requests use strict structured output, `store=false`, no tools, bounded output,
      timeout/retry, evaluated reasoning effort, and a privacy-preserving safety identifier.
- [x] Interpretation requires an exact `ApprovedPublicMetadataSurface`; missing, restricted, stale,
      cross-scope, cross-registry, or vocabulary-mismatched metadata fails before provider I/O.
- [x] Provider-output rejection uses only the payload-free
      `m27-output-failure-taxonomy-v1`; partial/mixed slot output is `semantic_contract`, not
      `ambiguous` or `conflicting`.
- [x] The existing key remains only in environment/secret-manager configuration and is absent from
      source, docs, fixtures, state, logs, browser responses, process arguments, and artifacts.
- [x] API, worker, catalog, reconciler, and profile-worker environments contain no OpenAI key or
      provider capability.
- [x] Managed endpoint/model configuration rejects caller-selected base URLs, redirects, proxies,
      credentials, query fragments, models, tools, and prompts.
- [x] Durable provider audit contains only sanitized usage/config/semantic facts; it contains no
      prompt, response, definitions, candidates, rows, values, SQL, parameters, or raw identity.
- [x] External AI is versioned tenant opt-in and defaults off; documentation does not claim that
      `store=false` alone provides Zero Data Retention.
- [x] AI request/concurrency/token limits are tenant-scoped, atomic across replicas, idempotent,
      conservatively charge attempted calls, and return bounded rate/quota errors.
- [x] Attempt reservations are stage/attempt scoped, leased and fenced; settlement or expiry
      releases concurrency exactly once and conservatively charges unknown usage after a crash.
- [x] Provider timeout, 429/5xx, refusal, missing output, and invalid structured output are
      sanitized, consume the appropriate reservation, and cause no semantic confirmation or I/O.
- [x] The cheapest model snapshot passing every live gate is selected and pinned; nano candidates
      are evaluated before Luna and there is no runtime model cascade.
- [x] The expanded corpus gives deterministic recall@20 of 100% for executable positives, zero
      ungoverned executable results, and exposes all false positives/negatives.
- [x] Deterministic retrieval also meets top-1 >=85%, top-3=100%, MRR >=0.90, critical no-match
      specificity=100%, ambiguity recall=100%, stable ordering, and documented tie behavior.
- [x] Live evaluation runs at least three repetitions per critical case and records exact quality,
      ambiguity, safety, token, latency, and calculated-cost evidence separately from fake results.
- [x] Equivalent guided and natural-language inputs produce byte-identical validated request and
      resolved-plan fingerprints across all five governed query cases.
- [x] Internal-browser desktop and 390x844 acceptance covers guided pagination, short-description
      match, ambiguity, no-match, stale confirmation, rate/quota, provider-down, deterministic SQL
      inspection, and separately approved execution.
- [x] Browser acceptance has a clean console, no horizontal overflow/XSS/UI deception, and zero
      key, prompt, provider payload, protected identity, credential, SQL parameter, or source-value
      leakage.
- [x] Browser UI hides parameter values, invalidates edited interpretations, safely marks bidi/
      control text, preserves only current signed selections, and discloses the bounded egress
      categories before live AI use.
- [x] The live provider smoke uses only the synthetic corpus and existing environment key; it
      records no secret and performs no source/DataHub mutation.
- [x] Schema v5→v8, populated v6→v8, fail-closed v6/v7 history preflights, pristine migration,
      six-role least privilege, runtime package, and no-auto-migration gates pass on final bytes.
- [x] Dynamic-cardinality tests cover 0, 1, 10, 5,434, and configured-policy-limit assets across
      multiple connections, while clearly separating 31 governed mappings from 41,028 physical
      fields and containing no size-specific code branch.
- [x] Tenant AI policy is applied only through the versioned exact-confirmation migrator CLI; audit
      retention remains append-only in M27 and later archival is not misrepresented as implemented.
- [x] Focused, integration, acceptance, evaluation, package, release-audit, `make check`, coverage
      at or above 80%, `git diff --check`, and internal-browser gates pass with exact evidence.

## Implementation sequence

1. Add pure bounded domain values, fingerprints, deterministic ranking, and adversarial tests.
2. Add schema v6 governed search, AI opt-in/admission/audit, grants, and PostgreSQL tests; preserve
   additive schema v7 settlement hardening bytes and apply additive schema v8 clock, lock-order,
   audit-derivation, and ACL hardening without rewriting incompatible history.
3. Implement registry/catalog/head-aware search and keyset cursor adapters with scale plans.
4. Implement deterministic fake expansion/intent and dynamic vocabulary/confirmation use cases.
5. Harden the OpenAI Responses adapters, model allowlist, privacy filtering, and usage settlement.
6. Refactor guided and natural-language entry paths to remove demo-domain hardcoding.
7. Build the dynamic Query Studio view over application use cases and existing governed workflow.
8. Expand evaluation fixtures/reports and run nano-first live model selection.
9. Run real PostgreSQL, source, process, package, browser, quality, coverage, and security gates.
10. Consolidate documentation, exact evidence, decisions, and known limitations before acceptance.

## Required automated checks

```bash
.venv/bin/pytest -q tests/unit/test_query_studio.py \
  tests/unit/test_query_studio_matching.py \
  tests/unit/test_query_studio_config.py \
  tests/unit/test_query_studio_schema_migration.py \
  tests/unit/test_query_studio_settlement_hardening_migration.py \
  tests/unit/test_query_studio_dynamic_cardinality.py \
  tests/unit/test_query_studio_guided.py \
  tests/unit/test_query_studio_physical_discovery.py \
  tests/unit/test_query_studio_ai_admission.py \
  tests/unit/test_query_studio_ai_policy.py \
  tests/unit/test_query_studio_evaluation.py \
  tests/unit/test_query_studio_corpus.py \
  tests/unit/test_query_studio_acceptance_scenarios.py \
  tests/unit/test_query_studio_bootstrap.py \
  tests/unit/test_query_studio_recorded.py \
  tests/unit/test_query_studio_fake_language.py \
  tests/unit/test_query_studio_live_evaluation.py \
  tests/unit/test_query_studio_security.py \
  tests/unit/test_postgres_query_studio_adapters.py \
  tests/unit/test_openai_boundary.py \
  tests/unit/test_openai_query_studio_v15.py \
  tests/unit/test_openai_query_studio_v15_security_regressions.py \
  tests/unit/test_m27_browser_acceptance_runtime.py \
  tests/unit/test_m27_browser_scenario_runtime.py
make control-plane-reset
make control-plane-migrate
make control-plane-check
.venv/bin/pytest -q -m integration \
  tests/integration/test_query_studio_postgres.py \
  tests/integration/test_query_studio_v5_upgrade_postgres.py \
  tests/integration/test_query_studio_scale_plans_postgres.py \
  tests/integration/test_query_studio_browser_corpus_postgres.py \
  tests/integration/test_catalog_scale_postgres.py
.venv/bin/pytest -q -m acceptance \
  tests/acceptance/test_query_studio_equivalence_acceptance.py \
  tests/acceptance/test_query_studio_streamlit_acceptance.py \
  tests/acceptance/test_query_studio_scenario_streamlit_acceptance.py
make test-api-integration
make test-worker-integration
make test-integration
make test-acceptance
make evaluate
make runtime-wheel-smoke
python scripts/release_audit.py
make check
make coverage
git diff --check
```

The exact files may be split during implementation, but every listed behavior and full gate remains
required.

## Manual internal-browser test

1. Migrate a clean control plane through v8, load the 10-table and 5,434-table synthetic tenants, and
   start authenticated Streamlit/runtime with external AI enabled only for the synthetic test
   tenant.
2. In guided mode, search/browse beginning, middle, and final pages at desktop and 390x844. Verify
   dynamic table/field counts, exact connection/generation, bounded continuation, and no hardcoded
   domain list.
3. Search a slight Spanish description such as `fecha en la que se registró el cliente`, inspect
   the unique match and any alternatives as evidence, then include it in a complete typed request;
   do not treat the field match itself as an approval.
4. Submit the north-star request and each additional governed query case. Resolve any displayed
   ambiguity if present, confirm the complete typed request, inspect approved mapping/join/fanout
   and deterministic SQL validation, then use the separate execution approval.
5. Exercise a support-schema homonym, equal field names across connections, no-match, prompt
   injection, stale catalog/registry state, exhausted AI quota, and provider outage. Verify they
   stop before compile/source I/O with sanitized UI.
6. Verify desktop/mobile widths, loading/empty/error states, escaped hostile metadata, clean
   console/network responses, and absence of protected key/prompt/provider/source/identity data.
7. Run the synthetic live model smoke, record the chosen snapshot and exact quality/token/latency/
   cost result, then disable external AI and clean only the dedicated acceptance state.

Operated local result:

- the fake/runtime internal-browser matrix passed at desktop and 390x844 over both the
  10-asset/75-field and 5,434-asset/41,028-field profiles, including all safe-state, deterministic
  SQL/AST/read-only preview, console, overflow, hostile-text, and protected-data checks above;
- the signed v11 synthetic provider campaign selected `gpt-5-nano-2025-08-07` with the exact
  quality/usage/cost facts recorded above and no source or DataHub mutation;
- the one separately authorized live-UI reload was blocked by the internal browser's URL policy
  before the page, form, click, submission, or provider boundary. The block was not bypassed;
- policy v86 was applied disabled immediately afterward, and the historical v2 attestation
  verifier still returned `verified=true`.

The URL-policy block is a limitation of the attempted live-UI smoke, not provider evidence and
not a product success. It does not invalidate the separately completed fake browser matrix or the
signed live synthetic campaign.

The final automated matrix also passes: the exact focused plan has 552 tests; schema v8 is applied
through migrations 1–8 and all six roles report current/expected v8 with no pending migration and
verified source/control separation; the M27 integration selection passed 20 while the absent small
fixture skipped one, then regenerating the exact small 10/75 fixture made that case pass 1/1;
focused acceptance has 15 passes; API/worker cuts have 18/24 passes; final full integration has
148 passes; full acceptance has 35 passes; deterministic evaluation, wheel smoke, release audit,
Ruff/format/mypy, 2,281-test `make check`, 2,459-test coverage at 81.47%, postflight scale plans,
and `git diff --check` pass. The release audit's dirty-tree warning remains explicit and means
this is not clean-room release evidence. Exact commands, timings, the one corrected test-only clock
fixture, and results are recorded in `tasks/M27_HANDOFF.md`.

## Completion contract

Every M27 criterion is now checked and exact automated/browser evidence is recorded in
`tasks/M27_HANDOFF.md`; examples/docs and durable task/decision state match the final behavior.
M27 is accepted locally. This remains synthetic local evidence, not production/release approval;
M28–M31 and the dirty release tree remain blockers.
