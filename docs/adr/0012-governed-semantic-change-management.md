# ADR 0012: Governed semantic change management

- Status: accepted; M26 implemented and accepted locally
- Date: 2026-07-24

## Context

SchemaBridge has two identities that deliberately solve different problems:

- the semantic registry identifies executable physical fields as `schema.table.path`;
- the dynamic catalog identifies observed metadata by workspace, connection, asset URN, field path,
  and generation.

The SQL-facing identity is intentionally small and safe inside one already governed connection,
but it cannot distinguish homonymous assets across multiple connections. A company with 5,434
tables also cannot be managed by rescanning or comparing every catalog field for every request.

M23 already makes one immutable DataHub registry version authoritative through a PostgreSQL
compare-and-swap pointer. M25 already promotes complete catalog generations and fingerprints every
asset and field. Neither milestone records which exact catalog resource supplied the evidence for
an approved mapping, preserves a complete downstream dependency index, or re-profiles an approved
join when source cardinality changes.

## Decision

### Explicit observation binding

Each governed mapping receives a separately approved observation binding containing:

- workspace and catalog scope;
- catalog connection;
- stable asset identity and field path;
- registry pointer/version/fingerprint;
- mapping version and decision identity;
- observed catalog generation and metadata fingerprints.

The binding is metadata evidence only. It does not become a connector credential, a cross-source
join contract, or an M28 execution-routing decision.

An equal `schema.table.field`, display name, definition, tag, or glossary term may produce a bounded
candidate for review but can never establish or replace a binding. Missing, ambiguous, disabled,
removed, stale, or cross-scope evidence fails closed.

Bindings from multiple connections may coexist in one tenant and registry. An individual approved
join and executable plan must nevertheless resolve to exactly one common connection. M26 persists
and validates that identity but does not resolve a catalog route into a credential. Each aggregate
profile worker is explicitly configured for one workspace/connection pair, claims and reclaims
only that pair's jobs, and validates the pair again before heartbeat and before opening its
read-only source.

### Two evidence layers

M26 compares:

1. exact M25 catalog metadata for governed fields; and
2. aggregate-only relationship profiles for approved join contracts.

Join profiling reuses the exact approved normalization plans, allowlist, read-only PostgreSQL
identity, timeout, and count-only boundary. No raw key or source row is persisted. A first
observation has no trusted baseline and is labelled `baseline_required`.

Profile requests are durable, workspace-and-connection-qualified jobs with an independent lease,
capability digest, monotonic fence, retry budget, and aggregate-only result. The reconciler enqueues the
complete required batch before deciding that pending evidence is unavailable, allowing a registry
with more joins than one retry attempt can process to make bounded progress without dropping work.

### Change policy

Definition, tag, glossary, and non-structural asset-metadata changes require explicit compatible
revalidation. Physical type, key status, nullability, field/asset removal, ambiguity, approved
cardinality/fanout change, lost foreign-key evidence, or unsafe null/invalid/multiplicity/overlap
change is blocking.

A blocking change cannot be waived against the same registry version. Remediation publishes a new
strict immutable registry version through the existing mapping/join approvals, activates it through
the M23 compare-and-swap path, and establishes new exact evidence. Old versions, reports,
decisions, and baselines remain immutable.

### Dependency-aware gate

The control plane maintains a versioned projection of mapping, join, workflow, and query-recipe
dependencies. Reports bind the complete impact-set fingerprint and counts; incomplete indexing is
visible and cannot be called complete.

Planning, retry, execution, and recipe reuse check only the exact dependencies they use. Affected
work stops before compilation, preview, rejected-source inspection, or source I/O. An unrelated
change among thousands of catalog assets does not block an unchanged plan.

Workflow and query-recipe inventories are paginated at a maximum of 50 items. Reconciliation
renews the current scan lease before, between, and after pages; lease loss prevents the dependency
watermark from advancing. Query-recipe current/version DataHub identities include a prefix of the
full semantic scope fingerprint, and active publication/reuse validates that complete binding.
Two workspaces with the same intent therefore cannot share a current recipe marker.

One atomic dependency replacement is explicitly bounded to 10,000 managed artifacts and 100,000
mapping/join edges; exceeding either bound makes coverage incomplete. PostgreSQL edge persistence
uses batches of 500. Same-scope stale workflows/recipes project conservatively through exact
current logical-field/contract identities; cross-scope or unresolvable artifacts make coverage
incomplete.

### Durable detection and approval

Catalog-generation and active-registry changes enqueue idempotent durable scan requests. A
separately operated reconciler uses leases and fencing, records immutable reports/findings/impacts,
and applies only exact current approvals through compare-and-swap.

One catalog generation may affect more than one active registry. Promotion fans out one
scope-qualified request per matching active pointer rather than relying on a configured singleton
scope. If no active pointer exists, the later registry transition supplies the initial exact scan.

Authenticated HTTP is read-only and paginated. Baseline, revalidation, rejection, and remediation
remain trusted operator flows. M26 does not call an LLM or write source data.

## Consequences

- An initial schema-v5 rollout requires an explicit baseline before managed affected queries are
  eligible.
- Historical recipes require an explicit dependency backfill; missing coverage blocks a complete
  blast-radius claim.
- Existing unscoped recipe documents remain legacy evidence; current managed publication creates
  scope-qualified identities.
- Catalog metadata can detect structural and governance changes, while join safety still requires
  bounded aggregate source reads.
- Revalidation has operational cost but preserves the rule that semantic ambiguity is decided by a
  person, not a similarity score.
- The catalog may be arbitrarily larger than one query without making drift checks or runtime
  gating catalog-sized.
- Each governed workspace/connection pair needs a matching isolated profile-worker deployment
  until M28 introduces explicit connector routing.
- Schema v4 binaries refuse schema v5; rollout and rollback remain coordinated.
- The final implementation uses separate exact locator-first initial-candidate and approved-evidence
  reads. Initial review returns at most two witnesses per mapping through a reconciler-only bounded
  capability backed by length-prefixed asset/field expression indexes and raw-value rechecks, so
  clean bulk-loaded tables do not rely on analyzed planner statistics; approved evidence never
  scans unrelated fields. It also uses bounded/batched dependency replacement, conservative
  stale-artifact projection, and
  workspace-plus-connection profile claims; focused regressions do not replace the complete
  milestone gate.

## Rejected alternatives

- **Match only by `schema.table.field`:** collides across connections and violates tenant/catalog
  identity.
- **Choose the closest name or definition:** candidate evidence is not semantic equivalence.
- **Block every request whenever any generation changes:** creates avoidable tenant-wide outages
  and ignores exact dependencies.
- **Auto-approve unchanged names after a type/key change:** leaves the immutable registry contract
  inconsistent with physical evidence.
- **Persist raw join keys or samples:** exceeds the aggregate-only privacy and least-privilege
  boundary.
- **Let an LLM decide drift or emit replacement SQL:** violates deterministic policy, approval, and
  compiler invariants.
