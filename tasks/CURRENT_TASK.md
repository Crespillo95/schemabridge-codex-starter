# Current task

- Current milestone: M30 — Production evaluation and security verification
- Status: Phase 0, Phase 1a, fail-closed Phase 1b policy preparation, local descriptor-anchored
  filesystem/subprocess hardening and the qsp3 target-bound copy-SQL vertical are integrated over
  the published draft-PR baseline; exact commit `23dea0f` passes its clean-room gate and the current
  D137/D138 implementation passes the 174-test M30 cut. Security hotfix commit `664b90f` upgrades
  only the exact runtime lock to `cryptography==50.0.0`, passes the 4,073-test full local gate and
  has an exact local/recorded desktop/responsive browser regression. D140 is hardening the current
  workflows onto reviewed Node-24 action releases, binding every action SHA to its exact reviewed
  version and adding real Streamlit download-byte acceptance. Its focal 171-test cut, 174-test M30
  cut, runtime wheel, full 4,076-test gate and desktop/390×844 in-app-browser regression pass. The
  managed/operated browser, campaign, pilot, commercial availability, production and release remain
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
  `7419abb1fd87e66e4f24e4102c6efe28cad8f1dd7937342cadd5fddd8ce61f6f`.
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
- D139 refreshes only the exact runtime dependency lock from `cryptography==49.0.0` to `50.0.0`
  after HIGH GHSA-g6cj-pr64-35w5. Exact exports, `pip check`, `pip-audit`, targeted auth/OIDC/
  readiness tests, static policy and clean runtime-wheel smoke pass without changing product scope
  or any M30 trust fact.
- D140 replaces every direct or Trivy-transitive Node-20 GitHub Action with the minimum reviewed
  Node-24 release. A closed static allowlist now rejects an unknown full SHA or a version comment
  that does not match its reviewed SHA. The exact M30/release workflow SHA-256 values are
  `7419abb1...e61f6f` and `cf817243...8d643d`; the CI workflow fixes Trivy `v0.69.3` explicitly.
  Streamlit acceptance also retrieves the real in-memory download and proves its UTF-8 bytes,
  visible SHA, MIME and filename match both simple and advanced SQL with no BOM or added newline.
  This is local implementation evidence, not a browser clipboard/destination or external control.
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
- Exact hotfix commit `664b90f` passes hashed lock export, `pip check`, vulnerability audit, 277
  targeted auth/OIDC/readiness/supply-chain tests, static policy, clean runtime-wheel smoke and
  `make check`: formatting/Ruff over 745 files, strict Mypy over 366 sources, performance and 4,073
  functional tests with 250 deselections. Its clean readiness report passes the clean-tree/source
  gates, fails only branch/tag repository gates, leaves all 24 controls `missing_external`, performs
  zero writes/network/database calls and returns `release_decision=no_go`.
- D140 focal supply-chain plus copy/download acceptance passes 171 tests. Static supply-chain and
  release policy pass over the modified tree; Actionlint 1.7.12 and ShellCheck 0.11.0 pass all
  workflows with only the documented `concurrency.queue` schema exclusion. The M30 cut passes 174,
  runtime-wheel smoke passes, and `make check` passes 4,076 functional tests with 250 deselections
  in 864.50 seconds. After recording the browser evidence, the final evidence tree passes the same
  complete gate again in 907.58 seconds. Exact hosted Node-20-warning-free evidence remains to be
  recorded.
- Codex's in-app browser completed the exact-`664b90f` local/recorded regression. Desktop covered
  25-line simple and 106-line advanced standalone PostgreSQL, pre-confirmation no-SQL, exact
  confirmation, `executed=false`, `date_meaning` and `unsupported_request`; both blocked paths
  returned no SQL. A same-engine 390×844 iframe verified Query Studio layout/navigation and
  `clientWidth=scrollWidth=390`; desktop and responsive warning/error logs were empty. The browser
  runtime ignored its direct viewport override and nested-frame text injection was not used, so
  mobile SQL generation, managed target operation, clipboard/download bytes and the complete
  accessibility matrix remain pending. No current-final-byte managed/operated browser PASS is
  claimed.
- The current D140 in-app-browser regression repeats the advanced 106-line path with no SQL before
  confirmation, `executed=false`, a real download event and optional execution disabled. The
  `date_meaning` request returns no SQL/download/confirmation. A native 390×844 viewport reports
  document/body widths exactly 390 with the same blocked state, and desktop/mobile warning/error
  logs are empty. The internal clipboard remained unobservable, so clipboard/destination fidelity
  is not claimed; the real Streamlit media-store acceptance is local server-side evidence only.
- Exact commit `23dea0f` passes a dedicated clean-room `make check`: supply-chain/release audit,
  formatting, Ruff, strict Mypy over 366 source files, the isolated performance node and 4,044
  functional tests pass with 250 deselections in 1,023.35 seconds. The later documentation-only
  evidence correction receives dedicated documentation/readiness/diff checks; PR merge-ref CI and
  exact tagged-main candidate evidence remain separate.
- Draft PR #1 publishes `785a052` (qsp3 target binding) and `23dea0f` (fail-closed Phase 1b policy
  preparation); D139 hotfix `664b90f` and documentation commit `5e55fd4` are the published head
  before D140.
  Run `30847018014` is correlated to head `23dea0f` but executes merge ref `00f72c1`;
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
  co-tenant can still ABA-substitute private verifier paths; adversarial reproduction consumed a
  substituted executable and input while their paths were restored before post-read. A portable
  fd-input/fd-exec repair is unavailable on the current Darwin matrix and would not isolate
  same-UID process authority. This remains a conditional P1 before commercial authority and
  requires a dedicated non-co-tenant evaluator with root-owned binaries and read-only inputs.

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
