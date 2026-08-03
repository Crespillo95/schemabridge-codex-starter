# M30 — Production evaluation and security verification

## Status

- State: planned; execution blocked until the operated M29 prerequisites exist
- Release decision: **NO-GO**
- Candidate SKU: PostgreSQL copy-first private beta, isolated per customer
- Depends on: accepted M29 contracts in the target environment and accepted M35 registry lifecycle
- Followed by: M31 controlled pilot and GA-readiness evidence

## Objective

Certify one immutable SchemaBridge release candidate against its exact commercial contract. M30
does not expand the SQL language, add a dialect, or turn an unsupported request into best-effort
SQL. It measures the complete path from Spanish or English business language through approved
semantic context, confirmation, deterministic PostgreSQL compilation, AST validation and a
copyable artifact, while proving that ambiguous, stale, unauthorized and unsupported requests
fail without SQL or external mutation.

The only candidate in scope is PostgreSQL. Every additional dialect requires its own typed
compiler, type/function/null/quoting rules, AST guard, timeout policy, corpus, scale campaign and
release decision; syntax translation is not certification.

## Commercial contract under test

The candidate may claim only that it:

1. uses exact approved models, mappings, physical bindings and joins from the active registry;
2. emits standalone PostgreSQL for supported typed intents after exact human confirmation;
3. deterministically compiles and validates the confirmed intent;
4. defaults to copy/download and does not execute the artifact automatically;
5. returns clarification or `unsupported` without SQL when authority or meaning is incomplete;
6. supports at most one connection, three tables and two approved joins per request; and
7. preserves the configured `NULL`, fanout, row-limit and timeout policies.

It may not claim infallibility, arbitrary SQL, cross-database portability, federation, unrestricted
schema size per query, or support for families absent from the typed language.

## Frozen inputs

Before campaign execution, release engineering records and signs:

- commit, lockfile, container and wheel digests;
- PostgreSQL, DataHub and browser versions;
- model/provider identifier, parameters and prompt/template fingerprints;
- schema version, migrations, feature flags and safe limits;
- supported/unsupported SQL-family matrix;
- corpus manifest and hidden answer-key digest;
- target environment, IAM grants, NetworkPolicy, secret versions and observability endpoints;
- owners for product, data semantics, security, operations and release approval.

Changing any frozen input invalidates the campaign or starts a separately identified run.

## Blind bilingual evaluation

An evaluator who did not author the implementation owns the hidden answer key. The corpus uses
synthetic public cases plus customer-authorized private cases that never enter the repository or
logs. Paraphrases and held-out schemas are separated from development fixtures.

The minimum campaign contains:

| Class | Minimum cases | Required coverage |
|---|---:|---|
| Supported simple ES/EN | 200 | projection, filters, sorting, limits, `NULL`, dates and identifiers |
| Supported advanced ES/EN | 300 | aggregates, `HAVING`, conditional aggregates, buckets, ranking, top-N, percentages, running totals, moving windows and lag/lead |
| Ambiguous | 150 | unclear metric/date/grain/filter/join/tie/null/fanout meaning |
| Deliberately unsupported | 150 | self/CROSS joins, arbitrary subqueries/sets, recursion, gaps/islands, unsafe `ROLLUP`, federation and over-limit plans |
| Adversarial/security | 200 | prompt injection, SQL smuggling, comments, multi-statement input, DDL/DML/utility requests, stale authority, IDOR and payload tampering |

Every supported case specifies the accepted semantic intent, required registry facts, allowed SQL
AST equivalence class, expected result over a frozen oracle dataset and acceptable ordering/null
semantics. Text equality alone is not an oracle.

## Release metrics and thresholds

The following are minimum private-beta thresholds, not evidence already achieved:

| Metric | Threshold |
|---|---:|
| Unauthorized source/DataHub/activation writes | exactly 0 |
| Cross-tenant disclosure or mutation | exactly 0 |
| DDL, DML, utility, hidden second statement, Cartesian or unknown-asset SQL emitted | exactly 0 |
| Deterministic compiler correctness from a valid frozen typed intent | 100% |
| AST guard acceptance of valid compiler output | 100% |
| End-to-end semantic correctness on supported held-out cases | at least 95%, with no silently wrong critical case |
| Safe no-SQL outcome on ambiguous/unsupported cases | at least 99% and 100% for security-critical cases |
| Confirmation bypass or SQL visible before confirmation | exactly 0 |
| Result equivalence on cases eligible for oracle execution | 100% after accepted typed intent |
| Regression against the last accepted candidate | no critical regression; any material regression requires signed exception and rerun |

A case is not rescued by a plausible-looking query. Wrong grain, join, denominator, ordering,
window frame, `NULL` handling or fanout is a semantic failure. Results are reported by language,
family, schema novelty and risk level with confidence intervals; an aggregate score cannot hide a
failed critical slice.

## Scale and cost campaign

Inventory scale and query breadth are measured separately. The campaign uses synthetic skew,
wide schemas, high-cardinality identifiers and retained generations. Initial tiers are hypotheses
to validate, not promises:

| Tier | Catalog assets / fields | Concurrent interactive users | Oracle table profile |
|---|---:|---:|---|
| S | 1,000 / 25,000 | 5 | up to 10 million rows |
| M | 5,000 / 100,000 | 20 | up to 100 million rows |
| L | 20,000 / 500,000 | 50 | up to 1 billion rows with controlled skew |

For each accepted tier record p50/p95/p99 and error rate for catalog lookup, intent resolution,
candidate preview, compilation, queue transitions and optional read-only oracle execution. Record
CPU, memory, PostgreSQL connections, queue depth/age, model tokens/cost and DataHub calls. Test
cold start, noisy tenant, burst, retry, stale generation, dependency-index growth and one-hour
soak. A tier is unpublished until it has a capacity envelope, alert thresholds and a documented
degradation policy.

## Security and operated-control verification

M30 requires evidence from the actual target environment:

1. independent threat-model review and penetration test with zero open Critical or High findings;
2. real OIDC/group mapping, tenant isolation, IDOR, session freshness and separation-of-duty tests;
3. DataHub IAM proving the isolated publisher has only the exact required write permission and
   web/API/runtime cannot obtain that credential;
4. source identities independently verified read-only with transaction mode, statement timeout
   and allowlist enforcement;
5. managed secret creation, rotation, revocation and leak-response drill;
6. cluster admission, NetworkPolicy, egress and workload-identity verification;
7. SIEM/paging delivery for auth, policy denial, drift, queue, publication and restore events;
8. signed backup plus restore into a fresh target with measured RPO/RTO;
9. software-composition, provenance, SBOM, signature and clean-room install verification; and
10. adversarial browser/API tests for CSRF, XSS, injection, body limits, pagination and error
    sanitization.

The browser matrix includes current supported Chrome, Safari, Firefox and Edge desktop builds plus
a 390×844 mobile viewport. The exact supported matrix and accessibility target are release
artifacts.

## Required artifacts

- signed campaign manifest and immutable raw-result bundle;
- bilingual corpus report with per-slice failures and adjudication trail;
- PostgreSQL execution-equivalence and AST-safety report;
- scale/cost envelope and proposed quotas;
- independent penetration-test attestation and remediation evidence;
- IAM, secrets, network, SIEM, backup/restore and incident-drill evidence;
- browser/accessibility compatibility report;
- release risk register and product/security/operations go/no-go signatures.

## Acceptance criteria

1. All frozen inputs and artifacts are reproducible from a clean checkout.
2. Every security invariant and zero-tolerance metric passes with no exception.
3. All quantitative thresholds pass overall and on every critical slice.
4. M35 replacement/remediation, publication and separate activation paths are in the campaign.
5. The scale envelope names what was tested and does not extrapolate beyond it.
6. Documentation, UI and sales claims match the measured PostgreSQL contract.
7. Product, security, operations and semantic owners sign one candidate-specific decision.

Failure keeps release **NO-GO**. Fixing a failure creates a new candidate and reruns every affected
campaign slice; a changed model/prompt/compiler/guard always reruns the complete language and
safety corpus.

## Operator sequence

1. Freeze and sign the candidate and campaign manifest.
2. Restore the clean-room environment and migrate a fresh control plane.
3. Verify IAM, network and secret boundaries before loading corpus metadata.
4. Run deterministic unit/integration/acceptance gates.
5. Run the hidden bilingual language, execution and adversarial corpus.
6. Run scale, soak, failure-recovery and browser campaigns.
7. Complete independent pentest and operated recovery/incident drills.
8. Publish the evidence bundle and hold the candidate-specific go/no-go review.
