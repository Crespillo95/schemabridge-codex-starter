# Current task

- Current milestone: M30 — Production evaluation and security verification
- Status: Phase 0 prepared locally; Phase 1a passes its final current-byte local gate; campaign,
  pilot, production and release remain blocked
- Plan: `plans/M30_PRODUCTION_EVALUATION_SECURITY.md`
- Machine contract: `plans/M30_CAMPAIGN_CONTRACT.yml`
- Handoff: `tasks/M30_HANDOFF.md`
- Production/release GO: **NO**

## Objective

Freeze the exact PostgreSQL copy-first commercial boundary, authenticate one canonical external
campaign-manifest subject without turning it into execution authority, and leave every hosted,
operated, independent and owner decision explicit. M30 must measure a 1,000-case blind bilingual
campaign and target controls before it can be accepted; repository evidence alone cannot do that.

## Prepared locally — not M30 acceptance

- Phase 0: strict typed-plan-v2 contract, exact candidate/source/lock/workflow/migration identities,
  24 evidence controls and deterministic ignored readiness reports that can return only NO-GO.
- Phase 1a: canonical external manifest, structural JSON Schema plus authoritative validator,
  artifact/provider/target/corpus/owner/control freezes and a maximum 30-day UTC window.
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
- Commercial operating-model documents cover roles/RACI, setup, tenant onboarding, daily bounded
  M32 use, incidents/DR, offboarding and a pilot scorecard while explicitly remaining non-executable
  end-to-end until OpenAPI/payloads and an integrated commercial surface exist.

## Current evidence and boundary

- `make m30-readiness` on the current development tree reports `blocked_prerequisites`, four
  repository failures, 24 `missing_external` controls, 37 exact source digests, no external calls or
  writes, `campaign_executable=false` and `release_decision=no_go`.
- Focused M30/commercial/supply-chain selections pass after the Phase-1a hardening. The final
  isolated current-byte `make check` passes 3,988 tests with 248 explicit deselections in 1,132.41
  seconds; publication evidence remains candidate-specific and is recorded only after upload.
- Hosted PR run `30826970514` passed quality, PostgreSQL integration and supply chain for branch
  head `e49e5d7d52f9e4005e4d469b24e7b23797a59aeb`, but its test checkout/evaluation subject is PR merge
  ref `43b4c21676c7bbffaa7b81c2e98d398692e43e8f`; it predates Phase 1a and is not exact M30 candidate
  evidence.
- GitHub external state was inspected read-only: `main` is unprotected and the repository has zero
  environments, zero rulesets and zero tags. Therefore `m30-manifest-attestation` cannot yet be a
  protected independently reviewed gate and must not be dispatched.
- Independent adversarial review found no remaining local P0 capable of enabling campaign/release.
  The residual trust-root limitation is explicit: policy/verifier and external protection must be
  distributed/proved independently before Phase 1b or any operated claim.

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
commercial hard stop, not a manually waivable risk.

## External work still required

1. Protect `main` and eligible annotated tags; create `m30-manifest-attestation` with independent
   reviewers, self-review/bypass disabled, no secrets and retained configuration evidence.
2. Freeze one exact clean tagged candidate and independently owned public manifest/corpus digests.
3. Authenticate control-specific Phase-1b receipts and enforce prerequisite ordering before any
   provider/source/target/corpus access.
4. Operate the 1,000-case blind corpus, execution equivalence, scale/soak, browser/accessibility,
   IAM/network/secrets/SIEM/backup/restore and independent penetration test.
5. Obtain candidate-specific owner/assessor decisions, then run M31 with real design partners.
6. Close the commercial OpenAPI/integrated-console, target-fingerprint, offboarding automation,
   legal/privacy/support/pricing and per-dialect certification gaps.

## Previous milestone snapshot — M35

M35 remains complete and accepted locally under D129. Its schema-v15 join/model-change lifecycle,
M34 publication handoff, PostgreSQL/HTTP/acceptance/browser/full gates and exact commercial limits
are recorded in `tasks/M35_HANDOFF.md`. Its local DataHub receipt remains simulated and the active
pointer is not changed.
