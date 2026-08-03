# Milestone handoff

## Summary

- Milestone: M30 — Production evaluation and security verification, Phase 0 + Phase 1a
- Status: partial; preparation/authentication vertical passes the final current-byte local gate,
  while the campaign and all operated evidence remain blocked
- Recommended operator decision: publish only to draft PR #1 for review; do not merge, tag or
  dispatch Phase 1a, and retain M30, pilot, commercial availability, production and release
  **NO-GO**
- Proposed commit message: `feat: harden M30 manifest authentication`
- GitHub delivery: update draft PR #1 on `agent/ignore-node-modules`; do not merge or tag

## Implemented

- Retained the Phase-0 PostgreSQL typed-plan-v2 contract and offline preflight that can never accept
  hosted, operated, independent or owner evidence from repository fixtures.
- Added a canonical external Phase-1a campaign manifest binding the exact candidate and its 37
  reviewed source identities, artifacts, provider/model/configuration, target IAM/network/secrets,
  corpus/oracle digests, owners and all 24 control assignments.
- Closed corpus weakening: every slice is exactly 50/50 Spanish/English; simple families are
  standard; every advanced family has standard, high and critical slices; ambiguous,
  unsupported and adversarial families/risks are exact; class totals remain 1,000 cases.
- Added a structural JSON Schema command and an authoritative canonical validator. Duplicate,
  extra, non-ASCII, NUL, deep, oversized, internal, symlinked, changing or non-canonical evidence
  fails closed.
- Added a two-job manual GitHub workflow at SHA-256
  `8944a48a3a14fe0c2aca4edca2d0a7bed00d0a0ce7fc699f5ff420ff5668d3d0`. The read-only job checks
  exact tagged-main identity and validates the manifest; the protected OIDC job retrieves one
  digest-bound artifact and never checks out or executes candidate code.
- Added detached-bundle verification through exact repository/ref/revision/workflow/OIDC/SLSA/
  hosted-runner policy. The verifier ignores caller `PATH`, accepts only official GitHub CLI 2.96.0
  executable bytes for four reviewed OS/architecture pairs, uses a private empty config and private
  evidence snapshots, and records verifier/bundle/summary/certificate/timestamp hashes.
- Renamed the workflow/environment to manifest attestation and the success field to
  `workflow_attested_manifest_authenticated`, preventing byte authentication from being presented
  as owner approval or campaign authorization.
- Added deterministic JSON/Markdown reports under the exact ignored local path or a new external
  destination. Success always retains `campaign_executable=false`, `release_decision=no_go`,
  `external_controls_passed=0` and `external_controls_remaining=24`.
- Added the commercial operating model: roles/RACI and signature gates, tenant setup/onboarding,
  bounded daily M32 use, incident/DR objectives, offboarding and pilot scorecard. It explicitly says
  the current manuals are not an integrated end-to-end commercial surface.

## Files changed

- `.github/workflows/m30-manifest-attestation.yml`: split read-only validation and protected
  attestation workflow.
- `src/schemabridge/domain/production_campaign.py`: manifest/corpus/owner/authentication invariants.
- `src/schemabridge/application/production_campaign.py`: validation and authentication use cases.
- `src/schemabridge/adapters/evaluation/m30_campaign.py`: filesystem, GitHub CLI and report adapters.
- `src/schemabridge/application/ports/production_evidence.py` and `src/schemabridge/bootstrap.py`:
  ports and composition-root builders.
- `scripts/m30_validate_campaign_manifest.py`,
  `scripts/m30_authenticate_campaign_manifest.py` and `Makefile`: operator commands and exit
  contracts.
- `src/schemabridge/domain/production_readiness.py`: 37 exact candidate source identities,
  including the Phase-1a security boundary and commercial operating model.
- `scripts/verify_supply_chain.py`: exact workflow bytes/topology/actions/sign-job policy.
- `docs/commercial/`, `docs/06_SECURITY.md`, `docs/12_RUNBOOK.md`, `docs/13_UI_SPEC.md`,
  `docs/14_DEPLOYMENT.md`, `docs/19_COMMERCIAL_USAGE.md`, `README.md` and the M30 plan: accurate
  product, operator and NO-GO boundary.
- M30 unit/acceptance/support, supply-chain and commercial-documentation tests: mutation, CLI,
  report, trust-provider and workflow regressions.
- `tasks/CURRENT_TASK.md`, `tasks/PROJECT_STATE.md`, `tasks/WORK_QUEUE.md`, `tasks/M30_HANDOFF.md`
  and `tasks/DECISION_LOG.md`: current candidate state, final local gate and D132.

## Commands executed

| Command | Result | Notes |
|---|---|---|
| `.venv/bin/ruff format src tests scripts` | pass | Current Python files formatted |
| `.venv/bin/ruff check src tests scripts` | pass | Zero findings |
| `.venv/bin/mypy src` | pass | 364 source files |
| Focused M30/commercial/supply-chain Pytest selection | pass | 243 tests on the pre-final refactor; subsequent changed branches rerun individually |
| Focused verifier/CLI/workflow selection | pass | 19 passed after byte-pinning and workflow rename |
| Corpus-balance/critical mutation regression | pass | 1 passed after exact risk/language hardening |
| `.venv/bin/pytest -q tests/unit/test_commercial_operations_docs.py` | pass | 4 documentation-contract tests on the corrected operating-model bytes |
| `make m30-readiness` | pass with explicit NO-GO | 4 repository failures, 24 missing external controls, 37 source digests, zero external I/O/writes |
| `make supply-chain-static` | pass | Static policy and release audit pass; dirty-tree warning expected before commit |
| `gh run view 30826970514 ...` | pass/read-only | All three jobs passed for old head `e49e5d7`; PR merge subject, not current M30 evidence |
| GitHub configuration read-only audit | pass/read-only | `main` unprotected; zero environments, rulesets and tags |
| First `make check` attempt | fail | One transient `inspect.getsource` assertion failed; 3,967 passed and 248 deselected in 895.58 s |
| Exact failing test and complete test module reruns | pass | 1 passed, then all 13 module tests passed without code or assertion changes |
| Final isolated `make check` | pass | Ruff format/lint, mypy over 364 source files, performance node and 3,988 functional tests passed; 248 deselected in 1,132.41 s |
| `git diff --check` | pass | Current corrected documentation bytes; rerun if any file changes |

## Automated test results

- Focused tests: pass; 243 in the combined M30/commercial/supply-chain selection, 19 in the final
  verifier/CLI/workflow selection, 4 commercial-documentation contracts and the final corpus
  mutation regression.
- `make check`: final isolated run passed 3,988 tests with 248 explicit deselections in 1,132.41
  seconds. The preceding contended attempt had one transient source-inspection failure; its exact
  test and complete 13-test module both passed unchanged before the clean full rerun.
- Integration tests: no new service adapter is composed by Phase 1a; old hosted PostgreSQL evidence
  passed for a prior PR merge subject and is not accepted for this candidate.
- Acceptance tests: Phase-1a clean synthetic subject authenticates bytes while execution/release
  remain blocked; dirty/non-main/unready subjects do not touch the trust provider.
- Coverage: not rerun for this vertical; no new coverage claim.
- Browser: no new runtime UI was added. M32 has exactly one later advanced desktop happy-path PASS;
  its remaining desktop/mobile/blocked-state matrix is pending. M35 remains historical local
  evidence, and the commercial integrated surface is explicitly missing.

## Operator manual test

1. From a clean exact candidate run `make m30-readiness`; inspect ignored
   `.local/m30/readiness.json` and `.md`.
2. Run `make m30-manifest-schema`; create the canonical public manifest outside the checkout and
   run `make m30-manifest-validate M30_MANIFEST=/external/.../manifest.json`.
3. Do **not** dispatch today. First obtain external evidence for protected `main`/annotated tags and
   the no-secret `m30-manifest-attestation` environment with independent reviewers and no bypass.
4. Once those prerequisites exist, follow the exact run-ID/actor/download/bundle commands in
   `docs/12_RUNBOOK.md`. Rerun all jobs, never failed jobs only.
5. Run `make m30-authenticate-manifest` with the downloaded manifest and bundle outside the
   checkout; inspect `authentication.json` and `authentication.md`.

Expected bounded success:

```text
workflow_attested_manifest_authenticated=true
campaign_executable=false
release_decision=no_go
external_controls_passed=0
external_controls_remaining=24
```

Exit `0` authenticates/validates only the bounded Phase-1a subject; `2` is blocked and `3` is an
invalid/unavailable evidence, trust-provider or report-write failure. No code path authorizes
provider/source/target/corpus access, production or release.

## Architecture and security review

- Dependency direction: domain remains pure; application depends on protocols; filesystem/GitHub
  and report I/O are adapters; `bootstrap.py` is the only composition root.
- Source database writes: zero; Phase 1a composes no database connector.
- SQL/LLM validation: no SQL, prompt or model response is accepted or produced by this vertical.
- DataHub mutation approval: no DataHub port or writer credential is composed.
- Secrets/proprietary data: workflow input permits only public opaque IDs, versions and digests;
  raw cases, answer key, prompts, SQL, rows, credentials and protected topology are forbidden.
- Fanout/semantic risks: the existing one-connection/three-table/two-join boundary is frozen; exact
  language/family/risk slices prevent aggregate scores from hiding a missing critical family.
- Independent review: no remaining local P0 can enable campaign/release. The four official GitHub
  CLI 2.96.0 archive/binary digests were checked against the release artifacts, while the local
  acceptance still substitutes the verifier response. Real Artifact Attestations → detached bundle
  → byte-pinned CLI interoperability remains external evidence, not an M30 control for this
  candidate.

## Decisions made

- Decision: authenticate only exact workflow-attested bytes and keep execution/release impossible.
- Reason: the candidate/workflow cannot be its own production trust root, and no external control
  or owner signature currently exists.
- Decision: require official executable bytes and exact bilingual standard/high/critical slices.
- Reason: version/PATH checks and class-level language totals left substitution and vacuous-slice
  gaps.
- Logged in: D132.

## Known limitations or unverified items

- GitHub `main` is unprotected; there are zero environments, rulesets and tags. The required
  attestation environment, reviewer/self-review/bypass policy and eligible-tag protection do not
  exist.
- No exact clean tagged candidate, final-candidate hosted CI/SBOM/provenance, authenticated Phase-1a
  manifest or control receipt exists.
- The verifier/policy remains distributed with the candidate; Phase 1b needs an independently
  controlled trust root and deterministic control-specific receipt adjudicators.
- Same-UID precheck/process/postcheck replacement is a residual local threat-model limitation; it
  cannot enable campaign/release in Phase 1a, but must be removed or independently sandboxed before
  reusing this boundary as execution authority.
- The 1,000-case blind corpus, exact provider/target operation, equivalence, scale/soak,
  browser/accessibility, IAM/network/secrets/SIEM/restore, pentest and owner decisions are absent.
- PostgreSQL is the only output dialect. Arbitrary SQL, cross-database portability, federation,
  more than three tables/two joins and unsupported families are not certified.
- M32 may emit `target_fingerprint=None`, which is a hard stop for tenant-facing commercial use and
  cannot be waived by manually checking the destination. The commercial OpenAPI/integrated
  console, offboarding automation, legal/privacy/support/pricing and real pilot are incomplete.

## Blockers

- External GitHub protection and independent attestation authority.
- Operated M29 prerequisites and all 24 M30 hosted/target/third-party/owner controls.
- M30 corpus/security campaign and M31 design-partner pilot.

## Next milestone readiness

- Dependencies satisfied: M35 and M29 local baselines; Phase 0/1a repository preparation only.
- Recommended next prompt: provision the external GitHub/target/assessor authorities, then design
  Phase 1b authenticated control receipts and prerequisite ordering without broadening SQL scope.
- Required operator prerequisites: protected GitHub controls, exact candidate/tag/artifacts,
  independently owned corpus/answer key, real isolated target, pentest assessor and distinct
  product/semantic/security/operations/release approvers.
