# M16 independent audit: architecture and domain boundaries

Mode: bounded read-only review; the subagent made no edits.
Snapshot: pre-fix M16 working tree, 2026-07-21.
Owner of dispositions: main M16 agent.

## Ranked findings

### ARCH-001 — Medium — Composition existed outside `bootstrap.py`

- Evidence: CLI and Streamlit entrypoints constructed application collaborators instead of obtaining
  the complete graph from `src/schemabridge/bootstrap.py`.
- Reproduction: inspect concrete adapter/application constructor imports with
  `rg -n 'schemabridge\.adapters|Psycopg|DataHub' src/schemabridge/entrypoints` and compare them with
  the `AGENTS.md` composition-root rule.
- Risk: entrypoints could create a second dependency graph and bypass centrally configured limits.
- Proposed regression: scan every production entrypoint import and reject adapters or construction
  roots outside bootstrap.
- Disposition: accepted and fixed. The entrypoints now request configured services from
  `src/schemabridge/bootstrap.py`; the whole-tree AST rule is in `scripts/release_audit.py:334-359`.

### ARCH-002 — Medium — Mutable domain lookup constants

- Evidence: semantic token/reason lookup dictionaries in `domain/` were module-level mutable values.
- Reproduction: import the modules and attempt to assign/delete a lookup entry.
- Risk: process-local mutation could make deterministic scoring or rejection text order-dependent.
- Proposed regression: assert mutation raises and repeated calls retain identical output.
- Disposition: accepted and fixed with `MappingProxyType`/`frozenset` in
  `src/schemabridge/domain/candidates.py`, `joins.py`, and `transformations.py`.

### ARCH-003 — Medium — Architecture tests did not cover the full production tree

- Evidence: focused import assertions could miss a newly added domain/application module.
- Reproduction: add a temporary forbidden import to a previously unlisted production module and run
  the former focused selection.
- Risk: dependency direction could regress while ordinary behavior tests still pass.
- Proposed regression: discover and parse every Python file under `src/schemabridge`.
- Disposition: accepted and fixed by `scripts/release_audit.py:72-90`; synthetic violations are
  exercised in `tests/unit/test_release_audit.py:18-27`.

### ARCH-004 — Medium — Some CLI translation creates typed approval/decision values

- Evidence: approval primitives are translated into domain/application values in CLI handlers.
- Reproduction: `rg -n 'Approval|Decision' src/schemabridge/entrypoints/cli` and inspect whether each
  constructor only translates input or also establishes policy.
- Risk: future policy logic could drift into the presentation layer.
- Proposed regression: keep policy transitions in use cases and test entrypoints only as primitive
  input/output translators.
- Disposition: retained as `ARCH-001` in the unresolved-risk register. No concrete adapter or
  business-policy construction remains in an entrypoint.

### ARCH-005 — Low — Doctor performs bounded host inspection in application code

- Evidence: the doctor use case reads bounded stdlib environment/platform facts.
- Reproduction: inspect `src/schemabridge/application/doctor.py` imports and calls.
- Risk: this is a narrow exception to otherwise pure application orchestration.
- Proposed regression: prohibit framework/database/network imports and keep all checks read-only.
- Disposition: challenged as a release blocker and retained as a documented diagnostic exception.

## Conclusion

No verified domain/application dependency-direction violation remains. The release scanner checks
the full production tree; the retained items are medium/low design risks, not hidden pass claims.
