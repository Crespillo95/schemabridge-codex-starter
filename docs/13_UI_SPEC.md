# UI specification

SchemaBridge should look like a professional semantic-governance and query-planning tool, not primarily like a chatbot.

## Navigation

```text
Overview | Semantic Models | Relationships | Query Studio | Decisions
```

## Overview

Show:

- datasets and fields scanned;
- candidate concepts;
- unresolved conflicts;
- high-risk type mismatches;
- approved models and joins;
- recent decisions;
- DataHub and source connection health.

Primary action: **Scan catalog**.

## Semantic Models

Three-column review layout:

```text
Physical fields | Canonical definition | Evidence and risks
```

For each field show asset, type, description, profile summary, samples only when safe, transformations, confidence, and evidence. Actions: approve, edit, reject, mark as different concept, validate, publish.

## Relationships

Graph/table of proposed model relationships. A detail panel shows:

- left/right model and fields;
- physical join expressions after normalization;
- evidence sources;
- value overlap;
- cardinality;
- duplicate/null rates;
- fanout policy;
- status and version.

## Query Studio

Two synchronized modes:

1. guided fields and operators;
2. natural-language request.

Both render an editable interpreted request. Before execution show:

- selected logical concepts;
- selected physical assets;
- approved mapping versions;
- join path and confidence;
- cardinality and fanout mitigation;
- assumptions/ambiguities;
- generated SQL;
- policy-check results.

Actions: edit interpretation, validate, preview, export SQL, save recipe, publish context.

## Validation result

Show:

- returned row count;
- scan/timeout information available to the adapter;
- validation findings;
- rejected source values and reasons;
- result table;
- downloadable SQL, query plan, and report.

## Decisions

Version history for models, field mappings, joins, and recipes. Show actor, time, evidence, changes, known risks, DataHub publication result, and linked assets.

## UX rules

- Do not hide ambiguity behind a confidence number.
- Red/yellow/green status must also have text/icons for accessibility.
- Disable execution until required approvals are satisfied.
- Keep the demo path reachable in fewer than six primary interactions after seed data exists.
- Provide a deterministic “Load demo scenario” action.
- Errors must explain the next corrective action without exposing secrets.
