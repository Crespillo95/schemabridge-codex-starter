# Milestone handoff

## Summary

- Milestone: M30 — Production evaluation and security verification, Phase 0 + Phase 1a + fail-closed
  Phase 1b policy preparation + descriptor-anchored local I/O + schema-v2 qsp3 target-bound
  copy-SQL hardening
- Status: partial; preparation/authentication and qsp3 target binding are published on draft PR #1,
  exact commit `23dea0f` passes its clean-room gate, and D137/D138 filesystem and subprocess-output
  hardening pass the current 174-test M30 cut. D139 hotfix `664b90f` upgrades only the exact runtime
  lock to `cryptography==50.0.0`, passes the 4,073-test full local implementation gate and completes
  the bounded exact-hotfix local/recorded browser regression. The managed/operated browser campaign
  and every external campaign control remain pending or blocked
- Recommended operator decision: keep draft PR #1 open for review; do not merge, tag or dispatch
  Phase 1a, and retain M30, pilot, commercial availability, production and release
  **NO-GO**
- Implementation commits on the delivery branch: `785a052` (qsp3), `23dea0f` (Phase 1b
  preparation) and `664b90f` (D139 dependency hotfix)
- GitHub delivery: draft PR #1 on `agent/ignore-node-modules`; do not merge or tag
- Proposed commit message: `docs: record M30 hotfix and browser regression`

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
- Added the remediated Phase-1b policy-preparation boundary under D135. Manifest schema v2 binds one
  canonical external policy that freezes all 24 controls, their exact DAG, receipt kinds, evidence
  subjects, immutable producer workflows, authorization stages and five-role quorum. The composed
  CLI only validates that binding and always returns trust/authentication false, 0/24, no
  capabilities and `no_go`; the receipt adjudicator remains uncomposed.
- Closed the first draft's direct hazards: canonical hashes are internal, GitHub workflow identity
  is case-insensitive, retries remain attempt 1 pending a ledger, prerequisites carry exact
  campaign/manifest/policy bindings, and Phase-1a/1b completion times reject rollback and receipts
  that predate authentication.
- Closed D135's external checked-pathname redirection family under D137. Every evidence/report path
  component is opened relative to held directory descriptors under a root/effective-UID and mode
  policy. Leaves are no-follow/nonblocking, bounded and stable across owner/mode/link/size/time
  checks. Reports use mode `0700` destinations, mode `0600` files, external no-clobber publication,
  same-dirfd canonical-local replacement, fsync, JSON-last and repeated descriptor/path read-back.
  Missing primitives, symlinks, hardlinks, FIFOs and deterministic parent/target/destination races
  fail closed without passing any control. Closing review additionally forced a final path recheck
  after the second read, safe temporary-parent validation, JSON-last republication, orphan-marker
  rejection and two-file Git ignore/index checks.
- Closed bounded-output enforcement before buffering under D138. The GitHub CLI verifier now drains
  `stdout` and `stderr` through nonblocking selectors with exact byte ceilings and a monotonic
  deadline; candidate Git inspection uses the same bounded pattern. Timeout, overflow, missing
  pipes, selector failure and all other exceptional exits signal the isolated process group, reap
  the direct child synchronously and close descriptors. A failure to reap is surfaced as a cleanup
  error; otherwise the original failure propagates. Descendant-heartbeat regressions prove that
  same-process-group children stop rather than only the leader. This is POSIX implementation
  evidence, not external verifier authority, and does not close D137's same-UID pathname ABA
  boundary or a hostile child that creates a new session.
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
- Upgraded the exact transitive runtime lock from `cryptography==49.0.0` to `50.0.0` under D139
  after HIGH GHSA-g6cj-pr64-35w5. Only `requirements/runtime.txt` and `uv.lock` changed; exact hashed
  exports, vulnerability audit, targeted auth/OIDC/readiness checks, static supply-chain policy,
  clean runtime-wheel smoke, full local gate and independent byte-level diff review pass.
- Added the commercial operating model: roles/RACI and signature gates, tenant setup/onboarding,
  bounded daily M32 use, incident/DR objectives, offboarding and pilot scorecard. It explicitly says
  the current manuals are not an integrated end-to-end commercial surface.

## Files changed

- `.github/workflows/m30-manifest-attestation.yml`: split read-only validation and protected
  attestation workflow.
- `src/schemabridge/domain/production_campaign.py`: manifest/corpus/owner/authentication invariants.
- `src/schemabridge/application/production_campaign.py`: validation and authentication use cases.
- `src/schemabridge/adapters/evaluation/m30_campaign.py`: filesystem, GitHub CLI and report adapters.
- `src/schemabridge/adapters/evaluation/m30_readiness.py`: bounded Git candidate inspection and
  process-group cleanup.
- `src/schemabridge/application/ports/production_evidence.py` and `src/schemabridge/bootstrap.py`:
  ports and composition-root builders.
- `scripts/m30_validate_campaign_manifest.py`,
  `scripts/m30_authenticate_campaign_manifest.py` and `Makefile`: operator commands and exit
  contracts.
- `plans/M30_CAMPAIGN_CONTRACT.yml`, `src/schemabridge/domain/production_readiness.py` and
  `tests/unit/test_m30_readiness.py`: schema-v2 managed copy-SQL contract and weakening regressions;
  the reviewed candidate source inventory is 41.
- `src/schemabridge/domain/production_campaign_receipts.py`,
  `src/schemabridge/application/production_control_policy.py`,
  `scripts/m30_validate_control_policy.py`, `docs/20_M30_PHASE1B_CONTROL_POLICY.md` and their tests:
  closed policy/DAG contracts, fail-closed validation/reporting and the explicit operator boundary.
- `src/schemabridge/domain/advanced_query_studio.py`,
  `src/schemabridge/application/natural_sql.py`,
  `src/schemabridge/application/ports/advanced_query_studio.py`,
  `src/schemabridge/adapters/query_studio/advanced_security.py` and
  `src/schemabridge/bootstrap.py`: qsp3 target claims, repeated current-target resolution and
  mandatory managed composition.
- `src/schemabridge/entrypoints/streamlit/natural_sql.py`: exact managed target identity and explicit
  unbound/non-commercial presentation.
- `scripts/verify_supply_chain.py`: exact workflow bytes/topology/actions/sign-job policy.
- `requirements/runtime.txt` and `uv.lock`: exact `cryptography==50.0.0` hotfix and hashes; no other
  package changed.
- `docs/commercial/`, `docs/06_SECURITY.md`, `docs/12_RUNBOOK.md`, `docs/13_UI_SPEC.md`,
  `docs/14_BROWSER_ACCEPTANCE.md`, `docs/19_COMMERCIAL_USAGE.md`, `README.md` and the M30 plan: accurate
  product, operator and NO-GO boundary.
- M30 unit/acceptance/support, supply-chain and commercial-documentation tests: mutation, CLI,
  report, trust-provider and workflow regressions.
- `tests/m32_target_support.py`, `tests/unit/test_natural_sql_target_binding.py` and the updated M32
  unit/acceptance/Streamlit cuts: exact target propagation, route drift, substitution,
  cross-connection and unavailable-target failures.
- `tasks/CURRENT_TASK.md`, `tasks/PROJECT_STATE.md`, `tasks/WORK_QUEUE.md`, `tasks/M30_HANDOFF.md`
  and `tasks/DECISION_LOG.md`: current candidate state, bounded local evidence and D132–D138.

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
| Pre-final isolated `make check` | pass | Ruff format/lint, mypy over 364 source files, performance node and 3,988 functional tests passed; 248 deselected in 1,132.41 s before the final trust/clock/byte-revalidation hardening |
| Exact final-byte local `make check` | incomplete (environment termination) | Reached 65% with no recorded test failure before SIGTERM 15; it is not a pass |
| Exact current-byte M30/supply-chain selection | pass | 52 passed, 158 deselected after the final trust/clock/byte-revalidation hardening |
| Specific qsp3/bootstrap/acceptance selection | pass | 16 passed after M26/target-bound-plan remediation |
| Broad qsp3 selection | pass | 74 passed |
| M26/governed execution/recipes/qsp3 selection | pass | 96 passed |
| Streamlit copy-first selection | pass | 4 passed, including purge after managed target rotation |
| Consolidated M26/qsp3/M32 selection | pass | 124 passed in 6.82 seconds |
| Schema-v2 M30 readiness/campaign selection | pass | 103 passed in 89.65 seconds |
| `.venv/bin/pytest -q -k m30` | pass, superseded snapshot | 159 passed, 4,149 deselected in 279.25 seconds before the D137 closing-review corrections |
| Phase-1b policy unit selection | pass | 28 passed in 70.89 seconds; includes ancestor/leaf/target/destination races and no receipt/control accepted |
| Phase-1a authentication/CLI selection | pass | 53 passed in 96.45 seconds after anchored `gh` bounds, bounded subprocess cleanup and macOS canonical-temp repair |
| D137 corrected focal unit/acceptance cut | pass | 63 passed in 128.33 seconds after final path/JSON/temp/Git-index regressions; no receipt/control accepted |
| D137 focal format/Ruff/Mypy/diff | pass | Adapter and adversarial test format/lint pass; strict adapter Mypy and `git diff --check` pass |
| Independent D137 review | blocked for commercial authority | P0=0; final-read and JSON-last P2 findings fixed. One conditional P1 remains for same-UID `Popen`/`gh` pathname ABA and requires isolated evaluator or reviewed fd-input/fd-exec consumption |
| D138 GitHub helper regressions | pass | 6 current regressions cover exact limits, stdout/stderr overflow, timeout, missing-pipe and selector-failure cleanup |
| D138 Git regressions | pass | 4 current regressions cover descendant-held stdout timeout, pre-buffer overflow, missing-pipe and selector-failure cleanup; the timeout case also passed eight repeated isolated runs on the earlier closing snapshot |
| D138 two-module cut | pass, independent review | 101 passed in 106.34 seconds: 40 campaign CLI/failure tests plus 61 readiness tests; reviewers also exercised limits, descendant groups and descriptor stability |
| D138 focal format/Ruff/Mypy/diff | pass | Four changed Python files are formatted; Ruff, strict Mypy over both adapters and `git diff --check` pass |
| Independent D138 reviews | pass | Both reviews report P0/P1/P2/P3=0 for the current bounded-subprocess deltas; POSIX-only and same-UID/mount D137 deployment limits remain explicit |
| Current `.venv/bin/pytest -q -k m30` | pass | 174 passed, 4,149 deselected in 286.66 seconds after D138 and all closing-review regressions |
| Final implementation-tree `make check` | pass | Supply-chain/release audit, 745-file formatting/Ruff, strict Mypy over 366 sources, performance and 4,073 functional tests passed with 250 deselections in 1,272.00 seconds; dirty-tree warning expected before commit |
| Final D138 documentation closure | pass | Four commercial-documentation contracts, strict Mypy over both adapters, format/Ruff over the four changed Python files and `git diff --check` pass on the final pre-commit tree |
| Qsp3/contract focal format, Ruff and mypy | pass | Seven qsp3 source files plus the contract model pass |
| Exact `7dea1a4` documentation patch `git diff --check` | pass | Historical six-file documentation/state correction; no whitespace errors |
| Codex in-app-browser local/recorded subset | bounded pre-final observation | Advanced desktop, 390×844 no-overflow and `date_meaning`; clean console; not managed/final-byte evidence |
| Final Codex in-app-browser retry | unavailable | Runtime connected, but browser inventory was empty; no substitute browser used and no PASS claimed |
| Final implementation-snapshot `make check` | pass | Supply-chain/release audit, Ruff, Mypy over 366 source files, performance and 4,044 functional tests pass; 250 deselected in 1,176.63 seconds |
| Clean-room qsp3-parent `make check`, first attempt | fail (transient timeout) | Exact `785a052`: 4,011 passed, one Streamlit AppTest exceeded 15 seconds and 249 were deselected in 1,154.17 seconds while another local full gate consumed CPU |
| Timed-out Streamlit AppTest, isolated rerun | pass | 1 passed in 2.01 seconds without a code or assertion change |
| Clean-room qsp3-parent `make check`, isolated rerun | pass | Exact `785a052`: 4,012 passed and 249 deselected in 934.84 seconds; supply chain, release audit, Ruff, Mypy over 364 files and performance pass |
| Exact Phase-1b focal unit/acceptance cut | pass | Exact `23dea0f`: 33 passed in 56.51 seconds; preparation only, no receipt/control accepted |
| Exact `23dea0f` clean-room `make check` | pass | Exact `23dea0f`: 4,044 passed and 250 deselected in 1,023.35 seconds; supply chain, release audit over 1,060 files/23 licenses, Ruff over 745 files, Mypy over 366 source files and performance pass |
| Exact `23dea0f` independent review | pass with known P2 | `23dea0f` composed surface P0=0/P1=0; adjudication/capabilities remain uncomposed and pathname-race hardening remains required |
| Final documentation/readiness contract selection | pass | 62 passed; commercial documentation and M30 readiness contracts cover the final evidence wording |
| Final current-tree `make m30-readiness` | pass with explicit NO-GO | `blocked_prerequisites`; 41 source paths, 3 failed repository gates, 1 passed repository gate, 24 `missing_external` controls, zero source/DataHub writes, database or network calls, `campaign_executable=false`, `release_decision=no_go` |
| Exact D139 lock refresh and hashed export | pass | Only `cryptography` changed 49.0.0 → 50.0.0; runtime export SHA-256 `e106fa...719a`, `uv.lock` SHA-256 `9ff9a7...9f85` |
| `pip check` plus exact hashed `pip-audit` exports | pass | No broken requirements and no known vulnerabilities; installed `cryptography==50.0.0` |
| Targeted auth/OIDC/readiness/supply-chain Pytest cut | pass | 277 passed in 10.60 seconds |
| D139 `make supply-chain-static` | pass | Supply-chain policy and release audit pass over 1,060 candidate files and 23 licenses |
| D139 `make runtime-wheel-smoke` | pass | Clean wheel install validates migrations 1–15, runtime entrypoints and `cryptography==50.0.0` |
| D139 exact implementation-tree `make check` | pass | Supply-chain/release audit, 745-file format/Ruff, strict Mypy over 366 sources, performance and 4,073 functional tests pass with 250 deselections in 909.30 seconds |
| Clean `make m30-readiness` on `664b90f` | pass with explicit NO-GO | Clean-tree/source-contract pass; branch/tag fail; 24 controls `missing_external`; zero writes/database/network calls; `campaign_executable=false`, `release_decision=no_go` |
| Exact-`664b90f` Codex in-app-browser regression | bounded local/recorded pass | Desktop simple 25-line and advanced 106-line paths, ambiguity and unsupported fail-closed, 390×844 iframe no-overflow layout/navigation and empty warning/error logs; no managed target or execution |
| D139 documentation/readiness/release/supply-chain selection | pass twice | 242 passed in 36.81 seconds after the browser, decision, state and handoff update; closing rerun passed 242 in 36.82 seconds after recording the full gate |
| Final D139 evidence-tree `make check` | pass | Supply-chain/release audit, 745-file format/Ruff, strict Mypy over 366 sources, performance and 4,073 functional tests pass with 250 deselections in 946.75 seconds; dirty-tree warning is expected before the evidence commit |

## Automated test results

- Focused tests: the recorded Phase-1a cuts pass: 243 in the combined
  M30/commercial/supply-chain selection, 19 in the final verifier/CLI/workflow selection, 4
  commercial-documentation contracts and the final corpus mutation regression. Current qsp3 cuts
  pass 16 specific, 74 broad, 96 with M26/governed execution/recipes, four Streamlit and 124 in the
  consolidated selection. Schema-v2 M30 contract/readiness/campaign passes 103; the current M30
  cut passes 174 on the 4,323-case snapshot, the current policy/authentication cuts pass 28/53 and
  the current readiness module passes 61.
- `make check`: exact commit `23dea0f` passes a dedicated clean-room run: supply-chain/release
  audit, formatting, Ruff, strict Mypy over 366 source files, the isolated performance node and
  4,044 functional tests with 250 deselections in 1,023.35 seconds. The later documentation-only
  evidence correction receives dedicated documentation/readiness/diff checks. Current D137/D138
  implementation passes 174 M30 tests and the full local gate: supply-chain/release audit,
  formatting, Ruff, strict Mypy, performance and 4,073 functional tests with 250 deselections.
  PR CI executes a merge ref; an exact clean tagged-main production candidate remains absent.
- Integration tests: Phase 1a adds no service adapter. Qsp3 composes the governed execution-target
  resolver in managed mode and exercises exact synthetic registry-v2 target contracts, but no
  operated destination receipt exists; old hosted PostgreSQL evidence for a prior PR merge subject
  is not accepted for this candidate.
- Acceptance tests: Phase-1a clean synthetic subjects authenticate bytes while execution/release
  remain blocked. The qsp3 acceptance cuts prove exact target propagation and `executed=false`;
  unavailable, substituted, cross-connection, semantic-stale and route-drift states return no SQL.
- Coverage: not rerun for this vertical; no new coverage claim.
- Browser: exact hotfix commit `664b90f` completed the local/recorded in-app-browser regression.
  Desktop covered the 25-line simple and 106-line advanced standalone paths, pre-confirmation
  no-SQL, `executed=false`, `date_meaning` and `unsupported_request`; both blocked cases returned no
  SQL. A real 390×844 same-engine frame had `clientWidth=scrollWidth=390` and empty warning/error
  logs. Direct viewport override was ignored by the browser runtime and nested-frame text injection
  was not used, so the mobile evidence is layout/navigation only. Managed rotation remains AppTest
  evidence. A managed/operated browser PASS and integrated commercial surface remain absent.

## Operator manual test

The qsp3 exact-hotfix local/recorded bounded subset is complete; managed/operated verification remains
**PENDING**:

- observed on `664b90f`: no SQL/download before confirmation; 25-line simple and 106-line advanced
  standalone output, visible download, optional execution disabled, `Ejecutado=No`, `Sin ligar`/
  non-commercial after confirmation; `date_meaning` and `unsupported_request` had no SQL; desktop
  had no overflow and browser warning/error logs were empty;
- responsive: an actual 390×844 same-engine iframe rendered Query Studio, loaded the demo through
  keyboard activation and retained exact 390-pixel document/main scroll widths. Because the
  browser's direct viewport override and nested-frame text injection were unreliable, this proves
  layout/navigation only, not a mobile SQL-generation or accessibility PASS;
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

Phase-1b policy preparation — also not a receipt or dispatch authority:

1. Obtain the authenticated Phase-1a manifest/bundle plus canonical policy from the future
   independent authority, all outside the checkout in owner-owned directories that are not
   group/other writable. Inputs must be owner-owned, single-link regular files without
   group/other write permission.
2. Run `make m30-control-policy-schema` for the structural schema, then
   `make m30-control-policy-validate` with `M30_MANIFEST`, `M30_ATTESTATION_BUNDLE` and
   `M30_CONTROL_POLICY` set to those exact files.
3. Inspect both reports and require policy bound true, external trust/authentication false, 0/24,
   `campaign_executable=false` and `no_go`. Any claim that this passes a control is invalid.
4. Use a new output directory or an existing owner-owned mode-`0700` directory. Pre-D137 `0755`
   report directories require explicit verified migration; interrupted external hard-link
   publication is quarantined rather than silently cleaned.

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
- Receipt trust: Phase 1b composes policy validation only. The pure contract-level receipt model/
  adjudicator has no port, adapter or CLI path; caller-built authentication literals and signed
  booleans are not operated evidence and cannot enable a capability.
- Secrets/proprietary data: workflow input permits only public opaque IDs, versions and digests;
  raw cases, answer key, prompts, SQL, rows, credentials and protected topology are forbidden.
- Fanout/semantic risks: the existing one-connection/three-table/two-join boundary is frozen; exact
  language/family/risk slices prevent aggregate scores from hiding a missing critical family.
- Independent review: qsp3 reports P0=0/P1=0. D137 closes external in-operation checked-pathname
  redirection, final-read ordering and JSON-last repair defects with dirfd/openat hardening. A
  same-UID process can still target the private path consumed by the verifier subprocess; this is
  a conditional P1 before commercial authority. A hostile mount authority can also bind a checkout
  subdirectory directly under an external path. Both require isolation rather than a local `Path`
  assertion. The four
  official GitHub CLI 2.96.0 archive/binary digests were checked against the release artifacts, while the local
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
- Decision: publish only the remediated canonical policy-preparation boundary; retain receipt
  adjudication and every capability as uncomposed.
- Reason: the binding/DAG/report are useful and fail closed after remediation, while independent
  trust, raw measurement, replay safety and filesystem authority are still absent.
- Logged in: D135, which supersedes D134 only for policy validation.
- Decision: replace external checked-pathname evidence/report I/O with fail-closed descriptor
  anchoring, but keep local files provisional and non-authoritative.
- Reason: held dirfds, stable metadata, nonblocking exact reads and same-dirfd publication close
  redirect/block/overwrite races without pretending portable POSIX state defeats a hostile
  same-UID verifier process or mount administrator.
- Logged in: D137; it changes no trust fact, control result, capability or release decision.
- Decision: enforce Git and GitHub CLI output ceilings during streaming, signal each isolated
  process group and synchronously reap its direct child on every exceptional exit.
- Reason: a post-`communicate()` size check allowed unbounded allocation before rejection, and the
  candidate-inspection cleanup needed the same directly testable timeout/descriptor contract.
- Logged in: D138; it changes no trust fact, control result, capability or release decision.

## Known limitations or unverified items

- GitHub `main` is unprotected; there are zero environments, rulesets and tags. The required
  attestation environment, reviewer/self-review/bypass policy and eligible-tag protection do not
  exist.
- No exact clean tagged candidate, final-candidate hosted CI/SBOM/provenance, authenticated Phase-1a
  manifest or control receipt exists.
- Phase-1b policy preparation is implemented, but receipt authentication/adjudication remains
  uncomposed. It needs an independently controlled trust root, cryptographic verifier over
  canonical raw snapshots, trusted time and a durable compare-and-swap replay ledger before any
  deterministic control-specific adjudicator can be accepted.
- External evidence/report I/O is descriptor-anchored, but the private executable and snapshot
  names consumed by `Popen`/`gh` remain replaceable by a malicious co-tenant with the same UID.
  This cannot enable a current control/capability because the surface is fixed at 0/24; it becomes
  a blocking conditional P1 before any receipt or release authority and requires a dedicated
  non-co-tenant evaluator boundary.
- Portable `openat`/`O_NOFOLLOW` does not uniformly detect hostile bind mounts or ACL authority. A
  reviewed mount namespace/ACL policy, independently owned read-only inputs and authenticated
  append-only/CAS retention remain mandatory before commercial evidence consumption.
- The 1,000-case blind corpus, exact provider/target operation, equivalence, scale/soak,
  browser/accessibility, IAM/network/secrets/SIEM/restore, pentest and owner decisions are absent.
- The exact-`664b90f` local/recorded browser regression and post-remediation AppTest are not a
  managed/operated browser PASS. Exact `23dea0f` passes its historical clean gate; the current
  D137/D138 implementation passes its 174-test M30 cut, and D139 passes the 4,073-test full local
  gate. Managed browser evidence remains required before M30 acceptance.
- PostgreSQL is the only output dialect. Arbitrary SQL, cross-database portability, federation,
  more than three tables/two joins and unsupported families are not certified.
- Managed staging/production now rejects an absent or stale target binding. Local/recorded mode may
  still emit `target_fingerprint=None` only with an explicit non-commercial label; it cannot be
  promoted by manually checking the destination. Operated destination certification, the
  commercial OpenAPI/integrated console, offboarding automation, legal/privacy/support/pricing and
  real pilot remain incomplete.

## Blockers

- External GitHub protection and independent attestation authority.
- Independent receipt trust bundle, isolated verifier, raw-measurement derivation and CAS
  anti-replay ledger.
- Managed/operated qsp3 browser matrix and exact clean tagged-main candidate CI.
- Operated M29 prerequisites and all 24 M30 hosted/target/third-party/owner controls.
- M30 corpus/security campaign and M31 design-partner pilot.

## Next milestone readiness

- Dependencies satisfied: M35 and M29 local baselines; Phase 0/1a, fail-closed Phase-1b policy
  preparation, descriptor-anchored local I/O and qsp3 target-binding implementation only.
- Recommended next prompt: first consolidate the final qsp3 managed browser matrix; then provision
  the external GitHub/target/assessor trust bundle and implement the isolated concrete receipt
  verifier plus CAS replay ledger before composing the first hosted adjudicator.
- Required operator prerequisites: protected GitHub controls, exact candidate/tag/artifacts,
  independently owned corpus/answer key, real isolated target, pentest assessor and distinct
  product/semantic/security/operations/release approvers.
