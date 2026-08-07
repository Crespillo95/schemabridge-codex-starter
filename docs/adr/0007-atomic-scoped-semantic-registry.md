# ADR 0007: Atomic scoped governed semantic registry

- Status: accepted for M21
- Date: 2026-07-23

## Context

M09 and M10 originally loaded logical request choices, physical mappings, and join contracts from
separate synthetic recordings. That was sufficient for the north-star vertical slice, but it could
not prove that every artifact belonged to the same approved revision. It also encouraged
Customer-specific composition and made partial or stale combinations possible.

A production-oriented planner needs one versioned unit that answers:

- which logical models and fields are active;
- which approved physical mappings implement them;
- which approved contracts connect them;
- which decisions, evidence, risks, transformations, and versions justify those artifacts;
- which workspace/catalog deployment scope selected the unit;
- whether the exact bytes and canonical content are the expected revision.

The registry may contain many models and relationships, while every executable query must retain
the existing safety bounds. Ingestion of physical assets into DataHub alone does not reconstruct
this governed planning state.

## Decision

1. Planning context is supplied as one immutable `GovernedSemanticRegistrySnapshot` containing
   format/registry versions, registry ID, source, catalog scope, approved logical context, governed
   mapping registry, governed join registry, and provenance for every artifact family.
2. `SemanticRegistryScope` binds every application load to an opaque workspace ID, catalog scope,
   and registry ID. A differently scoped snapshot is rejected.
3. The application depends on one `GovernedSemanticRegistryPort`. Guided validation, intent
   validation, semantic planning, workflow execution/retry, rejected-source reporting, evaluation,
   and UI composition receive the same scoped registry boundary.
4. The recorded adapter accepts only the manifest's exact active entry. The manifest is limited to
   128 KiB and the registry to 2 MiB. Duplicate YAML keys, unsupported formats, missing/unlisted
   active entries, absolute or parent-escaped paths, symlinks, non-files, oversized files,
   checksum mismatches, fingerprint mismatches, identity/version mismatches, and catalog-scope
   mismatches fail closed without fallback.
5. Registry construction verifies all active artifacts together:

   - every logical field has an approved current mapping;
   - mapping and logical-field versions agree;
   - one physical field has at most one active logical meaning;
   - approval decisions are present and covered by provenance;
   - logical join summaries and physical contracts agree exactly;
   - every join key uses the exact approved physical mapping and transformation.

6. Registry capacity and query capacity are separate controls. The domain registry is bounded to
   100 models, 1,000 logical fields, 2,000 mappings, and 500 contracts. One request remains limited
   to three physical tables and two joins, and the independent SQL guard repeats the table limit.
7. Rejected-source inspection derives its allowlist only from exact approved join keys in the
   loaded registry. A parallel hard-coded key list is not authoritative.
8. Before execution or retry, the application reloads the current registry and resolves the
   validated request again. Changed identity, fingerprint, approvals, versions, mappings,
   contracts, or scope produce a stable stale failure before SQL compilation, preview, or
   rejected-source PostgreSQL I/O. The user must review and confirm a new plan.
9. The checked-in M21 registry is an explicitly recorded synthetic deployment bundle. It contains
   no SQL, credentials, tokens, private samples, or employer data and never substitutes for a
   requested live DataHub registry.
10. Names and definitions may contribute evidence, but neither is sufficient to establish semantic
    equivalence. M21 does not select registry fields from a free-form description.

## Verified M21 fixture identity

The active recorded bundle is:

```text
registry ID: synthetic_enterprise
registry version: 1
catalog scope: synthetic-demo
logical models: 7
approved mappings: 31
approved join contracts: 5
registry file SHA-256: 4f086f8b9f0679d6405beab45d48c6601520b9b6e0c5f20d7dc0ad987485a723
canonical registry fingerprint: 0710148874049078f751ac96f0a18131cf01daa41f27dc034ca52a8b212dd966
```

The associated deterministic source corpus contains 465 rows across eleven tables and eight
schemas. Its tracked global seed fingerprint is:

```text
487388495115265d5fac5a17675e05c236fd4eb4c430613ef02050a5f6010654
```

These identities prove reproducibility of the checked-in synthetic fixtures, not production
quality, freshness, scale, or live DataHub provenance.

## Alternatives rejected

- **Keep three independently loaded recordings:** cannot prove atomic revision consistency.
- **Use field-name similarity as the registry:** names are weak evidence and homonyms are
  deliberately present in the support corpus.
- **Let registry size raise query limits:** catalog breadth must not weaken execution safety.
- **Fall back to the recorded bundle after live failure:** would mislabel synthetic evidence as
  current governed state.
- **Execute SQL generated by a model:** the model remains limited to validated typed intent; the
  deterministic compiler and independent AST guard remain mandatory.
- **Add registry-wide description matching in M21:** it requires a separately reviewed ambiguity,
  ranking, authorization, and evaluation surface.

## Consequences and residual work

M21 removes the north-star-only planning boundary and proves typed planning across Customer,
Product, SalesOrder, SaleLine, and Shipment models while preserving the original result and safety
invariants. Corrupt or stale registry state has an explicit fail-closed path, and a large registry
can coexist with tightly bounded queries.

The recorded adapter is not the production source of truth. M22 must reconstruct the same atomic
port contract from complete governed DataHub read-back and preserve missing/unavailable evidence
without a recorded fallback. M23 must provide approval-gated registry publication/activation,
durable migrations, rollback, and reconciliation. M27 must implement catalog-driven guided
controls and registry-wide natural-language/short-description matching with explicit ambiguity and
human approval. Until M27, the M11 parser remains Customer/AccountHolder-focused.
