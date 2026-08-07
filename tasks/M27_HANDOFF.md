# Milestone handoff

## Summary

- Milestone: M27 — Dynamic Query Studio and governed description matching
- Status: complete and accepted locally on 2026-07-27
- Recommended operator decision: accept M27 locally; M28 is eligible but has not started
- Proposed commit message: `feat: complete governed dynamic Query Studio`

M27 now supports dynamic 10-table and 5,434-table tenant inventories, bounded short-description
matching, registry-driven guided requests, signed interpretation confirmation, and natural
language → typed request → deterministic SQL through the existing M26 gate. The signed live
synthetic campaign selected `gpt-5-nano-2025-08-07`; no runtime model cascade exists.

The fake/runtime product browser matrix passed at desktop and 390x844. The separately authorized
single live-UI attempt did not reach the application or provider: the internal browser blocked
`http://127.0.0.1:8510` under its URL policy before page load/form/click/submission. That control
was not bypassed. Policy v86 is current and disabled, and a read-only repeatable-read ledger check
proved zero policy-v85 provider activity.

This is local synthetic evidence, not a production or release GO. M28–M31, provider
DPA/retention/region approval, operated infrastructure, and a clean reviewed release remain open.

All 50 M27 criteria and the final focused/schema/service/evaluation/package/release/quality/
coverage/scale/diff matrix pass. The release audit still reports the expected dirty-tree warning,
so this handoff is not clean-room release evidence.

## Implemented

- Governed executable matching and physical discovery use separate ports, cursors, result types,
  and UI lanes. Physical-only fields remain `needs_mapping_review` and cannot reach planning,
  compilation, source execution, or DataHub mutation.
- Guided controls, fields, allowed values, and join reachability derive from the active registry
  and current M26 evidence rather than Customer/AccountHolder-specific branches.
- Spanish and English descriptions produce bounded server-owned slots and deterministic retrieval
  over at most 20 candidates. The complete model-visible closure is at most three models, twelve
  fields, and two approved joins.
- Analytical expansion is local and provider-free. The optional model may return only complete
  `slot_id`/`option_index=1` selections plus closed ambiguity kinds. SchemaBridge owns grounding,
  candidate identity, values, roles/types, operations, grains, primary selection, joins, ordering,
  limits, SQL, and approval.
- Opaque candidate IDs and HMAC preview tokens bind the exact scope, registry, pointer, M26 head,
  catalog generations, matcher, provider configuration, shortlist, proposal, and expiry. Editing
  invalidates the signature; confirmation recomputes current state before yielding only the
  existing `ValidatedAnalyticalRequest`.
- Dynamic cardinality is data, not a code branch. The retained profiles cover 10 assets/75 fields
  and 5,434 assets/41,028 fields while exposing exactly 31 approved mappings. Executable requests
  still permit one connection, at most three tables, and two joins.
- Migrations 0006–0008 add governed keyset search, versioned tenant AI policy, atomic
  reservation/admission/settlement/audit, successful token bounds, serialized accounting lock
  order, deterministic audit derivation, and exact least-privilege wrappers.
- Live provider use defaults off, requires explicit tenant policy and exact configuration, reads
  the existing key only from process environment, uses strict structured output with
  `store=false`, no tools, bounded timeout/output, one governed transient retry, and never falls
  back to fake while labelled live.
- Signed `m27-cheapest-first-campaign-v11` selected
  `gpt-5-nano-2025-08-07`. All 136 planned synthetic cases were observed: positive retrieval
  `62/62`, negatives `31/31`, ambiguity `18/18`, exact typed core `15/15`, and adversarial
  `10/10`.
- Complete-corpus quality was top-1 `56/62` (`0.903226`), top-3 and recall@20 `62/62`, MRR
  `0.946237`, no-match specificity `1.0`, ambiguity recall `1.0`, and exact core success `1.0`.
  Usage was 16 interpretation attempts, 15,715 input tokens, 1,204 output/reasoning tokens,
  30,016 ms around the complete intent adapter, and EUR `0.001394085`.
- The accepted schema-v2 campaign/ledger attestation proves one unique chronological matching
  between all 16 signed attempts and all 16 durable policy-v83 reservation/audit pairs, including
  exact terminal and aggregate usage facts. It retains digests/aggregates only.
- The attestation explicitly claims authenticated ordinal correlation, not native
  content-derived case/request identity. Historical campaign bytes lacked one shared nonce-bound
  case/request digest; future evidence should persist a common sanitized
  run/case/repetition/ordinal digest.
- The fake/runtime internal-browser journey passed desktop and 390x844 coverage for guided
  pagination, short matching, all five governed cases, edit/re-sign, ambiguity, no-match,
  conflict, stale, sensitive input, provider unavailable, rate limit, quota exhaustion,
  `needs_mapping_review`, deterministic SQL/AST/read-only preview, hidden parameter values, safe
  hostile text, clean console, no horizontal overflow, and protected-data scans.
- The separately authorized live-UI reload was recorded as `blocked_before_provider`; policy v86
  was applied disabled immediately, zero v85 provider/control-accounting activity was proved, and
  the historical schema-v2 attestation still verifies.

## Files changed

- `src/schemabridge/domain/query_studio.py`: bounded immutable Query Studio values, semantic and
  operational states, scoring facts, opaque IDs, proposals, previews, and cardinality contracts.
- `src/schemabridge/application/query_studio.py`: governed/physical retrieval, local expansion,
  closure construction, natural/guided preview, edit/re-sign, exact confirmation, and planner
  handoff.
- `src/schemabridge/application/ports/query_studio.py`: distinct governed search, physical
  discovery, language, candidate-ID, and preview-token ports.
- `src/schemabridge/application/query_studio_ai_admission.py`,
  `src/schemabridge/application/query_studio_ai_policy.py`, and their ports: tenant policy,
  fenced attempt admission/accounting, and closed sanitized outcomes.
- `src/schemabridge/adapters/catalog/postgres_governed_search.py` and
  `src/schemabridge/adapters/catalog/postgres_physical_discovery.py`: scoped bounded PostgreSQL
  keyset adapters.
- `src/schemabridge/adapters/query_studio/`: recorded/fake retrieval, deterministic language, and
  HMAC/nonce adapters.
- `src/schemabridge/adapters/language/openai_boundary.py` and
  `src/schemabridge/adapters/language/openai_query_studio.py`: pinned managed Responses boundary
  and minimal typed interpretation.
- `src/schemabridge/adapters/control_plane/postgres_query_studio_ai.py` and
  `src/schemabridge/adapters/control_plane/postgres_query_studio_policy.py`: least-privilege
  PostgreSQL provider accounting and policy control.
- `src/schemabridge/adapters/evaluation/query_studio_live.py` and
  `src/schemabridge/adapters/evaluation/query_studio_live_attestation.py`: signed campaign,
  bounded historical evidence loading, unique ordered campaign/ledger correlation, and HMAC
  verification.
- `src/schemabridge/entrypoints/streamlit/query_studio.py`: dynamic guided/natural UI, cardinality,
  safe states, separate physical lane, hidden values, disclosure, and exact confirmation.
- `src/schemabridge/entrypoints/ai_policy/main.py`,
  `src/schemabridge/bootstrap.py`, and `src/schemabridge/config.py`: operator policy flow,
  composition, and fail-closed disabled/fake/live configuration.
- `migrations/control_plane/0006_dynamic_query_studio.sql`: schema-v6 governed search and external
  AI control; SHA-256
  `6319c7cb3409014ae4e96c9d99cc0ed2d7a5500baaad844ce1e904e476c5b535`.
- `migrations/control_plane/0007_harden_ai_usage_settlement.sql`: successful settlement bounds;
  SHA-256 `5dbc079a995b5fe0373ada20264c20d64c657259e10f77839f1f0805449fbcec`.
- `migrations/control_plane/0008_serialize_ai_provider_accounting.sql`: lock-order, lease-clock,
  audit-derivation, and ACL hardening; SHA-256
  `77b84a021f1b99a140236333b1f1f79a11893f771046fe14490886fc00b57708`.
- `demo/ground_truth/query_studio_matching.yml`: multilingual positive, negative, ambiguity,
  homonym, holdout, and adversarial synthetic corpus.
- `scripts/evaluate_query_studio_matching.py`, `scripts/evaluate_query_studio_live.py`, and
  `scripts/m27_browser_acceptance_runtime.py`: deterministic/live evaluation, policy, retained
  browser profiles, stale drills, attestation create/verify, and exact cleanup lifecycle.
- `tests/unit/test_query_studio*.py`, `tests/unit/test_openai*.py`,
  `tests/unit/test_postgres_query_studio_adapters.py`, M27 integration tests, and M27 acceptance
  tests: domain/application/provider/security/schema/scale/UI/equivalence/attestation coverage.
- `reports/m27-query-studio-live-history/campaign-signed-beef3391aef2ea2cedfdfbc6e065ce259f7143861ccc0470197c6edcb88ba30a.json`:
  content-addressed selected Nano campaign.
- `reports/m27-query-studio-live-history/campaign-ledger-attestation-f4fceb557b8dd0a2ddf3c05a5f670ba2ea759f32273ef515e0daf0231a117053.json`:
  accepted content-addressed schema-v2 ordered attestation.
- `docs/provider-governance/openai-global-m27.md`: reviewed provider boundary, campaign closure,
  attestation, one-smoke authorization, URL-policy block, zero-activity proof, and v86 closure.
- Product, architecture, security, test, runbook, browser, evaluation, deployment, plan, task,
  decision, and handoff documents: final M27 behavior, evidence, limitations, and production
  boundary.

## Commands executed

Exact commands retained in the available closure context:

| Command | Result | Notes |
|---|---|---|
| `.venv/bin/python scripts/m27_browser_acceptance_runtime.py verify-live-attestation --attestation-json reports/m27-query-studio-live-history/campaign-ledger-attestation-f4fceb557b8dd0a2ddf3c05a5f670ba2ea759f32273ef515e0daf0231a117053.json` | pass | Exit 0; `verified=true`, schema v2, 16 historical attempts, one compatible matching, expected campaign/ledger/witness/ordinal digests |
| Policy v85 exact apply through `scripts/m27_browser_acceptance_runtime.py ai-policy apply` | pass | Enabled at `2026-07-27T17:48:00.841467Z`; policy fingerprint `0f1bbbe4dd8f8f05824d96579d722f956d0a97cc02ec650b506169e5c735dc71` |
| Policy v86 exact disable through `scripts/m27_browser_acceptance_runtime.py ai-policy apply` | pass | Disabled at `2026-07-27T17:50:00.025348Z`; fingerprint `07f5537627c18f859e616c997c2c4769e533fd1c265292956781ce5f0db5ad7b` |
| PostgreSQL `REPEATABLE READ, READ ONLY` policy-v85/v86 ledger audit | pass | Zero v85 reservations, interpretations, settlements, audits, request-window changes, or daily-usage changes; post-v86 zero active/open/stale reservations and zero reserved tokens |
| Codex internal-browser fake/runtime matrix | pass | Desktop and 390x844; exact large 5,434/41,028/31 and small 10/75/31 states; all required UI/safety/leak checks passed |
| Codex internal-browser single live-UI reload | blocked before provider | Browser policy returned `URL is blocked by the Browser use URL policy` for `http://127.0.0.1:8510`; no page/form/click/submission/provider activity and no bypass |

Earlier confirmed focused evidence whose exact pytest argv is retained in the session transcript but
is unavailable in this compacted handoff context:

| Selection | Result | Notes |
|---|---|---|
| Pre-final combined provider-free M27 pytest selection | pass | 454 passed in 689.26s |
| Pre-final shared live-evaluation pytest selection | pass | 75 passed in 701.92s |
| Pre-final attestation/runtime pytest selection | pass | 84 passed |
| Independent final-review pytest selection | pass | 35 passed; reviewer reported P0=0, P1=0, P2=0 |
| Directed Ruff format/check, Ruff lint, mypy, and diff hygiene | pass | Focused attestation/runtime files clean |

Final acceptance commands:

| Command | Result | Notes |
|---|---|---|
| Focused pytest command with the exact M27 file list in `plans/M27_DYNAMIC_QUERY_STUDIO.md` | pass | 552 passed in 624.86s |
| `make control-plane-reset` | pass | Recreated only the dedicated synthetic control plane |
| `make control-plane-migrate` | pass | Applied immutable migrations 1–8; current schema v8 |
| `make control-plane-check` | pass | Runtime, reconciler, migrator, API, worker, and catalog all current/expected v8, no pending migration, source/control separation verified |
| `.venv/bin/pytest -q -m integration tests/integration/test_query_studio_postgres.py tests/integration/test_query_studio_v5_upgrade_postgres.py tests/integration/test_query_studio_scale_plans_postgres.py tests/integration/test_query_studio_browser_corpus_postgres.py tests/integration/test_catalog_scale_postgres.py` | pass with recovered fixture | Initial 20 passed/1 skipped because the small fixture was absent; regenerating exact 10/75 state made the skipped case pass 1/1 |
| `.venv/bin/pytest -q -m acceptance tests/acceptance/test_query_studio_equivalence_acceptance.py tests/acceptance/test_query_studio_streamlit_acceptance.py tests/acceptance/test_query_studio_scenario_streamlit_acceptance.py` | pass | 15 passed in 19.37s |
| `make test-api-integration` | pass | 18 passed in 31.69s |
| `make test-worker-integration` | pass | 24 passed in 39.15s |
| `make test-integration` | pass after test-fixture correction | First run: 147 passed/1 failed. Only the lease test fixture changed from `clock_timestamp()` to transaction-stable `statement_timestamp()`; production exact fencing stayed intact. Focused rerun 1/1; complete rerun 148 passed in 246.40s |
| `make test-acceptance` | pass | 35 passed in 114.62s |
| `make evaluate` | pass | Deterministic evaluation passed with `live_llm=not_run`; the signed v11 campaign remains the separate live evidence |
| `make runtime-wheel-smoke` | pass | Runtime package and packaged migrations passed |
| `.venv/bin/python scripts/release_audit.py` | pass with warning | Exit 0; 697 candidate files, 23 direct dependency licenses, zero external links; dirty-tree warning means no clean-room release evidence |
| `make check` | pass | Ruff format/lint, strict mypy, and 2,281 tests passed in 715.69s |
| `make coverage` | pass | 2,459 passed; 81.47% coverage in 2,115.40s |
| Postflight Query Studio scale-plan selection | pass | Reviewed 41,028-field indexes and cold execution budget passed |
| `git diff --check` | pass | Final whole-tree diff hygiene passed |
| Exact environment-key file/process scans | pass | Existing OpenAI key matched zero of 697 non-ignored files and zero process command lines; the key value was never printed |
| Dedicated M27 runtime cleanup plus cache pruning | pass | Dropped six synthetic acceptance databases and owner-only states; removed the temporary policy proposals, 849 MB local UV cache, 1.332 GB unused Docker images, and 3.703 GB Docker build cache; disk free space increased from 9.3 GiB to 19 GiB |

## Automated test results

- Focused tests: final exact-plan selection passed 552 in 624.86s. Earlier closure selections also
  passed 454 provider-free, 75 shared live-evaluation, 84 attestation/runtime, and 35
  independent-review tests.
- `make check`: passed Ruff format/lint, strict mypy, and 2,281 tests in 715.69s.
- Integration tests: final Query Studio selection passed its 20 available cases, then the
  regenerated exact small 10/75 fixture made the initially skipped case pass 1/1. Full integration
  passed 148 in 246.40s after correcting only the test fixture's transaction clock.
- Acceptance tests: focused M27 acceptance passed 15 in 19.37s; complete acceptance passed 35 in
  114.62s. The internal-browser fake/runtime product matrix passed. The separate live-UI attempt
  remained correctly classified as blocked before provider.
- API/worker: 18 passed in 31.69s and 24 passed in 39.15s.
- Coverage: 2,459 tests passed at 81.47% in 2,115.40s, above the 80% gate.
- Migration/package/release: schema v8, populated history, six-role matrix, wheel smoke, release
  audit, reviewed 41,028-field scale plans, and diff hygiene pass. Release audit exited 0 with the
  expected dirty-tree warning, so no clean-room release claim is made.
- Secret/cleanup closure: the exact existing provider key has zero repository/process-command
  matches. Dedicated synthetic acceptance state and only regenerable caches were removed; signed
  campaigns, attestations, source, reports, and the working virtual environment were retained.

## Operator manual test

1. Prepare the retained large and small M27 synthetic profiles and start the fake runtime.
2. In Codex's internal browser, run guided/natural journeys at desktop and 390x844. Exercise
   beginning/middle/final pages, short Spanish description matching, all five governed cases,
   edit/re-sign, ambiguity/no-match/conflict, stale catalog/registry, sensitive input,
   provider-down/rate/quota, hostile text, physical `needs_mapping_review`, deterministic SQL/AST
   inspection, and separately approved read-only execution.
3. Verify 5,434/41,028/31 and 10/75/31 server-observed cardinalities, hidden parameter values,
   clean console, no overflow, and zero protected-data/provider/key/source-value leakage.
4. Verify the selected signed Nano campaign and accepted v2 campaign/ledger attestation.
5. Review the exhausted one-submission live-UI authorization: it was blocked before provider and
   policy v86 was disabled. Do not repeat it without a new separately reviewed exact authorization,
   and never bypass a browser security policy.

Expected and observed result:

```text
fake/runtime desktop + 390x844 matrix: PASS
signed live synthetic campaign: SELECTED gpt-5-nano-2025-08-07
campaign/ledger attestation v2: verified=true; unique matching=1
single live-UI attempt: BLOCKED_BEFORE_PROVIDER by internal-browser URL policy
policy-v85 provider/accounting activity: 0
current tenant AI policy: v86 disabled
source/DataHub mutation from M27 provider work: 0
```

## Architecture and security review

- Dependency direction: Query Studio preserves entrypoint → application → domain; PostgreSQL,
  OpenAI, HMAC, and recorded implementations remain injected adapters. Focused review and the
  final whole-tree release audit pass.
- Source database writes: none. Physical discovery cannot execute; confirmed governed requests
  still traverse the M26 gate, deterministic compiler, independent AST guard, and bounded
  read-only source adapter.
- SQL/LLM validation: user/model SQL is never an input. Provider output is a strict minimal typed
  choice envelope; all executable semantics and SQL are server-owned and independently validated.
- DataHub mutation approval: M27 adds no automatic mapping, join, recipe, or DataHub write. The
  live campaign and browser work performed no DataHub mutation.
- Secrets/proprietary data: public data is synthetic. The existing provider key remained process
  environment only and was absent from source, docs, fixtures, state, argv, signed evidence,
  browser output, and logs. Provider/audit artifacts retain no prompt or response.
- Fanout/semantic risks: names, definitions, tags, terms, scores, and model output remain evidence,
  never approval. Ambiguity is explicit; exact human confirmation and current approved
  mapping/join/fanout policy remain mandatory.

## Decisions made

- Decision: keep physical discovery separate from governed executable retrieval and bind every
  interpretation to current state.
- Reason: catalog breadth and name/description similarity cannot establish semantic equivalence or
  query authority.
- Logged in: `tasks/DECISION_LOG.md` D092.

- Decision: keep analytical expansion local and provider-free; let the optional model select only
  one server-numbered strict leader per slot.
- Reason: server-owned semantic constraints and local ranking are deterministic authority, while
  provider repetition of grounding/IDs/values added instability without useful authority.
- Logged in: `tasks/DECISION_LOG.md` D095.

- Decision: accept the schema-v2 ordered attestation, classify the browser URL-policy refusal as
  pre-provider, do not bypass it, and disable external AI immediately as policy v86.
- Reason: v2 proves exact historical accounting without overclaiming content-derived identity;
  the URL-policy block occurred before application/provider activity and therefore must not be
  relabelled as provider or product success/failure.
- Logged in: `tasks/DECISION_LOG.md` D096.

- Decision: accept M27 locally after all 50 criteria and the final matrix passed while retaining
  the global production/release NO-GO.
- Reason: final schema, service, evaluation, package, release-audit, quality, 81.47% coverage,
  scale-plan, diff, signed-provider, attestation, and browser evidence now closes the local
  synthetic milestone; dirty-tree and later production controls remain real blockers.
- Logged in: `tasks/DECISION_LOG.md` D097.

## Known limitations or unverified items

- The internal browser's URL policy prevented the separately authorized live-UI submission before
  page load. No bypass was attempted. The signed live synthetic campaign independently validates
  model quality/accounting, while the fake/runtime browser matrix validates product presentation.
- The v2 attestation is an authenticated unique ordinal witness for the sequential historical
  campaign. It is not native content-derived case/request identity because the original campaign
  did not retain a shared nonce-bound digest.
- `store=false` is not Zero Data Retention. No production tenant may enable external AI without
  approved DPA/retention/region/legal/privacy and data-classification decisions.
- Local 10/5,434 asset fixtures and synthetic quality scores are regression evidence, not
  enterprise-scale SLO, real-metadata quality, or sustained-traffic proof.
- The repository remains dirty and has not been reviewed as a clean release candidate.
- M28 connector/credential routing, M29 operations/security/supply-chain controls, and the
  remaining M30–M31 pilot/release work are not implemented or accepted by M27.

## Blockers

- **Local M27 acceptance:** none; all 50 criteria and final gates pass.
- **Production/release GO:** complete M28–M31, provider governance, operated infrastructure and
  security controls, clean release identity, deployment/pilot evidence, and external sign-off.

## Next milestone readiness

- Dependencies satisfied: yes for starting M28; M28 is eligible but has not started.
- Recommended next prompt: review and explicitly start M28 connector routing when desired; do not
  interpret eligibility as production approval.
- Required operator prerequisites: keep all data synthetic for closure; keep external AI disabled
  at v86 unless a new separately reviewed exact authorization exists; preserve one
  connection/three-table/two-join/read-only limits; retain the dirty-tree and M28–M31 production
  blockers explicitly.
