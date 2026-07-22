# SchemaBridge planning/orchestration prompt

Use this prompt only to inspect project status and recommend the next milestone. Do not implement multiple milestones.

Read `AGENTS.md`, `plans/MASTER_PLAN.md`, `tasks/PROJECT_STATE.md`, `tasks/CURRENT_TASK.md`, and `tasks/DECISION_LOG.md`.

Return:

1. the last accepted milestone and evidence;
2. unresolved blockers or failed tests;
3. the next eligible milestone according to dependencies;
4. its recommended model/reasoning level;
5. the exact prompt filename to use;
6. any prerequisite command the operator must execute;
7. a go/no-go recommendation.

Do not edit files. Do not assume a milestone is complete merely because files exist; rely on recorded tests and operator acceptance.
