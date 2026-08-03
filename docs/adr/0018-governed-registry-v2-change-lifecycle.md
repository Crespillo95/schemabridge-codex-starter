# ADR 0018: Registry-v2 changes are explicit immutable deltas

- Status: accepted locally in M35 under D129
- Date: 2026-08-03

## Context

M33 and M34 let a tenant author and publish a first model or append a non-colliding model to a
strict registry-v2. They intentionally reject new joins and replacement. Consequently a newly
onboarded tenant cannot govern a multi-table relationship, while an M26 blocking drift report has
no authenticated v2 remediation path. The historical M26 replacement acceptance used registry-v1
and cannot produce an M34 `activation_ready` handoff.

## Decision

SchemaBridge will represent each registry-v2 change as an immutable, tenant-bound delta over one
exact active base. The initial delta adds one approved join between exact existing mappings. The
second delta replaces/remediates one model and completely accounts for every affected mapping,
binding and incident join.

Join discovery remains evidence only. A current aggregate-only relationship observation and a
fresh steward decision produce the contract; name similarity never does. Many-to-many and
cross-connection relationships remain unsupported. Structural remediation must bind a complete
current M26 blocking report and cannot waive it against the old registry.

The pure assembler creates the next complete registry-v2. The existing isolated M34 worker then
revalidates authority, obtains a post-assembly publisher authorization, writes one immutable
DataHub target and reads it back exactly. M23 activation and M26 post-activation evidence approval
remain separate steps.

Historical M33 proposal bytes and fingerprints are not rewritten. Persistence evolves additively
with explicit proposal/draft kinds and closed decision targets.

Each immutable M33 replacement proposal may be consumed by at most one outer replacement draft.
That draft can be approved or rejected once. Rejection, base/catalog/dependency drift, or other
staleness requires a newly reviewed M33 proposal with a new source identity; Phase B does not
silently rebind old source evidence or invent a `superseded` transition.

## Consequences

- New tenants can eventually govern approved multi-table relationships without repository edits.
- Drift remediation gains a truthful v2 path while old registry versions and evidence remain
  immutable.
- Change authoring, publication, activation and evidence revalidation require separate decisions,
  increasing operator work but preserving recovery and auditability.
- Rejected or stale replacement attempts require new source authoring. This is intentionally more
  work than editing/reusing a proposal, but it preserves one unambiguous provenance chain.
- M35 remains PostgreSQL, same-connection and bounded. It does not add arbitrary joins, federation,
  automatic semantic migration, multi-dialect SQL or commercial certification.

## Rejected alternatives

1. Reuse the legacy join review directly: it is not tenant-bound and is not an M34 v2 authority.
2. Add joins during M33 model onboarding: complete candidate/base authority and routed profiling
   are not available at that trust boundary.
3. Edit the active DataHub document: this destroys immutable versioning, rollback and exact
   approval binding.
4. Treat a blocking M26 report as a waiver: structural drift requires a corrected registry and a
   new evidence cycle.
5. Certify M30 first: it would certify an incomplete customer lifecycle and M29 still blocks the
   operated campaign.
