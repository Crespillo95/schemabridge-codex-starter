# M16 release audit

Audit date: 2026-07-22
Scope: M00–M15 release candidate, audited under M16 only
Decision: **NO-GO pending reviewed remediation commit, strict clean-room rerun, and final recorded manual journey**

This report is tracked; runtime evaluation JSON and logs remain ignored. The repository now has
`HEAD` `231187a`, but the fanout/audit remediation described below is still uncommitted. Its passing
development gates are therefore not yet strict release-commit evidence.

## Independent audit panel

Five bounded, read-only GPT-5.6 Sol/Ultra subagents reviewed independent surfaces and made no edits.
Each returned ranked findings, file/line evidence, reproduction commands, impact, and proposed
regressions.

| Audit | Highest verified finding | Main disposition |
|---|---|---|
| Architecture/domain boundaries | Medium: composition outside `bootstrap.py`, mutable lookup constants, incomplete global import checks | Fixed composition/immutability; added whole-tree AST regression |
| SQL/database security | High: unbounded rejected-source materialization; negative identifiers accepted by SQL interpreters | Fixed with bounded samples/exact totals and consistent negative rejection |
| DataHub/governance | High: cross-environment URNs and no unified per-target audit | Fixed environment isolation, restart proof, and common approval-bound target ledger |
| Tests/evaluation | High: CI could skip database-backed acceptance/evaluation; no release identity at audit time | CI DSN/evaluation fixed; current remediation still needs a reviewed commit |
| Judge experience/docs | Critical at audit time: no RC commit; stale disclosure/report | Docs/report fixed; current remediation commit and recorded operator journey remain |

The operator-readable ranked reports preserve each panel's evidence, reproduction, proposed
regression, and main-agent disposition:

- [Architecture and domain boundaries](m16-audits/architecture-domain.md)
- [SQL and database security](m16-audits/sql-database-security.md)
- [DataHub integration and governance](m16-audits/datahub-governance.md)
- [Tests and evaluation](m16-audits/tests-evaluation.md)
- [Judge experience and documentation](m16-audits/judge-documentation.md)

## Main-agent triage

Accepted findings were reproduced before editing. Related reports were deduplicated: the missing RC
commit appeared in three audits, and the ignored report appeared in two. The following challenged
findings were not treated as M16 implementation defects:

- M17 deployment/video and M18 final submission artifacts are explicitly later scope.
- Recorded/fake UI modes are acceptable because they are visibly labeled and never hidden fallback.
- The small synthetic metrics retain their false positive/negative and make no production claim.
- Missing lineage/query entities are typed missing evidence, not a fabricated signal.

The accepted governance finding `GOV-001` is now remediated: canonical, join, workflow, and recipe
writes return one common immutable record per target containing approval, actor/time, old/new
fingerprints, operation, decisions, outcome, and stable failure reason. The application validates
the adapter facts against the exact approval before an atomic append, and the ledger rejects an
approval ID reused with another identity. A fresh SQLite instance can query the records by approval
ID. A null old fingerprint now means “no prior fingerprint was verifiable,” never an inferred copy
of another target's marker. DataHub and SQLite still cannot form one distributed transaction, and
DataHub upserts expose no cross-client compare-and-swap; those residuals remain explicit below.
A supplemental adversarial pass also reproduced a workflow-specific false-success path: matching
payload fingerprints were accepted without reading back the embedded approval or target audit.
The workflow document now requires the exact workflow ID, fingerprints, approval actor/time/ID,
and validated successful per-target audit; post-write verification compares the exact audit record.
Workflow proposal hashes are recomputed by the domain model so a reused idempotency key cannot bind
different content.

## Accepted fixes and regressions

| Finding | Fix | Regression evidence |
|---|---|---|
| SQL-001 | Rejection SQL uses a hard `LIMIT`, `fetchmany`, exact window total, and `truncated` flag | `tests/unit/test_postgres_rejections.py` |
| SQL-002 / D013 | Compiler, relationship evidence, and rejection reporter reject negative identifiers | compiler, join, reporter unit regressions |
| SQL-004 / D017 | Preview adapter independently caps rows and timeout before connecting | forged-query unit regressions |
| SQL-007 | Relationship adapter verifies the observed timeout | focused adapter behavior and integration suite |
| DH-001 | Catalog rejects a DataHub dataset URN whose environment differs from configured `PROD` | `test_search_omits_assets_from_another_datahub_environment` |
| DH-006 | DataHub health selects exactly the `datahub` project GMS service | shell/static and live health checks |
| ARCH-002/003/004 | Composition moved to bootstrap; domain lookup constants frozen; global AST scanner added | `tests/unit/test_release_audit.py` |
| TE-001 | CI exports both database DSNs, runs integration/acceptance via Make, and runs/asserts evaluation | workflow configuration plus local equivalent commands |
| JDG-002/004/005/007 | Judge quickstart, disclosure, tracked report, and fanout story corrected | release scans and link checks |
| DH-007 | Fresh DataHub admin and writer provisioning now waits through the pinned GMS policy-cache window and fails closed on unexpected privileges | unit policy regressions plus clean-reset live provisioning |
| DH-008 | Canonical, join, and recipe writers independently ensure the decision structured property after approval | focused helper regression plus fresh-order live publication |
| TE-009 | The execution-derived query recipe and validation example were regenerated after negative identifiers became a fail-closed compiler rule | fixture validation and the complete acceptance suite |
| GOV-001 / DH-002 | Added one required approval-bound per-target audit value and append-only SQLite ledger across all four publication families | unit binding/collision/partial-retry tests plus live fresh-process canonical, join, workflow, and recipe read-back |
| DH-010 | Workflow current-state and post-write checks require the exact embedded approval and validated target audit; proposal hashes are self-validating and a conflicting deterministic target is never overwritten | missing-evidence, reused-key, and immutable-conflict unit regressions plus live workflow replay |
| FAN-001 | Cardinality and mitigation are evaluated in scan direction; reverse one-to-many preserves relationship `COUNT`, reverse many-to-one fails closed without an inverse policy | unit resolver/IR regressions and live PostgreSQL result `6` rather than incorrect `5` |
| FAN-002 | Automatic `COUNT DISTINCT` is limited to the exact approved one-side key; unsafe downstream aggregates fail closed while `COUNT DISTINCT`/`MIN`/`MAX` remain duplication-invariant | resolver and forged-IR regressions over attributes and three-table paths |
| IR-001 | Every physical join predicate, normalization plan, scan direction, and reversed `LEFT` constraint must match its approved contract | forged predicate/transformation/direction unit regressions |
| DH-005 | Canonical, join, and recipe version targets reject a different valid fingerprint before mutation; join/recipe writes require target read-back before reporting success | unit conflict/no-op/read-back regressions plus live canonical retry/conflict coverage |
| SQL-008 | Parameterized normalized dimensions group/order by their identical validated projection ordinal | compiler regression and live holder-role result `PRIMARY=1`, `SECONDARY=8` |

## Clean-room proof

The strict command is:

```bash
make release-clean
```

It refuses a missing/dirty `HEAD`. For this explicitly uncommitted development audit only, the
verified equivalent is:

```bash
bash scripts/release_clean_room.sh --allow-uncommitted
```

The development command completed from zero state on 2026-07-22. It removed and recreated `.venv`,
installed all release extras, reset the named PostgreSQL and DataHub volumes, provisioned identities,
ingested 61 metadata events, and then produced this evidence:

| Stage | Verified result |
|---|---|
| Install | Python 3.13.13; editable complete extras; `pip check` passed |
| PostgreSQL | PostgreSQL 16.13; reader identity, default read-only mode, and 5000 ms timeout passed |
| DataHub | Core v1.6.0; CLI 1.6.0.15; five datasets/profiles; logical UI; scoped writer; MCP search/schema reads; no MCP mutations |
| Quality | Ruff format/lint passed over 182 files; strict mypy passed over 110 source files; 274 unit tests passed |
| Integration | 26 passed; the fresh publication order was recipe, relationships, then canonical metadata |
| Acceptance | 7 passed, including guided, natural-language, workflow, context reuse, evaluation, and Streamlit paths |
| Evaluation | deterministic suite passed; live LLM remained explicitly `not_run` |
| UI | headless Streamlit health smoke passed |
| Persistence | DataHub stop/start preserved catalog, MCP reads, and all three governed write-back families; 3 focused read-backs passed |
| Release scan | 314 candidate files passed in the uninterrupted run; the final post-report scan passed 319 files, 19 direct dependency licenses, and 20 external links |

Two earlier clean-reset attempts exposed, rather than concealed, the GMS authorization-cache race
and publication-property ordering dependency. Regressions were added, both defects were fixed, and
the table above comes from the subsequent uninterrupted zero-state run.

At the time of that clean-room run, strict `make release-audit` and `make release-clean` both failed
closed because `HEAD` did not yet exist. The repository now has `HEAD` `231187a`, but the remediation
in this report is newer and uncommitted.

Post-audit remediation was then verified without resetting the already healthy synthetic services:

| Stage | Current development result |
|---|---|
| Quality | Ruff passed over 187 files; strict mypy passed over 113 source files; 310 unit tests passed |
| Integration | 29 passed against PostgreSQL/DataHub, including reverse fanout count, parameterized grouping, immutable conflicts, and fresh-process audit read-back |
| Acceptance | 7 passed, including live workflow publication audit read-back |
| Audit robustness | Approval binding/collision, partial failure, actual per-target canonical/join retry, exact workflow approval/audit read-back, proposal-hash integrity, post-write read-back, and append/reopen regressions passed |
| Candidate scan | Development scan passed 324 files, 19 direct licenses, and 20 external links; strict scan fails only because the tree is dirty |

These results establish the behavior of the working tree, not a strict release candidate. The
operator must review/commit it and rerun `make release-clean` before release.

## Unresolved-risk register

| ID | Severity | Risk / limitation | Release treatment |
|---|---|---|---|
| RC-002 | High | Current fanout/audit remediation is not committed and has not run in strict clean-room mode | **Blocks release**; operator must review/commit, then run `make release-clean` |
| UX-001 | High/manual | Live browser journey passed at 1440×1000 and 1024×900, but timing/recording and second-person challenge are absent | **Blocks release** until operator evidence is recorded |
| GOV-002 | Medium | DataHub mutation and SQLite ledger append cannot share one distributed transaction | A ledger failure is typed and retryable/reconcilable; add a durable outbox/reconciler before production-grade guarantees |
| GOV-003 | Medium | DataHub document upserts offer no compare-and-swap, leaving a narrow concurrent-writer race after immutable-version preflight | Keep the scoped publisher single-writer/serialized; move version URNs to content-addressed IDs or add CAS when supported |
| GOV-004 | Low | Structured-property and logical-parent aspects cannot persist a publication fingerprint | Their exact state is re-read and re-ensured; audit old fingerprint is explicitly null rather than fabricated |
| SQL-003 | Medium | Compiler applies the float exactness cap to a cast step without physical-type provenance, rejecting exact very large integers | Fail-closed undercount risk; add typed physical provenance before widening |
| SQL-005 | Medium | Guard does not reject every unsupported AST shape or encode exact approved join edges | Current compiler cannot emit them; extend independent defense before broader compiler scope |
| SQL-006 | Medium | A deliberately misconfigured expected username can bless an admin DSN | Default demo role is least-privilege and ACL-tested; add capability probe |
| DH-004 | Medium | Stock DataHub all-users policy grants the MCP identity personal-token generation | MCP mutation tools stay disabled; document/remove upstream grant when supported |
| DH-009 | Low | A newly minted one-month writer token could remain valid but unsaved if terminal policy verification fails | Local state is never written on failure; revoke the scoped token manually after a terminal provisioning failure |
| TE-003 | Medium | Result-correctness denominator includes non-executed query cases | Failures remain visible/blocking; align documented denominator |
| TE-004 | Medium | Zero-denominator evaluation metrics raise before report output | Current fixture denominators are nonzero; add typed undefined/skipped metric |
| TE-005 | Medium | Configured 80% coverage threshold is not part of `make check`; observed aggregate is below it | Do not claim coverage gate; add failure-path tests before enabling |
| TE-006 | Medium | Python dependencies use bounded ranges rather than a lock/constraints file | Release audit records exact installed versions; add reviewed constraints |
| ARCH-001 | Medium | Some CLI translation still constructs typed approval/decision values | No adapter/business-rule construction remains; keep entrypoint surface under review |
| ARCH-005 | Low | Doctor application use case performs bounded stdlib environment inspection | Isolated diagnostic exception; no domain/external-system dependency |
| TE-008 | Low | Duplicate result-column normalization would collapse a cell | Guard currently rejects duplicate aliases; add model-level uniqueness defense |
| ENV-001 | Low/manual | PowerShell and standalone Codex CLI remain unverified on this Mac | Do not claim Windows/CLI verification |
| ENV-002 | Medium/manual | Only 5.7 GiB remained on the Docker host after the final run, below DataHub's documented clean-start recommendation | Final reset passed, but free space must be checked before another reset; no unrelated images or volumes were pruned |
| JDG-008 | Low | One recipe partial-failure log can name the publication target too broadly | Typed per-item results remain correct; tighten the operator-facing label in a later maintenance change |

## Architecture and security conclusion

- No verified domain/application inward dependency violation remains after the accepted fixes.
- Source PostgreSQL operations remain read-only and use the least-privilege synthetic reader.
- LLM output remains typed intent/explanation only; deterministic compiler output still passes the
  independent SQL guard before preview.
- No secret, private key, `.env`, virtual environment, cache, runtime token, or proprietary sample
  is intended for the candidate set.
- Project license is Apache-2.0; the reproducible scanner inventories installed direct dependency
  licenses and checks local/external links.

## Go/no-go

**NO-GO for release; core application behavior is green.** GOV-001, workflow target-audit
read-back, proposal-hash integrity, fanout orientation/grain, contract-bound joins, immutable
preflight, and post-write verification are fixed and all current development suites pass. M16 still cannot
claim release evidence until the operator reviews/commits this working tree, reruns strict
`make release-clean`, and records the remaining manual acceptance. No commit was created
automatically.
