# M31 — Controlled pilot and general-availability readiness

## Status

- State: planned; execution blocked until one M30 release candidate is accepted
- Release decision: **NO-GO**
- Initial offer: PostgreSQL copy-first private beta in an isolated customer environment
- Pilot size: one to three low-risk design partners for 30–60 days
- GA is a separate signed decision after the pilot; pilot entry is not GA

## Objective

Operate the exact M30-certified candidate with authorized customer schemas and real business
requests, prove support and recovery procedures under bounded load, and collect enough evidence to
decide whether the PostgreSQL SKU is ready for broader availability. M31 does not add a dialect,
raise table/join limits, enable automatic execution or waive a safe rejection during the pilot.

## Entry criteria

Every pilot tenant needs:

- an accepted M30 bundle for the exact deployed candidate;
- signed scope listing PostgreSQL version, region, permitted schemas, query families and excluded
  data/use cases;
- technical owner, business steward, publisher, auditor, security contact and incident contact;
- DPA, privacy/retention/deletion rules, subprocessors, residency and support terms;
- real OIDC/groups, least-privilege DataHub/source identities and managed secrets;
- tested catalog, registry-v2 lifecycle, publication, separate activation and rollback;
- tenant-specific blind acceptance subset and capacity budget;
- dashboards, SIEM, paging, backups and a restored fresh target;
- agreed stop conditions, rollback owner and offboarding/export process.

For the first cohort, an isolated deployment/control plane per customer is preferred. Any shared
multi-tenant deployment requires separate operated isolation evidence and explicit security
approval.

## Pilot operating model

The primary journey is copy-first:

```text
approved metadata → governed semantic context → natural-language request
→ typed interpretation → exact human confirmation → deterministic PostgreSQL
→ AST validation → copy/download → customer-controlled destination
```

No source execution happens by default. If optional preview is contracted, it uses a separately
approved read-only identity, one statement, enforced timeout/row limit and the same schema/join
authority. Customer operators remain responsible for permissions and workload controls in the
editor where copied SQL is eventually run.

Changes use M35 to create a new immutable registry version, M34 for isolated publication/readback
and M23 for separately approved activation. Drift or unresolved remediation blocks affected use;
the pilot must never patch active meaning or generated SQL silently.

## Service targets to validate

These are pilot objectives to measure, not current contractual SLOs:

| Measure | Pilot objective |
|---|---:|
| Managed service availability | at least 99.5% excluding agreed maintenance |
| Interactive request-to-confirmable interpretation p95 | at most 10 seconds |
| Confirmation-to-copyable SQL p95 | at most 3 seconds |
| Safe rejection/clarification response p95 | at most 5 seconds |
| Publication queue age p95 under normal load | at most 5 minutes |
| Security-critical acknowledgement | at most 15 minutes, 24×7 contact path |
| Backup RPO / restore RTO | at most 24 hours / 4 hours unless stricter terms are signed |
| Unauthorized mutation or tenant disclosure | exactly 0 |

Final SLOs must use observed pilot distributions, capacity headroom and support staffing. They are
not inferred from local tests.

## Metrics and review cadence

Collect only bounded, privacy-approved telemetry. Never log prompts, SQL, row data, credentials or
protected schema details by default. Tenant dashboards and weekly reviews cover:

- active users and successful governed requests;
- clarification, ambiguity, unsupported and policy-denial rates by reason code;
- steward decisions, registry changes, drift and time to remediation;
- semantic defects, escaped defects and corrected documentation/context;
- p50/p95/p99 latency, error rate, queue age, saturation and retries;
- model token/cost, infrastructure cost and support effort per accepted request;
- preview timeouts/row limits if preview is enabled;
- security alerts, access reviews, secret rotation, backup and restore evidence;
- customer satisfaction and time saved, without converting those measures into correctness claims.

Review critical events immediately, operations weekly and scope/product fit every two weeks. Any
corpus failure found in production becomes a redacted regression case before a candidate upgrade.

## Stop and rollback conditions

Pause the affected tenant immediately for any:

- unauthorized write, tenant boundary breach, credential exposure or unbounded source query;
- DDL/DML/utility/multi-statement/unknown-asset SQL escaping the guard;
- silently wrong critical metric, grain, join, fanout or `NULL` semantics;
- loss of exact registry/catalog/dependency authority;
- uncontained High/Critical vulnerability;
- inability to meet the agreed incident, backup or restore path;
- sustained saturation outside the certified capacity envelope.

Rollback means disable affected generation/feature, revoke credentials if relevant, restore the
last accepted application and immutable active registry pointer through approved controls, verify
readback, notify the customer and preserve audit evidence. It never means overwrite a historical
registry version or edit generated SQL behind the user's confirmation.

## Upgrade policy

Each pilot upgrade has a new signed artifact digest, migration rehearsal, regression bundle,
rollback image and customer maintenance notice. A changed model, prompt, compiler, AST guard,
semantic contract or dialect reruns its M30 gates. Database migrations are forward-only and tested
from the oldest supported pilot version plus a fresh install.

## Exit and GA-readiness criteria

The PostgreSQL SKU can enter a GA decision only when:

1. all pilot tenants complete the agreed period and acceptance subset without an open Critical or
   High security/semantic defect;
2. zero unauthorized writes, tenant disclosures and unsafe SQL escapes are observed;
3. measured service, recovery and support objectives are met with documented headroom;
4. every incident and near miss has a closed root-cause/regression trail;
5. onboarding, change, drift, publication, activation, rollback and offboarding runbooks have each
   been exercised by operators other than their authors;
6. capacity tiers, quotas, pricing/cost model and supported browser/PostgreSQL matrix are published;
7. support hours, escalation, status communication and vulnerability response are staffed;
8. DPA, privacy, retention/deletion, subprocessors, residency, licensing and billing are approved;
9. accessibility and browser findings within the committed target are closed;
10. product, customer, semantic owner, security, operations, support and legal sign the GA
    candidate-specific decision.

Missing evidence yields an extended pilot or **NO-GO**, not a broad release with caveats.

## Dialect and scope expansion

MySQL, SQL Server, Oracle, Snowflake, BigQuery, Redshift and every other engine remain separate
products until they pass their own implementation and M30/M31 evidence. The same applies to
self/CROSS joins, arbitrary subqueries/set operations, recursion, gaps/islands, governed
`ROLLUP/GROUPING`, federation and plans beyond three tables/two joins. Customer demand is an input
to prioritization, not authority to bypass typed contracts or certification.

## Pilot handoff package

- signed M30 candidate and tenant acceptance addendum;
- deployment/IAM/secret/network inventory;
- data-flow, privacy and retention record;
- tenant registry/catalog ownership and escalation matrix;
- SLO/capacity/cost dashboards and alert routes;
- change, drift, publication, activation, rollback and incident runbooks;
- backup/restore evidence and offboarding export/delete procedure;
- weekly reviews, incident register and final signed go/no-go record.
