# Codex operating model

## Model strategy

| Work type | Model | Reasoning |
|---|---|---|
| Routine scaffold, docs, UI wiring | GPT-5.6 Sol | High |
| Domain invariants and transformation algebra | GPT-5.6 Sol | Extra High |
| SQL compiler, guard, and threat cases | GPT-5.6 Sol | Extra High |
| DataHub integration and logical-model write-back | GPT-5.6 Sol | Extra High |
| Matching, join discovery, intent parsing | GPT-5.6 Sol | Extra High |
| End-to-end multi-perspective audit | GPT-5.6 Sol | Ultra |

Use the lowest level that reliably satisfies the acceptance criteria. Ultra is reserved for M16 because independent architecture, security, DataHub, testing, and submission reviews can run in parallel.

## Context management

- Start one fresh Codex chat per milestone.
- Durable decisions live under `tasks/` and `docs/adr/`.
- Root and nested `AGENTS.md` files enforce boundaries.
- The local skill defines the repeated milestone method.
- Prompts point to files rather than duplicating the complete project specification.
- Commit after each accepted milestone.

## Codex permissions

Project defaults use:

```toml
approval_policy = "on-request"
sandbox_mode = "workspace-write"
web_search = "cached"
```

The user approves package installation, Docker operations, credentials, DataHub mutations, and anything outside the workspace. Do not switch to unrestricted access merely for convenience.

## MCP workflow

M04 enabled the pinned local DataHub MCP server only after its service-account search and
schema-field checks passed. The project wrapper loads ignored local credentials and forces mutation,
document-save, and document-search switches off. MCP write tools remain unavailable until the
explicit write-back milestone, where both product approval and Codex write approval are required.

## Prompt structure

Each milestone prompt requires Codex to:

1. inspect before editing;
2. give a concise plan;
3. implement only the stated scope;
4. add tests and run quality gates;
5. review security and architecture;
6. update durable state;
7. return a standardized handoff.

## User test loop

Codex performs automated work. The user executes the manual checklist and supplies unedited failures. A milestone is accepted only after both automated and manual checks pass.

## When to use subagents

Good uses:

- read-only codebase inspection;
- independent security and architecture audits;
- test-log diagnosis;
- documentation/evaluation review;
- M16 release audit.

Avoid concurrent writes to the same modules. The main agent owns integration and final decisions.

## Recovery rules

When a chat becomes confused or broad:

1. stop it;
2. discard no user work;
3. run `git status` and inspect the diff;
4. update `tasks/PROJECT_STATE.md` with the actual state;
5. open a fresh chat with a narrow repair prompt;
6. add a regression test for any discovered defect.
