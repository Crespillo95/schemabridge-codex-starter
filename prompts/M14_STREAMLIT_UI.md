# Codex milestone M14: Judge-ready Streamlit interface

Use the `$schemabridge-milestone` skill. Work only on **M14**. Recommended setting: **GPT-5.6 Sol — High**.

## Read first

1. `AGENTS.md` and all nested `AGENTS.md` files governing files you may edit.
2. `docs/00_PRODUCT_VISION.md`
3. `docs/01_SCOPE.md`
4. `docs/02_ARCHITECTURE.md`
5. `docs/06_SECURITY.md`
6. `plans/M14_STREAMLIT_UI.md`
7. `tasks/PROJECT_STATE.md`
8. `tasks/DECISION_LOG.md`

## Objective

Expose the full governed workflow in a clear professional UI centered on semantic evidence, joins, query plans, safety, and reusable decisions.

## Implementation contract

- Inspect the current repository and tests before changing anything.
- Start with a concise plan of at most 12 lines and name the exact files or modules you expect to touch.
- Implement the smallest complete vertical slice that satisfies the milestone; do not pull later milestones forward.
- Preserve ports-and-adapters dependency direction and all security invariants.
- Add focused tests before or with behavior. Do not weaken existing tests.
- Run every relevant command and then the full `make check` gate.
- Review the final diff for architecture drift, source writes, unvalidated model output, SQL risk, secrets, proprietary data, and misleading documentation.
- Update durable project state and return the standard handoff.

## Required deliverables

- Implement the navigation and pages specified in `docs/13_UI_SPEC.md`.
- Use application use cases/view models; no business rules or direct adapter construction in page callbacks.
- Add a deterministic “Load demo scenario” path and visible live/recorded/fake integration modes.
- Show interpretations, evidence, confidence, risks, transformations, approvals, join cardinality, fanout mitigation, SQL checks, results, and rejections.
- Add actionable error states, loading states, empty states, and connection health.
- Add lightweight UI/component tests plus a browser acceptance checklist and screenshots.

## Acceptance criteria

- [ ] The north-star demo is reachable in fewer than six primary interactions after loading the scenario.
- [ ] The UI never permits execution while required ambiguity/mapping/join approvals are missing.
- [ ] Every result view exposes selected assets, join path, fanout mitigation, and validation status.
- [ ] Fake or recorded modes are labeled; no integration is misrepresented.
- [ ] Rejected rows and limitations are visible and downloadable.
- [ ] No token, connection password, stack trace, or chain-of-thought appears in the browser.

## Expected checks

```bash
python -m pip install -e ".[dev,ui,postgres,sql,datahub,llm]"
pytest tests/unit -k "view_model or ui"
pytest -m acceptance -k streamlit
make check
streamlit run src/schemabridge/entrypoints/streamlit/app.py
git diff --check
```

If a listed command contains a placeholder, determine and document the verified command rather than pretending the placeholder ran.

## Do not do

- Do not create a full BI dashboard builder, mobile app, or enterprise design system.
- Do not move domain logic into Streamlit session state.

## Operator test to prepare

1. Perform the entire demo from a clean browser session while screen recording.
2. Test DataHub unavailable, source unavailable, invalid request, unapproved join, query timeout, and publication failure states.
3. Ask a second person to explain the product after using the UI without reading source code.

Do not commit automatically unless the operator explicitly asks. End using `tasks/HANDOFF_TEMPLATE.md` and include a single proposed commit message.
