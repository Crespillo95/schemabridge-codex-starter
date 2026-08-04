# Current task

- Current milestone: M30 — Production evaluation and security verification
- Status: Phase 0, Phase 1a, fail-closed Phase 1b policy preparation, local descriptor-anchored
  filesystem/subprocess hardening and the qsp3 target-bound copy-SQL vertical are integrated over
  the published draft-PR baseline; exact commit `23dea0f` passes its clean-room gate and the current
  D137/D138 implementation passes the 174-test M30 cut and 4,073-test full local gate. The managed/operated
  browser remains pending; campaign, pilot, commercial availability, production and release remain
  blocked
- Plan: `plans/M30_PRODUCTION_EVALUATION_SECURITY.md`
- Machine contract: `plans/M30_CAMPAIGN_CONTRACT.yml`
- Handoff: `tasks/M30_HANDOFF.md`
- Production/release GO: **NO**

## Objective

Freeze the exact PostgreSQL copy-first commercial boundary, bind managed output to one current
registry-v2 target, authenticate one canonical external campaign-manifest subject without turning
either operation into execution authority, and leave every hosted, operated, independent and owner
decision explicit. M30 must measure a 1,000-case blind bilingual campaign and target controls before
it can be accepted; repository evidence alone cannot do that.

## Prepared locally — not M30 acceptance

- Phase 0: strict schema-v2 typed-plan-v2 contract, exact candidate/source/lock/workflow/migration
  identities, 24 evidence controls and deterministic ignored readiness reports that can return
  only NO-GO. The contract freezes the qsp3/M26/target-bound-plan/artifact-rerun policy.
- Phase 1a: canonical external manifest, structural JSON Schema plus authoritative validator,
  artifact/provider/target/corpus/owner/control freezes and a maximum 30-day UTC window.
- Phase 1b policy preparation is included under D135/D137/D138: manifest schema v2 binds one canonical
  external policy; the policy freezes the exact 24-control DAG, receipt kinds, evidence subjects,
  producer workflows, authorization stages and five-role quorum. Its only composed use case
  validates policy binding and always keeps external trust/receipt authentication disabled, 0/24,
  no capabilities and `no_go`; the receipt adjudicator remains deliberately uncomposed. External
  reads and report publication now use fail-closed per-component dirfds, stable owner/mode/link/time
  identities, nonblocking leaves, same-dirfd publication, fsync and final read-back. Git and GitHub
  subprocess output is drained through selectors with exact byte ceilings, monotonic deadlines,
  process-group termination and synchronous direct-child reaping on every exceptional path.
- Exact corpus matrix: every slice has equal Spanish/English counts; simple families are standard;
  every advanced family has standard/high/critical slices; ambiguity, unsupported and adversarial
  families/risk levels are closed. Totals remain exactly 500 ES + 500 EN.
- Split GitHub workflow: candidate code runs only in the read-only validation job; the protected
  attestation job downloads one digest-bound artifact and never checks out or executes candidate
  code. Workflow SHA-256 is
  `8944a48a3a14fe0c2aca4edca2d0a7bed00d0a0ce7fc699f5ff420ff5668d3d0`.
- Detached-bundle verifier: exact repository/ref/revision/workflow/OIDC/predicate/hosted-runner
  policy, official platform-specific GitHub CLI 2.96.0 executable hashes, private snapshots,
  bounded inputs/output/time, sanitized config, before/after reads and trace hashes.
- Authentication success means only
  `workflow_attested_manifest_authenticated=true`, `campaign_executable=false`,
  `release_decision=no_go`, zero material controls passed and all 24 remaining.
- Managed staging/production natural-SQL composition first applies M26 to the complete active
  registry before target/provider access, then to selected-plan dependencies after interpretation,
  at confirmation and before generation. Only then does it resolve/re-resolve the exact current
  registry-v2 target. Missing/stale semantics or missing, disabled, cross-connection, substituted or
  route-rotated targets fail before copyable SQL is returned.
- Signed qsp3 preview claims bind `connection_id`, target route revision, target fingerprint and
  target type-contract fingerprint. The target is part of the resolved-plan fingerprint; the
  deterministic compiler, both AST guards, renderer metadata and final `executed=false` artifact
  receive it. Streamlit provider-free regenerates and compares any retained artifact on every later
  rerun and purges SQL/download on drift. This adds no source credential, executor or automatic
  execution capability.
- Local/recorded composition may remain unbound for deterministic development evidence only and is
  labelled explicitly as non-commercial; it is not an alternate tenant-facing operating mode.
- Commercial operating-model documents cover roles/RACI, setup, tenant onboarding, daily bounded
  M32 use, incidents/DR, offboarding and a pilot scorecard while explicitly remaining non-executable
  end-to-end until OpenAPI/payloads and an integrated commercial surface exist.

## Current evidence and boundary

- `make m30-readiness` on clean commit `bf7d18a` reports `blocked_prerequisites`, two repository
  failures (`candidate_main_branch` and `candidate_annotated_release_tag`), 24
  `missing_external` controls, 37 exact source digests, no external calls or writes,
  `campaign_executable=false` and `release_decision=no_go`.
- Focused current-byte M30/supply-chain tests pass 52 tests with 158 deselections after the final
  trust, clock and verifier byte-revalidation hardening. A historical final-byte attempt was terminated
  externally at 65% without a recorded test failure; it was superseded by the completed final
  implementation-snapshot and exact-commit clean-room `make check` results recorded below. PR CI
  executes a merge ref and remains separate from exact tagged-main candidate evidence.
- Current qsp3 evidence passes 16 specific target/bootstrap/acceptance tests, 74 in the broad qsp3
  cut, 96 with M26/governed execution/recipes, four Streamlit cases including post-artifact target
  rotation, and a consolidated 124-test M26/qsp3/M32 selection. The schema-v2 contract/readiness/
  campaign selection passes 103 tests. Focal Ruff, formatting, mypy over seven qsp3 source files
  plus the contract model, and `git diff --check` pass. These are bounded implementation checks,
  not external M30 evidence.
- Final D137/D138 bytes pass `.venv/bin/pytest -q -k m30` with 174 tests and 4,149 deselections in
  286.66 seconds. `make check` passes supply-chain and release audits, formatting, Ruff, strict Mypy
  over 366 sources, the isolated performance node and 4,073 functional tests with 250 deselections
  in 1,272.00 seconds. Independent reviews report no P0–P3 in either bounded subprocess delta on
  the current 0/24, no-capability surface. This validates local policy preparation only, not a
  receipt or external control.
- Codex in-app-browser evidence is bounded and pre-final: local/recorded desktop advanced, 390×844
  no-overflow and `date_meaning` ambiguity were observed with clean console; managed rotation is
  post-remediation AppTest only. On 2026-08-03 the requested final retry connected to the browser
  runtime but returned an empty browser inventory, so no current-final-byte managed/operated
  browser PASS is claimed.
- Exact commit `23dea0f` passes a dedicated clean-room `make check`: supply-chain/release audit,
  formatting, Ruff, strict Mypy over 366 source files, the isolated performance node and 4,044
  functional tests pass with 250 deselections in 1,023.35 seconds. The later documentation-only
  evidence correction receives dedicated documentation/readiness/diff checks; PR merge-ref CI and
  exact tagged-main candidate evidence remain separate.
- Draft PR #1 publishes `785a052` (qsp3 target binding) and `23dea0f` (fail-closed Phase 1b policy
  preparation). Run `30847018014` is correlated to head `23dea0f` but executes merge ref `00f72c1`;
  it must not be described as exact-head or tagged-main candidate evidence.
- Hosted PR run `30826970514` passed quality, PostgreSQL integration and supply chain for branch
  head `e49e5d7d52f9e4005e4d469b24e7b23797a59aeb`, but its test checkout/evaluation subject is PR merge
  ref `43b4c21676c7bbffaa7b81c2e98d398692e43e8f`; it predates Phase 1a and is not exact M30 candidate
  evidence.
- GitHub external state was inspected read-only: `main` is unprotected and the repository has zero
  environments, zero rulesets and zero tags. Therefore `m30-manifest-attestation` cannot yet be a
  protected independently reviewed gate and must not be dispatched.
- Independent qsp3 review reports P0=0/P1=0. Phase-1b remediation closes the earlier direct
  PASS/hash/case/attempt/DAG/clock findings. D137 closes external read/report redirection and
  JSON-last repair defects; D138 closes pre-limit subprocess buffering and proves bounded group
  cleanup while keeping trust, measurement, ledger, dedicated evaluator
  isolation and append-only retention outside the composed authority path. A malicious same-UID
  co-tenant can still ABA-substitute private verifier paths; this is a conditional P1 before
  commercial authority and requires evaluator isolation or a reviewed fd-input/fd-exec verifier.

## Commercial product boundary

The defensible candidate is governed natural-language-to-standalone PostgreSQL for a closed
analytical language, one connection, up to three tables and two approved joins, with exact human
confirmation and no automatic execution. It is not an infallible “supreme SQL expert”, arbitrary
SQL generator, cross-database translator or certified multi-dialect product. Query length (including
50+ lines) is not the criterion; representability, approved semantics and deterministic validation
are. New dialects, larger per-query limits and unsupported families require separate typed
compilers, guards, corpora, scale envelopes and release decisions. Several isolated PostgreSQL
tenants are a future deployment model, not cross-connection query support; a large paginated
catalog does not raise the three-table/two-join request limit. `target_fingerprint=None` is a
commercial hard stop in tenant-facing use, not a manually waivable risk. Managed staging/production
therefore requires the complete qsp3 target binding; only visibly non-commercial local/recorded
evidence may remain unbound.

## External work still required

1. Protect `main` and eligible annotated tags; create `m30-manifest-attestation` with independent
   reviewers, self-review/bypass disabled, no secrets and retained configuration evidence.
2. Freeze one exact clean tagged candidate and independently owned public manifest/corpus digests.
3. Provision an independently authenticated trust bundle, concrete receipt verifier, dedicated
   non-co-tenant evaluator/mount boundary and durable CAS anti-replay attempt ledger; then
   authenticate control-specific receipts and enforce prerequisite ordering before any
   provider/source/target/corpus access.
4. Operate the 1,000-case blind corpus, execution equivalence, scale/soak, browser/accessibility,
   IAM/network/secrets/SIEM/backup/restore and independent penetration test.
5. Obtain candidate-specific owner/assessor decisions, then run M31 with real design partners.
6. Certify the implemented target binding against operated destinations and close the commercial
   OpenAPI/integrated-console, offboarding automation, legal/privacy/support/pricing and per-dialect
   certification gaps.

## Previous milestone snapshot — M35

M35 remains complete and accepted locally under D129. Its schema-v15 join/model-change lifecycle,
M34 publication handoff, PostgreSQL/HTTP/acceptance/browser/full gates and exact commercial limits
are recorded in `tasks/M35_HANDOFF.md`. Its local DataHub receipt remains simulated and the active
pointer is not changed.
