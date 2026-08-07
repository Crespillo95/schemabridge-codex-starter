# M27 implementation prompt

Implement `plans/M27_DYNAMIC_QUERY_STUDIO.md` as one vertical slice.

Preserve every repository invariant. In particular:

- derive guided selectors and natural-language vocabulary from the authenticated tenant's active
  catalog, governed registry, and current M26 evidence; remove demo-domain hardcoding;
- support 10, 5,434, or more catalog tables through indexed keyset pagination without loading the
  complete inventory into Python, the browser, or a model prompt;
- keep executable requests to one governed connection, three tables, two approved joins, fanout
  policy, result/timeout bounds, deterministic compilation, independent AST validation, and
  read-only source access;
- treat names, descriptions, types, tags, terms, scores, and model output as evidence only;
- keep a unique short-description match as evidence only; make it executable only after it is
  included in a complete typed request and that request receives exact fingerprint-bound human
  confirmation;
- create a `GuidedRequestDraft/Preview`; do not let the existing guided builder yield
  `ValidatedAnalyticalRequest` until the signed confirmation use case invokes it;
- keep governed executable retrieval and ungoverned physical discovery as distinct ports, results,
  cursors, and UI lanes;
- enrich exact PostgreSQL binding results from the immutable active registry port instead of
  introducing a second logical-registry authority in the v6 projection retained by schema v8;
- authenticate an expiring text-free preview token and recompute the exact registry/head/catalog
  shortlist on confirmation; never trust `session_state` or browser candidate state as authority;
- keep ungoverned physical discovery `needs_mapping_review` and outside planning/execution;
- derive the exact bounded field-slot inventory and all purposes, roles, canonical types, metric
  operations, filter operators, date grains, selection counts, ordering, limit, and primary
  candidate server-side;
- constrain expansion output to exact `slot_id` and grounded atomic `query`; derive and validate
  the unique `source_span` server-side;
- expose interpretation option `1` only for a strict compatible top-1 candidate, return only
  complete `slot_id`/`option_index=1` pairs plus bounded `ambiguity_kinds`, and map the option and
  derive any governed filter value server-side; a tied top score is explicit ambiguity before
  provider interpretation;
- reconstruct the final typed proposal server-side. Reject duplicate/unknown slots, an option
  other than `1`, partial slot coverage, or selections mixed with ambiguity as payload-free
  `semantic_contract` invalid output, never as business `conflicting`;
- reconstruct empty selections plus ambiguity as `ambiguous`, and empty selections without
  ambiguity as `no_match`;
- never accept model-authored roles, types, operations, joins, ordering, limits, semantic approval,
  SQL, physical identifiers, tools, or execution actions;
- require `ApprovedPublicMetadataSurface` before interpretation, bound to the managed policy,
  semantic scope, active registry, exact vocabulary, and its own canonical fingerprint; block
  missing, restricted, stale, cross-registry, or mismatched surfaces before provider I/O;
- pin public-metadata policy `m27-approved-public-definition-surface-v1`, orchestration policy
  `m27-local-atomic-preflight-v2`, and attempt policy `m27-durable-attempts-v3` into the relevant
  provider/configuration fingerprints;
- pin the reviewed v16 prompt/output contract to `m27-openai-prompts-v14`,
  `m27-query-studio-v8`, expansion contract `m27-expansion-contract-v7`, slot-selection contract
  `m27-slot-selection-v2`, and proposal normalizer `m27-proposal-defaults-v3`; keep output failures
  in the closed payload-free
  `m27-output-failure-taxonomy-v1` (`output_limit`, `schema_validation`, `grounding`,
  `semantic_contract`);
- reload registry, pointer, M26 head, catalog generations, shortlist, model, and prompt/schema state
  before confirmation;
- use the existing environment key without printing, copying, persisting, logging, or sending it
  to the browser;
- keep fake/CI mode key-free and live mode explicit with no silent fallback;
- evaluate `gpt-5-nano-2025-08-07` first, then `gpt-5.4-nano-2026-03-17` only if needed, and
  `gpt-5.6-luna` only if both nano candidates fail; pin the cheapest passing configuration;
- use structured Responses output, `store=false`, no tools, bounded tokens/timeout/retry, safe
  identifier, tenant AI opt-in, atomic quota/cost admission, and sanitized audit;
- screen obvious PII/credential/high-entropy secret-like user text before provider egress, block
  visibly rather than changing it silently, and keep guided/manual mode available;
- reserve each provider stage/attempt atomically with lease/fencing, settle observed usage or
  conservatively charge unknown usage, and expire crashed attempts exactly once;
- never claim `store=false` is Zero Data Retention;
- persist no prompts, model responses, candidate definitions, source rows/values, SQL, parameters,
  DSNs, credentials, raw identities, claims, tokens, or private reasoning;
- leave M24 API/worker, catalog indexer, semantic reconciler, and profile worker without OpenAI
  capability;
- keep M27 Query Studio server-side in authenticated Streamlit/runtime; do not add an optional M24
  API provider route;
- require configured control-plane schema v8; preserve migrations
  `0006_dynamic_query_studio.sql`, `0007_harden_ai_usage_settlement.sql`, and
  `0008_serialize_ai_provider_accounting.sql` as immutable additive history and fail closed on
  incompatible v6/v7 data;
- hide SQL parameter values in the UI and invalidate confirmation after any interpretation edit;
- cap live model selection at 180 calls, 250,000 input tokens, 40,000 output/reasoning tokens, and
  EUR 1.00, stopping after the first model that passes every quality/safety gate;
- preserve terminal signed campaign v6 as immutable superseded-contract history, keep policy v64
  disabled with no selected model, and start only a fresh `m27-cheapest-first-campaign-v7`;
- bind current v7 to two frozen synthetic holdout paraphrases for each of the five core cases
  without changing the retained v4–v6 corpus digest; cap any two-attempt stage at 79,576 input and
  8,192 output/reasoning tokens and EUR 0.141600800;
- run real PostgreSQL scale, source safety, package, quality, coverage, evaluation, and internal
  browser desktop/mobile acceptance on final bytes;
- keep local evidence distinct from production/release approval.

Write tests alongside behavior. The focused matrix must reference the existing
`tests/unit/test_openai_query_studio_v15.py`,
`tests/unit/test_openai_query_studio_v15_security_regressions.py`,
`tests/unit/test_query_studio_schema_migration.py`,
`tests/unit/test_query_studio_settlement_hardening_migration.py`,
`tests/unit/test_query_studio_dynamic_cardinality.py`,
`tests/integration/test_query_studio_v5_upgrade_postgres.py`,
`tests/integration/test_query_studio_scale_plans_postgres.py`,
`tests/acceptance/test_query_studio_equivalence_acceptance.py`,
`tests/acceptance/test_query_studio_streamlit_acceptance.py`, and
`tests/acceptance/test_query_studio_scenario_streamlit_acceptance.py`; the complete matrix remains
the one in the plan.

Do not rewrite migrations 0001–0008, weaken assertions, add a raw SQL path, infer an approval, raise
query-width limits, silently truncate a candidate closure, or claim production readiness. Record
every command, failure/correction, provider model/configuration, token/latency/cost fact, and exact
result in the M27 handoff.
