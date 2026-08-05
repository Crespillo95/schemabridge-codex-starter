# Milestone handoff

## Summary

- Milestone: M18 — README, examples, video, and Devpost submission package
- Status: partial — local candidate and DataHub evidence pass; immutable release/publication pending
- Recommended operator decision: needs manual test
- Proposed commit message: `release: harden M18 judge candidate`

## Implemented

- Hardened the public recorded Docker surface, dependency installation, Streamlit privacy/error
  presentation, English north-star copy, and judge-focused workflow.
- Extended the release audit across human-facing links and high-confidence Git-history secret
  patterns without printing candidate secret contents.
- Exercised the full recorded browser flow and the separate pinned DataHub Core v1.6.0 read,
  approval-gated write, read-back, restart, and reuse path using synthetic data only.
- Updated disclosure, runbooks, Devpost/video material, state, decisions, release packaging, generated
  development evidence, tests, and reviewed DataHub screenshots.
- Defined a two-commit release identity: immutable source/build/tag commit first; evidence-only commit
  generated from that clean source second.

## Files changed

- `Dockerfile`, `.streamlit/config.toml`, `scripts/package_huggingface_space.sh`: reproducible and
  privacy-reduced judge image/package.
- `src/schemabridge/application/ui_workflow.py`, `src/schemabridge/bootstrap.py`,
  `src/schemabridge/entrypoints/streamlit/`: exact English request and focused judge workflow.
- `scripts/release_audit.py`, `Makefile`: external-link and Git-history release auditing.
- `tests/`: release-audit, web/UI, exact-copy, workflow-focus, and isolated live-registry coverage.
- `README.md`, `HACKATHON_DISCLOSURE.md`, `docs/`, `plans/M18_SUBMISSION_PACKAGE.md`: judge story,
  operational truth, submission checklist, and reviewed DataHub evidence.
- `examples/`: regenerated development-only evaluation/package evidence; clean-release regeneration
  remains required.
- `tasks/CURRENT_TASK.md`, `tasks/PROJECT_STATE.md`, `tasks/DECISION_LOG.md`: current M18 state while
  preserving M30 commercial/production NO-GO.

## Commands executed

| Command | Result | Notes |
|---|---|---|
| `make judge-build` | pass | Linux AMD64 image; Python 3.13.13, Streamlit 1.60.0, SQLGlot 29.0.1 |
| `make judge-smoke` | pass | Local health and root page |
| `.venv/bin/pytest -m acceptance -k deployed` | pass | Deployed recorded flow |
| human browser north-star run | pass | Exact `2/1/1`; `127.5`, `NaN`, `NULL`; no developer popup |
| `make datahub-health` plus catalog/MCP/registry checks | pass | 11 datasets; read-only MCP; registry 7/31/5/37 |
| DataHub restart plus live acceptance | pass | Registry/reuse survived restart; 3 tests passed |
| `.venv/bin/pytest -m integration` | pass | 184 passed, 3 explicit skips |
| `.venv/bin/pytest -m acceptance` | pass | 66 passed, 1 explicit skip |
| `make evaluate` | pass | Small deterministic synthetic fixture only |
| `.venv/bin/pytest -q tests/unit/test_release_audit.py` | pass | 16 passed |
| release/UI/web focused selection | pass | 34 passed |
| `.venv/bin/python scripts/release_audit.py --check-external --check-history` | pass | 1,068 files, 23 licenses, 41 links, 39 revisions; dirty-tree warning only |
| `git diff --check` | pass | No whitespace errors |
| `make check` | pass | Static gates, isolated performance, then 4,080 passed/250 deselected in 898.48 s |
| first clean `make release-clean` at `7f298b4` | fail | 180 integration passed/3 skipped; 4 live-registry reads found the reset orchestration gap |
| corrected prepare/publish/read-back focal | pass | Exact audited publish; registry 7/31/5/37; affected integration 4 passed/1 IAM skip |
| corrected `make check` | pass | 4,082 passed/250 deselected in 950.53 s, including 2 clean-room ordering tests |
| corrected strict clean-room rerun | not run | Requires the superseding clean source commit |

## Automated test results

- Focused tests: release-audit 16 passed; affected release/UI/web selection 34 passed.
- `make check`: pass; supply-chain and release policy, format (745 files), Ruff, strict Mypy (366
  source files), one isolated performance test, and 4,080 functional tests passed with 250
  deselected in 898.48 seconds.
- Corrected `make check`: pass; 746 formatted files, Ruff, strict Mypy over 366 sources, isolated
  performance, and 4,082 functional tests passed with 250 deselected in 950.53 seconds.
- Integration tests: 184 passed, 3 explicit skips (historical superseded v1 fixture, separately
  provisioned M34 document-publisher IAM, unavailable retained M27 browser corpus).
- Clean-room integration on source `7f298b4`: 180 passed, 3 skipped, 4 failed because the fresh
  DataHub reset had not published the registry prerequisite. The corrected explicit prepare,
  approval-gated publish, read-back, and affected integration cut passes; full rerun is pending.
- Acceptance tests: 66 passed, 1 superseded historical skip; post-restart live cut 3 passed.
- Coverage, where applicable: the repository gate does not emit a coverage percentage in this
  configuration; correctness uses the complete selected functional suite plus the isolated
  performance node.

## Operator manual test

1. From the clean source commit, build the judge image and run the complete English north-star flow.
2. Publish that exact package to the public Hugging Face Docker Space and repeat it signed out from
   a second device/network.
3. Open every repository, example, demo, video, and Devpost link signed out and compare claims with
   the image, manifest, and video.
4. Ask an unfamiliar reviewer the five questions in `docs/15_SUBMISSION_CHECKLIST.md` using only the
   final README and video.

Expected result:

```text
The reviewer identifies semantic ambiguity, DataHub-governed mappings/joins, deterministic SQL
compilation and AST validation, the 2/1/1 distinct-customer result, and explicit invalid-key
rejections. The public demo needs no login or secrets and matches the recorded release identity.
```

## Architecture and security review

- Dependency direction: unchanged; Streamlit invokes application use cases through the composition
  root and no business rule moved into the entrypoint.
- Source database writes: none; public runtime contains no source database credential.
- SQL/LLM validation: the LLM does not emit executable SQL; typed intent, deterministic compiler,
  two independent AST checks, allowlists, limits, timeout, and fanout policy remain enforced.
- DataHub mutation approval: exact explicit approval, pseudonymous authenticated actor, immutable
  audit facts, read-back, and idempotent replay passed locally; public runtime uses fake labeled
  publication and carries no writer credential.
- Secrets/proprietary data: synthetic data only; candidate screenshots reviewed; release tree,
  direct dependency licenses, external links, and Git history scan pass on the dirty candidate.
- Fanout/semantic risks: one-to-many expansion requires approved `COUNT DISTINCT`; name similarity
  alone never establishes equivalence and ambiguity remains human-confirmed.

## Decisions made

- Decision: public hackathon service uses recorded synthetic catalog/result evidence while live
  DataHub proof remains a separate local pinned-stack artifact.
- Reason: a free public single-container service cannot honestly or safely embed DataHub/source
  credentials, yet judges still receive a stable functional path and inspectable real integration.
- Logged in: D141–D143 in `tasks/DECISION_LOG.md`.

## Known limitations or unverified items

- No public Hugging Face URL, public YouTube/Vimeo video, Devpost project URL, release tag, or final
  clean manifest exists yet.
- Final Streamlit screenshots must be recaptured from the immutable clean source image.
- Public signed-out/cold-start/second-network checks and unfamiliar-reviewer comprehension are not
  yet performed.
- M18 is not production acceptance: M30 remains 0/24 external controls and commercial/production
  NO-GO.

## Blockers

- Owner-scoped Hugging Face authentication is required to create/upload the public Space.
- Owner YouTube/Vimeo and Devpost sessions plus legal/eligibility attestations cannot be fabricated
  by repository automation.
- Release identity now depends on the superseding clean source commit and strict clean-release
  proof; `7f298b4` must not be tagged or deployed.

## Next milestone readiness

- Dependencies satisfied: local M18 source/UX/DataHub evidence passes and the first clean-room run
  exposed a now-remediated orchestration prerequisite; the superseding full gate/freeze remain.
- Recommended next prompt: continue M18 freeze, publish the exact Space/video, run signed-out human
  acceptance, and complete Devpost without changing executable source.
- Required operator prerequisites: scoped Hugging Face login, YouTube/Vimeo login, Devpost login,
  unfamiliar reviewer, and second network/device.
