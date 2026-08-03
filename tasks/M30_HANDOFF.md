# Milestone handoff

## Summary

- Milestone: M30 — Production evaluation and security verification, Phase 0 + Phase 1a + schema-v2
  qsp3 target-bound copy-SQL hardening
- Status: partial; preparation/authentication and qsp3 target binding are implemented locally, with
  focused evidence passing while the final exact-current-byte `make check`, managed/operated browser
  campaign and all external campaign evidence remain pending or blocked
- Recommended operator decision: publish only to draft PR #1 for review; do not merge, tag or
  dispatch Phase 1a, and retain M30, pilot, commercial availability, production and release
  **NO-GO**
- Proposed commit message: `feat: bind governed copy SQL to current target`
- GitHub delivery: update draft PR #1 on `agent/ignore-node-modules`; do not merge or tag

## Implemented

- Advanced the Phase-0 machine contract to schema v2. It freezes PostgreSQL typed-plan-v2 plus the
  exact qsp3/M26 checkpoints, four-field target tuple, target-bound consumers, provider-free
  retained-artifact revalidation and non-commercial-only unbound output. The offline preflight can
  never accept hosted, operated, independent or owner evidence from repository fixtures.
- Added a canonical external Phase-1a campaign manifest binding the exact candidate and its 41
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
- Held the exploratory Phase-1b policy/receipt draft out of the publishable candidate after
  independent P1 review found self-assertable PASS objects, no trusted clock/anti-replay ledger and
  a same-UID path race. No Phase-1b code or PASS claim is included as evidence.
- Added a mandatory managed semantic/target boundary: staging/production gates the complete active
  registry through M26 before target/provider access, gates selected dependencies after
  interpretation/at confirmation/before generation and then resolves the exact current registry-v2
  PostgreSQL target at every boundary. Missing or stale authority produces no preview/SQL.
- Upgraded signed preview claims from qsp2 to qsp3 and bound the complete target tuple into the
  resolved-plan fingerprint. The same target reaches compiler, both AST guards, renderer metadata
  and final artifact; Streamlit provider-free regenerates/compares on every later rerun and purges
  drift before display/download. The artifact remains `executed=false` and no executor or source
  credential is added.
- Retained unbound local/recorded composition only as visibly non-commercial deterministic evidence;
  it is not an alternate managed or tenant-facing mode.
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
- `plans/M30_CAMPAIGN_CONTRACT.yml`, `src/schemabridge/domain/production_readiness.py` and
  `tests/unit/test_m30_readiness.py`: schema-v2 managed copy-SQL contract and weakening regressions;
  the reviewed candidate source inventory remains 37.
- `src/schemabridge/domain/advanced_query_studio.py`,
  `src/schemabridge/application/natural_sql.py`,
  `src/schemabridge/application/ports/advanced_query_studio.py`,
  `src/schemabridge/adapters/query_studio/advanced_security.py` and
  `src/schemabridge/bootstrap.py`: qsp3 target claims, repeated current-target resolution and
  mandatory managed composition.
- `src/schemabridge/entrypoints/streamlit/natural_sql.py`: exact managed target identity and explicit
  unbound/non-commercial presentation.
- `scripts/verify_supply_chain.py`: exact workflow bytes/topology/actions/sign-job policy.
- `docs/commercial/`, `docs/06_SECURITY.md`, `docs/12_RUNBOOK.md`, `docs/13_UI_SPEC.md`,
  `docs/14_BROWSER_ACCEPTANCE.md`, `docs/19_COMMERCIAL_USAGE.md`, `README.md` and the M30 plan: accurate
  product, operator and NO-GO boundary.
- M30 unit/acceptance/support, supply-chain and commercial-documentation tests: mutation, CLI,
  report, trust-provider and workflow regressions.
- `tests/m32_target_support.py`, `tests/unit/test_natural_sql_target_binding.py` and the updated M32
  unit/acceptance/Streamlit cuts: exact target propagation, route drift, substitution,
  cross-connection and unavailable-target failures.
- `tasks/CURRENT_TASK.md`, `tasks/PROJECT_STATE.md`, `tasks/WORK_QUEUE.md`, `tasks/M30_HANDOFF.md`
  and `tasks/DECISION_LOG.md`: current candidate state, bounded local evidence and D132/D133.

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
| `make m30-readiness` on clean `bf7d18a` | pass with explicit NO-GO | 2 repository failures (`main`/tag), 24 missing external controls, 37 source digests, zero external I/O/writes |
| `make supply-chain-static` | pass | Static policy and release audit pass; dirty-tree warning expected before commit |
| `gh run view 30826970514 ...` | pass/read-only | All three jobs passed for old head `e49e5d7`; PR merge subject, not current M30 evidence |
| GitHub configuration read-only audit | pass/read-only | `main` unprotected; zero environments, rulesets and tags |
| First `make check` attempt | fail | One transient `inspect.getsource` assertion failed; 3,967 passed and 248 deselected in 895.58 s |
| Exact failing test and complete test module reruns | pass | 1 passed, then all 13 module tests passed without code or assertion changes |
| Pre-final isolated `make check` | pass | Ruff format/lint, mypy over 364 source files, performance node and 3,988 functional tests passed; 248 deselected in 1,132.41 s before the final trust/clock/TOCTOU closures |
| Exact final-byte local `make check` | incomplete (environment termination) | Reached 65% with no recorded test failure before SIGTERM 15; it is not a pass |
| Exact current-byte M30/supply-chain selection | pass | 52 passed, 158 deselected after the final trust/clock/TOCTOU closures |
| Specific qsp3/bootstrap/acceptance selection | pass | 16 passed after M26/target-bound-plan remediation |
| Broad qsp3 selection | pass | 74 passed |
| M26/governed execution/recipes/qsp3 selection | pass | 96 passed |
| Streamlit copy-first selection | pass | 4 passed, including purge after managed target rotation |
| Consolidated M26/qsp3/M32 selection | pass | 124 passed in 6.82 seconds |
| Schema-v2 M30 readiness/campaign selection | pass | 103 passed in 89.65 seconds |
| Qsp3/contract focal format, Ruff and mypy | pass | Seven qsp3 source files plus the contract model pass |
| `git diff --check` | preliminary pass | Current qsp3 implementation/state bytes pass; rerun after final consolidation |
| Codex in-app-browser local/recorded subset | bounded pre-final observation | Advanced desktop, 390×844 no-overflow and `date_meaning`; clean console; not managed/final-byte evidence |
| Final exact-current-byte qsp3 `make check` | **PENDING** | Must complete on the exact publishable snapshot before upload |

## Automated test results

- Focused tests: the recorded Phase-1a cuts pass: 243 in the combined
  M30/commercial/supply-chain selection, 19 in the final verifier/CLI/workflow selection, 4
  commercial-documentation contracts and the final corpus mutation regression. Current qsp3 cuts
  pass 16 specific, 74 broad, 96 with M26/governed execution/recipes, four Streamlit and 124 in the
  consolidated selection. Schema-v2 M30 contract/readiness/campaign passes 103.
- `make check`: the isolated 3,988-test pass predates the final trust, clock and verifier-TOCTOU
  closures. The exact final-byte local attempt reached 65% with no recorded assertion failure but
  was terminated by the execution environment, so it is not a pass. The exact current-byte focal
  selection passes 52 tests with 158 deselections. The qsp3 changes have no final
  exact-current-byte `make check` yet; exact-head hosted CI also remains pending.
- Integration tests: Phase 1a adds no service adapter. Qsp3 composes the governed execution-target
  resolver in managed mode and exercises exact synthetic registry-v2 target contracts, but no
  operated destination receipt exists; old hosted PostgreSQL evidence for a prior PR merge subject
  is not accepted for this candidate.
- Acceptance tests: Phase-1a clean synthetic subjects authenticate bytes while execution/release
  remain blocked. The qsp3 acceptance cuts prove exact target propagation and `executed=false`;
  unavailable, substituted, cross-connection, semantic-stale and route-drift states return no SQL.
- Coverage: not rerun for this vertical; no new coverage claim.
- Browser: before the final P1 remediation, Codex's in-app browser observed the local/recorded
  advanced desktop path, 390×844 no-overflow and `date_meaning` ambiguity with empty warning/error
  console, 106 SQL lines only after confirmation, no placeholders, download visible,
  `Ejecutado=No`, `Sin ligar` and non-commercial warning. Post-remediation managed rotation is
  AppTest only. A current-final-byte managed/operated browser PASS and integrated commercial surface
  remain absent.

## Operator manual test

Qsp3 local/recorded observation completed pre-final; managed/operated verification remains
**PENDING**:

- observed: no SQL/download before confirmation; 106 visible standalone lines, no `%s`/`$1`,
  download visible, optional execution disabled, `Ejecutado=No`, `Sin ligar`/non-commercial after
  confirmation; 390×844 had no overflow; `date_meaning` had no confirmation/SQL/download; console
  warnings/errors were empty;
- post-remediation automation: four Streamlit AppTests include target rotation after an artifact and
  prove SQL/download are purged with a sanitized error;
- this does not prove current-final-byte managed or operated behavior.

Remaining managed procedure:

1. Open Natural SQL in Codex's in-app browser against synthetic registry-v2 context with one exact
   current same-connection PostgreSQL target. Verify the page shows connection, route revision,
   target fingerprint and type-contract fingerprint before confirmation.
2. Exercise simple and advanced Spanish/English requests at desktop and mobile widths. Confirm the
   preview produces no copy artifact before explicit confirmation and the final artifact carries
   the same target tuple with `executed=false`.
3. Repeat with missing, disabled and cross-connection targets and with route rotation after
   interpretation, after preview and after confirmation. Every state must fail closed with no SQL
   artifact and no hidden browser error.
4. Verify the unbound target state remains limited to local/recorded and is never presented as a
   managed success.

M30 manifest-authentication operator flow — not authorized for dispatch today:

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
- Source database writes: zero. Phase 1a composes no database connector, and qsp3 target binding
  adds neither a source credential nor an executor.
- SQL/LLM validation: Phase 1a accepts or produces no SQL, prompt or model response. The qsp3 path
  still permits only typed intent; deterministic compilation and two AST guards receive one exact
  resolved target before the copy-only artifact is returned.
- Target isolation: managed composition applies M26 to the complete registry before target/provider
  access and to the selected plan at every later trust boundary. The target is part of the plan
  fingerprint; route drift, target substitution and cross-connection resolution fail before SQL is
  exposed, and later UI reruns purge a retained artifact on drift.
- DataHub mutation approval: no DataHub port or writer credential is composed.
- Receipt trust: no Phase-1b policy/receipt implementation is included. The exploratory draft was
  rejected because caller-built authentication literals/digests/booleans could represent PASS
  without authenticated raw measurement, trusted time or a durable replay ledger.
- Secrets/proprietary data: workflow input permits only public opaque IDs, versions and digests;
  raw cases, answer key, prompts, SQL, rows, credentials and protected topology are forbidden.
- Fanout/semantic risks: the existing one-connection/three-table/two-join boundary is frozen; exact
  language/family/risk slices prevent aggregate scores from hiding a missing critical family.
- Independent review: qsp3 reports P0=0/P1=0. The excluded Phase-1b draft reports no composed P0 and
  multiple P1 findings. The four official GitHub
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
- Decision: require M26-current semantics plus one exact registry-v2 target in qsp3 and re-resolve
  both authorities before every provider/confirmation/generation trust transition; revalidate
  retained artifacts before later display/download.
- Reason: qsp2 or one-time resolution could not prevent homonymous destination substitution or
  route drift between interpretation, confirmation and copy artifact generation.
- Decision: freeze those requirements in M30 campaign-contract schema v2; the prior schema v1 must
  not admit a targetless candidate.
- Logged in: D132 and D133.
- Decision: hold the exploratory Phase-1b draft out of the candidate.
- Reason: policy chosen by the candidate, replayable attempts and caller-supplied signed facts do
  not prove truthful independent measurement.
- Logged in: D134.

## Known limitations or unverified items

- GitHub `main` is unprotected; there are zero environments, rulesets and tags. The required
  attestation environment, reviewer/self-review/bypass policy and eligible-tag protection do not
  exist.
- No exact clean tagged candidate, final-candidate hosted CI/SBOM/provenance, authenticated Phase-1a
  manifest or control receipt exists.
- Phase 1b remains unimplemented. It needs an independently controlled trust root, cryptographic
  verifier over canonical raw snapshots, trusted time and durable compare-and-swap replay ledger
  before any deterministic control-specific adjudicator can be accepted.
- Same-UID precheck/process/postcheck replacement is a residual local threat-model limitation; it
  cannot enable campaign/release in Phase 1a, but must be removed or independently sandboxed before
  reusing this boundary as execution authority.
- The 1,000-case blind corpus, exact provider/target operation, equivalence, scale/soak,
  browser/accessibility, IAM/network/secrets/SIEM/restore, pentest and owner decisions are absent.
- The pre-final local/recorded browser observation and post-remediation AppTest are not a
  current-final-byte managed/operated browser PASS. Final exact-current-byte `make check` is still
  pending.
- PostgreSQL is the only output dialect. Arbitrary SQL, cross-database portability, federation,
  more than three tables/two joins and unsupported families are not certified.
- Managed staging/production now rejects an absent or stale target binding. Local/recorded mode may
  still emit `target_fingerprint=None` only with an explicit non-commercial label; it cannot be
  promoted by manually checking the destination. Operated destination certification, the
  commercial OpenAPI/integrated console, offboarding automation, legal/privacy/support/pricing and
  real pilot remain incomplete.

## Blockers

- External GitHub protection and independent attestation authority.
- Independent receipt trust bundle, verifier, raw-measurement derivation and anti-replay ledger.
- Final exact-current-byte `make check` and managed/operated qsp3 browser matrix.
- Operated M29 prerequisites and all 24 M30 hosted/target/third-party/owner controls.
- M30 corpus/security campaign and M31 design-partner pilot.

## Next milestone readiness

- Dependencies satisfied: M35 and M29 local baselines; Phase 0/1a repository preparation and qsp3
  target-binding implementation only.
- Recommended next prompt: first consolidate the final qsp3 browser matrix and exact-current-byte
  `make check`; then provision the external GitHub/target/assessor trust bundle and implement the
  concrete receipt verifier plus replay ledger before composing the first hosted adjudicator.
- Required operator prerequisites: protected GitHub controls, exact candidate/tag/artifacts,
  independently owned corpus/answer key, real isolated target, pentest assessor and distinct
  product/semantic/security/operations/release approvers.
