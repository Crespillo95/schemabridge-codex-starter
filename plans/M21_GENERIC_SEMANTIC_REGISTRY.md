# M21: Generic governed semantic registry and diverse synthetic corpus

- Status: complete; awaiting operator review
- Timebox: 10 hours
- Recommended Codex: GPT-5.6 Sol — Extra High (xhigh)
- Dependencies: locally verified M20 identity/RBAC boundary; M10 planner/compiler/guard contracts

## Objective

Replace the split north-star planning fixtures with one atomic, versioned, governed semantic
registry that can contain many approved models, mappings, and join contracts while each executable
query remains bounded to three tables and two joins. Prove that the boundary is not overfit to the
Customer/AccountHolder example by expanding the deterministic PostgreSQL corpus with a separate
commerce/fulfillment domain and executing exact multi-scenario ground truth.

M17/M18 external release evidence remains pending. M21 does not claim live DataHub reconstruction;
that is M22.

## Scope assumptions

1. The checked-in registry is an explicitly labeled synthetic deployment bundle. It never silently
   replaces a requested live DataHub registry and contains no raw SQL, credentials, tokens, samples,
   or private data.
2. Registry capacity and query capacity are different controls: the registry may hold more than
   three models and two joins, while every request/plan retains the existing three-table/two-join
   maximum.
3. The current M11 natural-language vocabulary and public judge workflow remain deliberately
   Customer-focused until M27. M21 must not present that bounded parser as a dynamic registry-wide
   language agent.
4. All new database content is deterministic synthetic data. Existing north-star rows and exact
   `2, 1, 1` results remain unchanged.

## Deliverables

- Add an immutable `GovernedSemanticRegistrySnapshot` with format version, registry identity,
  registry version, catalog scope, explicit source/provenance, logical models, governed mappings,
  a registry-wide join collection, and a canonical full-snapshot fingerprint.
- Enforce cross-registry integrity: approved/current artifacts only, known logical fields,
  unambiguous physical-to-logical meaning, exact decision references, exact logical/physical join
  agreement, and join keys backed by the approved mapping and transformation.
- Add one scoped application port and a recorded manifest adapter with bounded file sizes,
  duplicate-key rejection, path-containment checks, exact SHA-256 verification, and no legacy
  fallback.
- Inject one registry boundary into guided request, intent, planner, workflow, executor, and UI
  composition. Derive rejected-source allowlists from approved registry join keys.
- Re-resolve from the current registry before execution and retry. A changed or revoked registry
  fails closed before SQL or PostgreSQL I/O and requires a new workflow confirmation.
- Remove Customer-specific assumptions from generic resolution and registry UI copy. Show registry
  identity, fingerprint, model/mapping/join counts, evidence, risks, versions, and decisions.
- Expand PostgreSQL from five to eleven tables and from four to eight schemas with six new
  commerce/fulfillment/support tables and 427 deterministic rows, without altering old rows.
- Add a tracked seed manifest and read-only verifier for exact row counts, schema/constraint hashes,
  per-table canonical data hashes, safety facts, and a global fingerprint.
- Add commerce ground truth covering string/integer key normalization, padded references, malformed
  values, homonyms, booleans, timestamps, decimals, mapped categorical values, duplicate fanout,
  one-to-many and many-to-one traversal, and a three-table query.
- Update DataHub ingestion/recorded catalog expectations for all eleven synthetic datasets without
  broadening any writer permission.

## Synthetic expansion

The expanded database adds:

- `commerce.products`: 40 products, four category codes, decimals, booleans, timestamps, and
  inactive records;
- `legacy.item_master`: 42 integer-key legacy product representations including two orphans;
- `sales.orders`: 60 orders across two months with heterogeneous status codes, nullable channels,
  regions, and decimal totals;
- `sales.order_lines`: 180 repeated product/order relationships with quantities, decimals, nullable
  discounts, fanout, and invalid/unmatched references;
- `fulfillment.shipments`: 75 rows with duplicate shipments, padded order references, mapped status
  synonyms, nullable dates, plus `NULL`, empty, negative, and malformed keys;
- `support.order_cases`: 30 case-local homonymous identifiers that must never become semantic
  equivalence through name similarity alone.

The total corpus is 465 deterministic rows across eleven tables and eight schemas.

## Implementation sequence

1. Define the registry domain snapshot, limits, fingerprint, integrity validators, scope, and port.
2. Build the manifest-backed recorded adapter and migrate the existing approved context into one
   atomic enterprise registry bundle.
3. Expand SQL schemas/seeds/grants, add the seed verifier/manifest, and prove repeatable resets.
4. Add commerce logical models, mappings, contracts, requests, expected rows, and rejection facts.
5. Migrate composition and derive executable slices/allowlists from the registry.
6. Add stale-registry revalidation before execution/retry and close fixed generic UI copy.
7. Run focused, integration, acceptance, evaluation, coverage, browser, and full quality gates.
8. Update state, decisions, architecture, security, runbooks, and handoff evidence.

## Acceptance criteria

- [x] One manifest-backed snapshot atomically supplies logical request and physical planning
      context; corrupt, oversized, duplicate-key, unlisted, checksum-mismatched, escaped, or
      unsupported files fail closed without fallback.
- [x] The default registry contains at least seven logical models, twenty mappings, and five join
      contracts; the registry accepts this while each query still rejects a fourth table or third
      join.
- [x] Every active mapping/join is approved, decision-bound, version-consistent, referentially
      complete, and fingerprinted. Orphans, conflicting meanings, stale summaries, and join-key
      mismatches are rejected before planning.
- [x] Existing north-star planning, SQL validation, `2, 1, 1` result, and three rejection codes are
      unchanged through the new registry.
- [x] A separate commerce query resolves only commerce/sales assets, uses two governed joins and
      three tables, executes as `schemabridge_reader` in a read-only 5000 ms transaction, and matches
      exact tracked ground truth.
- [x] A one-to-many commerce count visibly applies only the approved exact-key `COUNT DISTINCT`
      mitigation; unsafe downstream aggregates, ambiguity, many-to-many, and forged cross-domain
      joins fail before execution.
- [x] Rejected-source inspection accepts only keys derived from the exact registry snapshot and
      reports the new malformed/negative/empty/`NULL` values under the same bounds.
- [x] Replacing or revoking the registry between confirmation and execution yields a stable stale
      failure, performs no preview, and requires a new confirmation.
- [x] Two destructive-but-scoped demo resets reproduce all eleven row counts and the same tracked
      per-table/global hashes. Reader DML/DDL remains independently rejected.
- [x] DataHub ingestion and catalog checks cover eleven described/profiled synthetic datasets; MCP
      remains read-only and writer policies remain limited to their existing governed targets.
- [x] Internal-browser acceptance shows the larger registry and both semantic domains without fixed
      Customer-only relationship copy, completes the unchanged north-star workflow, has no
      warning/error console entries, and has no horizontal overflow at a narrow viewport.

## Required automated checks

```bash
pytest tests/unit/test_semantic_registry.py tests/unit/test_semantic_planner.py \
  tests/unit/test_governed_execution.py tests/unit/test_ui_view_models.py
make demo-reset-proof
make test-integration
make test-acceptance
make evaluate
make check
make coverage
git diff --check
```

## Manual test for the operator

1. Start the internal browser with the recorded enterprise registry and local synthetic reader.
2. Verify Overview shows the registry identity/fingerprint and counts above the old 3-model/2-join
   ceiling.
3. Inspect Semantic Models and Relationships; verify Customer/Account and
   Product/SalesOrder/SaleLine/Shipment artifacts, transformations, decisions, evidence, and risks.
4. Complete the north-star workflow and verify exact results/rejections and read-only safety facts.
5. Use a narrow viewport, inspect browser warning/error logs, and confirm no secrets, raw claims,
   private data, or fixed relationship diagram is rendered.

## Explicit non-goals

- Reconstructing the complete registry from live DataHub; M22 implements the same port.
- Registry publication/activation workflows and durable schema migrations; M23 owns those controls.
- A registry-wide dynamic natural-language or guided UI; M27 owns that surface.
- Raising the three-table query maximum, executing many-to-many contracts, or weakening SQL policy.
- Adding employer data or using random, time-dependent, or network-derived seed generation.

## Handoff requirements

Update `tasks/PROJECT_STATE.md`, `tasks/CURRENT_TASK.md`, `tasks/DECISION_LOG.md`, the relevant ADRs,
and all changed examples/runbooks. Return `tasks/HANDOFF_TEMPLATE.md` with exact reset hashes,
commands, browser evidence, known limitations, and one proposed commit message.

## Completion evidence

- Registry file SHA-256:
  `4f086f8b9f0679d6405beab45d48c6601520b9b6e0c5f20d7dc0ad987485a723`
- Registry fingerprint:
  `0710148874049078f751ac96f0a18131cf01daa41f27dc034ca52a8b212dd966`
- Seed global SHA-256:
  `487388495115265d5fac5a17675e05c236fd4eb4c430613ef02050a5f6010654`
- `make check`: 501 service-free tests plus Ruff and strict mypy passed.
- `make test-integration`: 31 passed.
- `make test-acceptance`: 13 passed.
- `make coverage`: 545 passed at 80.48%.
- `make evaluate`: PASS for 5 query cases, 38 safety cases, and 2 recipe cases.
- `python scripts/release_audit.py`: PASS for architecture, secrets, local links, artifacts, and
  dependency licenses; the uncommitted-tree warning remains expected.
- Live DataHub: 11 described/profiled datasets verified; MCP mutation tools absent.
- Internal browser: clean three-action run, 7/31/5 registry view, both domains visible, zero
  warning/error entries, and no horizontal overflow at 390 × 844.
