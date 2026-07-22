# M16 independent audit: judge experience and documentation

Mode: bounded read-only review; the subagent made no edits.
Snapshot: pre-fix M16 working tree, 2026-07-21.
Owner of dispositions: main M16 agent.

## Ranked findings

### JDG-001 — Critical — No release-candidate commit

- Evidence: no commit or remote; durable state already said the initial commit was pending.
- Reproduction: `git log -1 --oneline; git remote -v; git status --short`.
- Impact: no immutable release source, clean-checkout proof, or revision-bound evaluation.
- Proposed test: create/review the RC commit, require a clean tree, regenerate artifacts, and assert
  their revision equals `git rev-parse HEAD`.
- Disposition: accepted as `RC-001`; it blocks release and no commit was created automatically.

### JDG-002 — High — README did not provide the implemented judge path

- Evidence: it described a scaffold and stopped before launching/labelling the UI.
- Reproduction: follow only the former README from a fresh shell.
- Impact: a judge could not reach the product or distinguish fake/recorded/live boundaries.
- Proposed test: README-only run through the three primary UI actions and expected `2, 1, 1`.
- Disposition: accepted and fixed in `README.md:5-86`.

### JDG-003 — High — Strong DataHub claims lacked an honest boundary matrix

- Evidence: browser evidence used recorded catalog/planning, deterministic fake intent, live
  synthetic PostgreSQL, and fake publication; lineage/query context remains absent.
- Reproduction: compare README/evaluation-alignment claims with
  `docs/14_BROWSER_ACCEPTANCE.md` and `tasks/PROJECT_STATE.md` limitations.
- Impact: a visible demo could be mistaken for complete live DataHub semantic reconstruction.
- Proposed test: record a separate live DataHub before/after publication/reuse flow.
- Disposition: accepted as documentation work. README now labels the default/live matrix; the clean
  live browser recording remains blocker `UX-001`.

### JDG-004 — High — Hackathon disclosure was unfinished

- Evidence: it described intended rather than actual Codex use and retained asset/license TODOs.
- Reproduction: `rg -n 'intended|TODO|Before submission' HACKATHON_DISCLOSURE.md`.
- Impact: provenance and IP/compliance ambiguity.
- Proposed test: require no placeholders and inventory actual tools, dates, dependencies, assets,
  and licenses.
- Disposition: accepted and fixed in `HACKATHON_DISCLOSURE.md`.

### JDG-005 — High — Required release audit was ignored

- Evidence: `.gitignore` ignored all of `reports/`.
- Reproduction: `git check-ignore -v reports/release-audit.md`.
- Impact: the risk register could be absent from the submitted repository.
- Proposed test: require the report in the candidate scanner.
- Disposition: accepted and fixed with narrow tracked exceptions.

### JDG-006 — High for submission, later milestone — Final media/package is incomplete

- Evidence: M17/M18 plans own deployment, video, Devpost copy, and the final artifact bundle.
- Reproduction: compare `examples/` with those plan deliverables.
- Impact: submission is not complete even if M16 code is healthy.
- Proposed test: regenerate all declared artifacts from the clean release commit.
- Disposition: challenged as an M16 implementation defect; retained as later-scope readiness work.

### JDG-007 — High — Fanout story credited an automatic choice already present in intent

- Evidence: the request already used distinct count while copy said SchemaBridge selected it.
- Reproduction: compare the former README/demo story with planner control behavior.
- Impact: misleading flagship safety claim.
- Proposed test: show plain Customer count mitigated to distinct and the relationship-count control.
- Disposition: accepted; current README/demo copy says the approved contract enforces/validates the
  mitigation and retains the `3, 1, 1` relationship control.

### JDG-008 — Medium — DataHub/architecture docs described completed work as future

- Evidence: logical-model publication and bounded writer paths were called planned/deferred.
- Reproduction: search current docs for `planned`, `deferred`, and `enabled only` and compare code.
- Impact: judge/operator cannot distinguish implementation from roadmap.
- Proposed test: present-tense claim audit against adapters and commands.
- Disposition: accepted and corrected in architecture/DataHub docs.

### JDG-009 — Medium — Chronological runbook obscured the release path

- Evidence: the operator record is comprehensive but long and M16 had no single verified command.
- Reproduction: attempt a clean judge setup using only the former top-level material.
- Impact: prerequisites, destructive scope, and expected evidence were hard to find.
- Proposed test: run one concise clean-room command verbatim and record interventions.
- Disposition: accepted; `make release-clean` and its destructive scope are documented. Full timing
  from a clean committed checkout remains operator evidence.

### JDG-010 — Medium — English-language path was incomplete

- Evidence: the former main redirect led to a stale Spanish-only starter guide.
- Reproduction: follow the README as an English-only reviewer.
- Impact: avoidable accessibility friction.
- Proposed test: second-person English-only README journey.
- Disposition: README now contains the primary English path; second-person review remains manual.

### JDG-011 — Medium — No reproducible license inventory

- Evidence: Apache-2.0 existed, but direct dependency metadata was not scanned.
- Reproduction: enumerate `pyproject.toml` dependencies and installed license metadata.
- Impact: compliance evidence was incomplete, not evidence of an incompatible license.
- Proposed test: fail on missing/unknown direct dependency license metadata.
- Disposition: accepted and fixed by the M16 scanner; the final run inventoried 19 direct licenses.

## Challenged false positives

- Recorded/fake modes are acceptable when explicitly labelled and never hidden fallback.
- Small synthetic evaluation metrics retain the false positive/negative and make no production claim.
- Optional live LLM evaluation remains explicitly `not_run` and is not mixed with deterministic data.
- A `NOTICE` file is not automatically required merely because dependencies are installed.

## Conclusion

The judge path and claims are materially clearer and scanned. Missing release identity and the
operator's clean live journey remain explicit blockers; later deployment/submission work was not
pulled into M16.
