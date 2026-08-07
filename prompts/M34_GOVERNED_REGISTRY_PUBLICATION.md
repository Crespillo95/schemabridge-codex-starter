# M34 implementation prompt

Implement `plans/M34_GOVERNED_REGISTRY_PUBLICATION.md` as one vertical milestone. Preserve all
repository invariants and ADR 0017. Start with red tests for registry v2, truthful zero-join
provenance and a deliberately non-conventional observed DataHub URN. Add a deterministic additive
assembler, an exact post-assembly approval, a dedicated fenced PostgreSQL publication queue and an
isolated publisher process. Reuse M22 immutable write/read-back and M23 activation contracts only
where their authority assumptions remain valid. Never derive an asset URN, never give API/web the
writer credential, and never activate from the publisher. Run focused tests, PostgreSQL/DataHub
integration, acceptance, internal-browser verification and `make check`; update all durable state
and handoff files with exact evidence. The global release verdict remains NO-GO unless M30, M31 and
external controls are independently satisfied.
