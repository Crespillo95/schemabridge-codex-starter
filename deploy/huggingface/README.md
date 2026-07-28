---
title: SchemaBridge Judge Demo
emoji: 🌉
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
fullWidth: true
header: mini
license: apache-2.0
short_description: Governed semantic query agent using explicit recorded demo integrations.
---

# SchemaBridge judge demo

This public Space runs the deterministic, API-key-free judge path.

- Catalog and candidate evidence: recorded synthetic fixtures.
- Intent: deterministic typed fake parser.
- SQL: compiled from typed intent and independently AST-validated on every run.
- Result and rejected-source evidence: replayed only for the exact fingerprinted north-star query.
- Publication: local fake adapter; no DataHub mutation.

This Space does **not** claim a live DataHub, PostgreSQL, or LLM connection. The repository documents
the full local DataHub/PostgreSQL integration path and its approval-gated write-back evidence.
