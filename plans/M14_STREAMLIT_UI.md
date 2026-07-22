# M14: Judge-ready Streamlit interface

- Status: planned
- Timebox: 4 hours
- Recommended Codex: GPT-5.6 Sol — High
- Dependencies: M06–M13

## Objective

Expose the full governed workflow in a clear professional UI centered on semantic evidence, joins, query plans, safety, and reusable decisions.

## Why this milestone exists now

Submission quality and a comprehensible demo are part of the judged product; the UI must reveal why the result is trustworthy.

## Deliverables

- Implement the navigation and pages specified in `docs/13_UI_SPEC.md`.
- Use application use cases/view models; no business rules or direct adapter construction in page callbacks.
- Add a deterministic “Load demo scenario” path and visible live/recorded/fake integration modes.
- Show interpretations, evidence, confidence, risks, transformations, approvals, join cardinality, fanout mitigation, SQL checks, results, and rejections.
- Add actionable error states, loading states, empty states, and connection health.
- Add lightweight UI/component tests plus a browser acceptance checklist and screenshots.

## Implementation sequence

1. Define presentation view models and navigation before page implementation.
2. Build the north-star happy path first across the minimum pages.
3. Add review/edit/approval controls with safe disabled states.
4. Add query plan, SQL, validation, result, and rejected-record panels.
5. Add decision history and context-reuse indication.
6. Run browser tests at common desktop widths and record clean screenshots.

## Acceptance criteria

- [ ] The north-star demo is reachable in fewer than six primary interactions after loading the scenario.
- [ ] The UI never permits execution while required ambiguity/mapping/join approvals are missing.
- [ ] Every result view exposes selected assets, join path, fanout mitigation, and validation status.
- [ ] Fake or recorded modes are labeled; no integration is misrepresented.
- [ ] Rejected rows and limitations are visible and downloadable.
- [ ] No token, connection password, stack trace, or chain-of-thought appears in the browser.

## Required automated checks

```bash
python -m pip install -e ".[dev,ui,postgres,sql,datahub,llm]"
pytest tests/unit -k "view_model or ui"
pytest -m acceptance -k streamlit
make check
streamlit run src/schemabridge/entrypoints/streamlit/app.py
git diff --check
```

Replace angle-bracket placeholders only when the actual environment has established the command. Never report an unexecuted placeholder as passing.

## Manual test for the operator

1. Perform the entire demo from a clean browser session while screen recording.
2. Test DataHub unavailable, source unavailable, invalid request, unapproved join, query timeout, and publication failure states.
3. Ask a second person to explain the product after using the UI without reading source code.

## Explicit non-goals

- Do not create a full BI dashboard builder, mobile app, or enterprise design system.
- Do not move domain logic into Streamlit session state.

## Handoff requirements

Update `tasks/PROJECT_STATE.md`, `tasks/CURRENT_TASK.md`, and `tasks/DECISION_LOG.md`. Return the format in `tasks/HANDOFF_TEMPLATE.md`, including exact commands, results, limitations, and the proposed commit message.
