# M30 — Production evaluation and security verification

## Status

- State: Phase 0 and Phase 1a pass their prior local gate; M30 target-binding hardening is under
  final verification. No externally authenticated manifest or operated receipt exists, so M30,
  campaign and release remain blocked
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

The managed candidate must additionally bind one exact registry-v2 connection and current
`GovernedExecutionTarget` before provider access. The complete active registry must pass M26 before
either target or provider access, and selected-plan dependencies must pass M26 again after
interpretation, at confirmation and before generation. A `qsp3` confirmation and resolved-plan
fingerprint bind connection ID, route revision, target fingerprint and type-contract fingerprint;
preparation, confirmation and generation re-resolve that identity, compiler plus both guards
receive it, and the UI revalidates retained artifacts before every later display/download.
Development or recorded `target_fingerprint=None` output is explicitly non-commercial and cannot
enter M30.

It may not claim infallibility, arbitrary SQL, cross-database portability, federation, unrestricted
schema size per query, or support for families absent from the typed language.

## Frozen inputs

Before campaign execution, release engineering records a workflow-attested freeze:

- commit, lockfile, container and wheel digests;
- PostgreSQL, DataHub and browser versions;
- model/provider identifier, parameters and prompt/template fingerprints;
- schema version, migrations, feature flags and safe limits;
- supported/unsupported SQL-family matrix;
- corpus manifest and hidden answer-key digest;
- target environment, IAM grants, NetworkPolicy, secret versions and observability endpoints;
- six named authorities: product, data semantics, security, operations, release and independent
  evaluation. Naming an authority or its key fingerprint is not its signature.

Changing any frozen input invalidates the campaign or starts a separately identified run.

### Phase 0 offline preflight

`plans/M30_CAMPAIGN_CONTRACT.yml` schema v2 is the machine-readable contract for the PostgreSQL
typed-plan-v2 SKU. It freezes copy-first/no-default-preview behavior, one connection, three tables,
two joins, the qsp3 target tuple, M26/target-resolution checkpoints, target-bound consumers and
provider-free retained-artifact revalidation,
context and request-complexity bounds, 500 default/10,000 maximum preview rows, 5,000 ms timeout,
explicit `NULL`/fanout policies, five corpus classes with 500 Spanish plus 500 English cases,
zero-tolerance/quantitative thresholds, supported/unsupported SQL families and 24 required
evidence controls. `make m30-readiness` materializes deterministic JSON and Markdown under ignored
`.local/m30/`.

This preflight reads only Git and repository files. It may pass repository-contract facts, but it
deliberately cannot pass hosted CI, operated-target, independent-third-party or owner-approval
controls. A pull-request artifact bound to an ephemeral merge ref, a checked-in synthetic report
or a locally authored `status=passed` document never satisfies those controls. Missing evidence,
a dirty tree, a non-`main` checkout, an absent annotated stable SemVer tag or an uncommitted contract
returns `blocked_prerequisites`, `campaign_executable=false` and `release_decision=no_go`.

The command's `--report-only` switch changes only its process exit code so an operator can retain
the report. It cannot change any gate or verdict. Phase 0 is preparation evidence, not M30
acceptance, a signed candidate freeze, campaign execution or release authority.

The inspector neutralizes ambient Git configuration/environment, replacement refs, lazy fetch,
hooks and fsmonitor; rejects a mismatched repository root, hidden/sparse index state, symlink or
gitlink objects and unexpected migrations; hashes tracked worktree bytes/modes without Git clean
filters; and rechecks HEAD/tree/branch/tags/index/worktree before returning. JSON is written last
as the report-bundle commit marker and binds the Markdown digest.
These controls still do not create an external trust root: the candidate contains this code and
contract. Phase 1a therefore authenticates a separately workflow-attested manifest; it still does
not permit a corpus runner to touch provider, source or target.

The Phase 0 contract requires every named SQL family and balanced language/class totals. The
Phase-1a corpus manifest additionally freezes exact per-family/risk counts and case IDs;
Phase 0 cannot run or accept the campaign until that independently owned manifest exists.

### Phase 1a authenticated campaign manifest

Phase 1a defines canonical JSON frozen inputs and authenticates their exact bytes with GitHub
Artifact Attestations. The manifest binds the complete Phase-0 candidate observation, contract/SKU,
wheel/image/SBOM/provenance/frozen-requirements digests, provider/model/config/prompt/parameters,
target versions/IAM/network/secrets/observability, corpus/IDs/hidden-answer-key/oracle digests,
balanced class/language/family/risk slices, six owner authorities and all 24 control assignments.
It has an exact UTC window of at most 30 days.

Every slice is exactly balanced between Spanish and English. Simple families are `standard`;
every advanced family appears once at each of `standard`, `high` and `critical`; ambiguity is
`cross_family/high`, every unsupported family is `high`, and adversarial security is
`security/critical`. This prevents both monolingual family slices and a vacuous “zero critical
semantic failures” threshold.

The manual workflow `.github/workflows/m30-manifest-attestation.yml` accepts only public canonical
manifest bytes and may run only from an existing annotated stable tag whose commit is protected
`main`; it validates that requirement, processes the manifest outside the checkout and creates a
GitHub-hosted SLSA provenance attestation. The dedicated
`m30-manifest-attestation` environment **must** have independent required reviewers, self-review
and administrator bypass disabled, no secrets, and retained deployment evidence. Those settings,
tag/default-branch protection and exclusive authority remain unproven external prerequisites; do
not dispatch without them. Canonical bytes are capped at 45 KiB so their 61,440-character base64 encoding stays
below GitHub's aggregate dispatch-input limit; no raw case, answer key, secret, SQL or row belongs in
the workflow input.

After the external environment/tag protections have been evidenced, a second operator uses the
following sequence from a clean, tagged `main` checkout. Every path below is outside the candidate;
the manifest contains only public, opaque identifiers and digests. The operator creates the
canonical JSON against the candidate-generated schema, validates it locally, dispatches the exact
tag, and records the returned run from the run listing in the change record:

```bash
M30_REPOSITORY='Crespillo95/schemabridge-codex-starter'
M30_EVIDENCE_DIR='/external/evidence/m30'
M30_MANIFEST="$M30_EVIDENCE_DIR/m30-campaign-manifest.json"
M30_SCHEMA="$M30_EVIDENCE_DIR/m30-campaign-manifest.schema.json"
M30_REVISION="$(git rev-parse HEAD)"
M30_TAG="v$(.venv/bin/python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"

make m30-manifest-schema > "$M30_SCHEMA"
# Independently populate M30_MANIFEST; the schema never supplies owner approval or hidden cases.
make m30-manifest-validate M30_MANIFEST="$M30_MANIFEST"
M30_MANIFEST_B64="$(.venv/bin/python -c 'import base64,pathlib,sys; print(base64.b64encode(pathlib.Path(sys.argv[1]).read_bytes()).decode("ascii"))' "$M30_MANIFEST")"
gh workflow run m30-manifest-attestation.yml \
  --repo "$M30_REPOSITORY" \
  --ref "$M30_TAG" \
  -f release_tag="$M30_TAG" \
  -f manifest_base64="$M30_MANIFEST_B64"
unset M30_MANIFEST_B64
gh run list \
  --repo "$M30_REPOSITORY" \
  --workflow m30-manifest-attestation.yml \
  --branch "$M30_TAG" \
  --event workflow_dispatch \
  --limit 10 \
  --json databaseId,headSha,headBranch,createdAt,status,url
```

The operator selects only the just-dispatched row whose `headSha` equals `M30_REVISION`, records
its ID/URL, and then retrieves the exact run, actor and attestation. Do not derive the ID from
“latest” in unattended automation:

```bash
M30_RUN_ID='<recorded workflow run databaseId>'
gh run view "$M30_RUN_ID" --repo "$M30_REPOSITORY" \
  --json attempt,event,headBranch,headSha,status,conclusion,url
gh api "repos/$M30_REPOSITORY/actions/runs/$M30_RUN_ID" \
  --jq '{actor: .actor.login, actor_id: .actor.id, triggering_actor: .triggering_actor.login}'
gh run watch "$M30_RUN_ID" --repo "$M30_REPOSITORY" --exit-status
M30_RUN_ATTEMPT="$(gh run view "$M30_RUN_ID" --repo "$M30_REPOSITORY" --json attempt --jq '.attempt')"
M30_RUN_DIR="$M30_EVIDENCE_DIR/run-$M30_RUN_ID-attempt-$M30_RUN_ATTEMPT"
mkdir -m 0700 "$M30_RUN_DIR"
gh run download "$M30_RUN_ID" --repo "$M30_REPOSITORY" \
  --name "m30-campaign-manifest-$M30_REVISION-$M30_RUN_ID-$M30_RUN_ATTEMPT" \
  --dir "$M30_RUN_DIR"
M30_DOWNLOADED_MANIFEST="$M30_RUN_DIR/m30-campaign-manifest.json"
cmp -s "$M30_MANIFEST" "$M30_DOWNLOADED_MANIFEST"
M30_MANIFEST_SHA256="$(shasum -a 256 "$M30_DOWNLOADED_MANIFEST" | cut -d' ' -f1)"
(cd "$M30_RUN_DIR" && gh attestation download "$M30_DOWNLOADED_MANIFEST" \
  --repo "$M30_REPOSITORY" \
  --predicate-type 'https://slsa.dev/provenance/v1' \
  --limit 1)
M30_BUNDLE="$M30_RUN_DIR/sha256:$M30_MANIFEST_SHA256.jsonl"
make m30-authenticate-manifest \
  M30_MANIFEST="$M30_DOWNLOADED_MANIFEST" \
  M30_ATTESTATION_BUNDLE="$M30_BUNDLE" \
  M30_AUTHENTICATION_OUTPUT="$M30_RUN_DIR/authentication"
```

If the run must be retried, rerun **all** jobs with `gh run rerun "$M30_RUN_ID"`; never use
`--failed`, because the artifact name is bound to `run_attempt` and `sign` cannot reuse a prior
attempt's validation artifact.

The two report files are `authentication.json` and `authentication.md`. Validation or
authentication exit `0` only for their bounded success, `2` for a blocked candidate/manifest, and
`3` for malformed/unavailable evidence, provider failure or report-write failure. The operator
retains the schema, original/downloaded manifest digests, run metadata, detached bundle and both
reports under the tenant evidence-retention policy; they are not committed to the repository.

The verifier requires an official-release, platform-specific byte-pinned GitHub CLI 2.96.0
executable (archive and binary checksums are in the runbook), repository and signer workflow identity, candidate
revision as source/signer digest, exact tag ref, GitHub OIDC issuer, SLSA predicate, hosted runner,
detached bundle and at least one cryptographically verified log/TSA timestamp. `--bundle` avoids an attestation API lookup,
but GitHub CLI can still bootstrap/update its trusted root; this is not an air-gapped verification
claim. Inputs are bounded regular non-symlink files outside the repository, copied into private
owner-only snapshots for verification and reread before/after to reject TOCTOU. There is no
key/issuer, unattested, `status`, `report-only` or bypass argument. The report binds hashes of the
official verifier executable plus its platform, bundle, certificate evidence, verification summary
and trusted timestamps; it does not call a TSA timestamp “transparency” or count owner fingerprints
as owner signatures.

Success means only `workflow_attested_manifest_authenticated=true` and
`campaign_executable=false`. Provider, source, target and corpus execution stay blocked, release
remains `no_go`, synthetic evidence is not accepted, material controls passed remain `0` and all 24
controls remain unadjudicated. Phase 1b must define control-specific authenticated receipts,
prerequisite ordering and deterministic adjudicators; a signed `claimed_outcome=passed` can never be
sufficient by itself.

## Blind bilingual evaluation

An evaluator who did not author the implementation owns the hidden answer key. The corpus uses
synthetic public cases plus customer-authorized private cases that never enter the repository or
logs. Paraphrases and held-out schemas are separated from development fixtures.

The minimum campaign contains:

| Class | Total | Spanish | English | Required coverage |
|---|---:|---:|---:|---|
| Supported simple | 200 | 100 | 100 | projection, filters, sorting, limits, `NULL`, dates and identifiers |
| Supported advanced | 300 | 150 | 150 | aggregates, `HAVING`, conditional aggregates, buckets, ranking, top-N, percentages, running totals, moving windows and lag/lead |
| Ambiguous | 150 | 75 | 75 | unclear metric/date/grain/filter/join/tie/null/fanout meaning |
| Deliberately unsupported | 150 | 75 | 75 | self/CROSS joins, arbitrary subqueries/sets, recursion, gaps/islands, unsafe `ROLLUP`, federation and over-limit plans |
| Adversarial/security | 200 | 100 | 100 | prompt injection, SQL smuggling, comments, multi-statement input, DDL/DML/utility requests, stale authority, IDOR and payload tampering |

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

- workflow-attested campaign manifest and immutable raw-result bundle;
- separate candidate-specific approvals from product, semantic, security, operations and release
  owners; the independent evaluator signs/adjudicates its corpus and equivalence evidence but cannot
  replace an owner approval;
- bilingual corpus report with per-slice failures and adjudication trail;
- PostgreSQL execution-equivalence and AST-safety report;
- scale/cost envelope and proposed quotas;
- independent penetration-test attestation and remediation evidence;
- IAM, secrets, network, SIEM, backup/restore and incident-drill evidence;
- browser/accessibility compatibility report;
- release risk register and product/semantic/security/operations/release go/no-go signatures.

## Acceptance criteria

1. All frozen inputs and artifacts are reproducible from a clean checkout.
2. Every security invariant and zero-tolerance metric passes with no exception.
3. All quantitative thresholds pass overall and on every critical slice.
4. M35 replacement/remediation, publication and separate activation paths are in the campaign.
5. The scale envelope names what was tested and does not extrapolate beyond it.
6. Documentation, UI and sales claims match the measured PostgreSQL contract.
7. Product, semantic, security, operations and release owners sign one candidate-specific
   decision; the independent evaluator separately signs its adjudication evidence.

Failure keeps release **NO-GO**. Fixing a failure creates a new candidate and reruns every affected
campaign slice; a changed model/prompt/compiler/guard always reruns the complete language and
safety corpus.

## Operator sequence

1. Freeze the candidate, authenticate the workflow-attested campaign manifest and retain the
   independent environment-review evidence; this does not yet authorize campaign execution.
2. Restore the clean-room environment and migrate a fresh control plane.
3. Verify IAM, network and secret boundaries before loading corpus metadata.
4. Run deterministic unit/integration/acceptance gates.
5. Run the hidden bilingual language, execution and adversarial corpus.
6. Run scale, soak, failure-recovery and browser campaigns.
7. Complete independent pentest and operated recovery/incident drills.
8. Publish the evidence bundle and hold the candidate-specific go/no-go review.
