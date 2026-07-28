# Evaluation methodology

M15 evaluates only the checked-in synthetic SchemaBridge release-candidate fixtures. It is a
reproducibility harness, not a claim about production data, an employer benchmark, or general model
quality. The JSON report records the source revision or the explicit `working-tree-uncommitted`
state, a source fingerprint, every ground-truth version, and a combined fixture fingerprint.
The checked-in SQL-free recipe fingerprint is regenerated whenever a governed plan invariant
changes; this remediation refreshed it after contract-bound fanout validation changed the plan
fingerprint without changing executable SQL or expected rows.

M21 expands query ground truth from two to five cases against one atomic semantic registry:
the north-star, a Customer no-join control, a Product no-join control, a SalesOrder/Shipment
fanout case, and a three-table commerce revenue case. The registry bundle is part of the combined
fixture fingerprint. The four new requests are evaluated as exact typed requests; their
natural-language rows are explicitly skipped in the M15 report because its deterministic M11
parser remains Customer-focused. M27 evaluates description matching in a separate corpus and its
scores must not be merged into this baseline.

## One-command deterministic run

From a prepared development environment, this resets only the M01 synthetic Compose volume and
then writes both report formats:

```bash
make evaluate
```

The equivalent non-resetting command uses the fixed public demo-reader default:

```bash
.venv/bin/python -m schemabridge.entrypoints.cli.main evaluate \
  --output reports/evaluation.json \
  --markdown examples/evaluation-report.md
```

An explicit `DATABASE_URL` overrides that default for a relocated copy of the same synthetic
reader. Evaluation never supplies a default credential to non-evaluation commands or other data.

`reports/` is ignored runtime output. The example Markdown report is judge-readable synthetic
evidence. The current remediation differs from `HEAD`, so generated scores remain an uncommitted
working-tree snapshot and are not a release-commit claim. Regenerate both artifacts after the
reviewed release commit before quoting a score externally.

## Metric definitions

Every rate includes its numerator, denominator, evaluated cases, skipped cases, and failed cases.
The fixtures are too small for a meaningful bootstrap confidence interval, so the report contains
raw counts only and explicitly states that no confidence interval was calculated.

| Metric | Definition |
|---|---|
| Candidate precision | true positives / (true positives + false positives) at the configured deterministic recommendation threshold |
| Candidate recall | true positives / (true positives + false negatives) |
| Candidate F1 | `2 × true positives / (2 × true positives + false positives + false negatives)` |
| Candidate top-k recall | positive labels among the first k ranked cases / all positive labels |
| Join-path accuracy | query cases whose ordered approved contract IDs exactly equal ground truth / evaluated query cases |
| Cardinality accuracy | discovered contracts whose classified cardinality exactly equals ground truth / expected contracts |
| Intent equivalence | confirmed typed requests exactly equal the versioned request object / supported intent cases |
| Compile success | query cases reaching independently accepted final SQL / query cases |
| Execution success | compiled cases completing the bounded read-only preview and rejection inspection / query cases |
| Result correctness | cases whose type-tagged, column-keyed, order-independent row multisets exactly equal expected rows / executed cases |
| Safety rejection rate | malicious SQL cases rejected with the expected first stable guard code / security cases |
| Recipe reuse accuracy | current/stale compatibility cases matching expected status and staleness reasons / recipe cases |

Result comparison does not use SQL string equality. Values retain type tags, columns are keyed and
sorted, and complete rows are compared as multisets so duplicate rows remain observable.

## Current M21 development evidence

The 2026-07-23 local run completed with:

- join-path accuracy `5/5` and cardinality accuracy `2/2`;
- intent equivalence `1/1`, with 4 intentional M27 skips and no failed intent case;
- compilation, execution, result correctness, and source-rejection correctness `5/5` each;
- SQL safety rejection `38/38`;
- recipe compatibility `2/2`;
- the exact reader `schemabridge_reader`, read-only transactions, and 5000 ms timeout for every
  executed query case.

Candidate precision/recall/F1 remain `3/4 = 0.750`; the intentionally retained homonym false
positive demonstrates why a name or a short description is evidence, not semantic approval. These
figures are dirty-tree development evidence and must be regenerated from a clean release commit
before external publication.

## Current M27 evaluation evidence

M27 has separate provider-free and signed live-provider evidence. The final boundary pins prompt
`m27-openai-prompts-v16`, strict output schema `m27-query-studio-v10`, expansion contract
`m27-expansion-contract-v10`, slot-selection contract `m27-slot-selection-v4`, proposal normalizer
`m27-proposal-defaults-v3`, matcher `m27-deterministic-v9`, orchestration policy
`m27-local-analytical-preflight-v5`, OpenAI SDK `2.46.0`, and append-only campaign plan
`m27-cheapest-first-campaign-v11`.

The checked-in JSON report `reports/m27-query-studio-deterministic-evaluation.json` identifies:

- matcher `m27-deterministic-v8`;
- corpus SHA-256
  `6bb72e6efd4801bd5042e15a29616d634118d62e5f8a83b2f19d4691b1502ece`;
- report SHA-256
  `09afcbbc586b60bcbebfcc521d5ee35b86c2ed647513b15c7a1541ebfaed8fbf`;
- top-1 `56/62` (`0.903226`), top-3 and recall@20 `62/62`, and MRR `0.946237`;
- no-match specificity `31/31` and critical ambiguity recall `6/6`; and
- zero provider calls, zero provider tokens/cost, and zero ungoverned executable results.

This PASS proves the published synthetic retrieval gates only. It is not semantic approval,
production accuracy, or a production scale claim.

The signed v11 campaign separately selected `gpt-5-nano-2025-08-07`, the first and cheapest
candidate, after one qualification attempt and one retained full-corpus run. It stopped there; 5.4
Nano and Luna were not called and there is no runtime cascade.

| Scope | Result |
|---|---:|
| Observed synthetic cases | `136/136` expected outcomes |
| Positive recall@20 | `62/62` |
| Top-1 / top-3 / MRR | `56/62` / `62/62` / `0.946237` |
| Negative outcomes | `31/31` |
| Ambiguity trials | `18/18` |
| Exact typed core trials | `15/15` |
| Adversarial cases | `10/10` |
| Provider attempts | `16` |
| Input / output+reasoning tokens | `15,715` / `1,204` |
| Duration / calculated cost | `30,016 ms` / `EUR 0.001394085` |

The immutable whole-file SHA-256 is
`beef3391aef2ea2cedfdfbc6e065ce259f7143861ccc0470197c6edcb88ba30a`; the logical report digest is
`e13a63fd2add281e567e3b4e4086ba3bb8510ec6e554608533a4c532215c0b7f`; and the composite corpus
SHA-256 remains `a03d96b6ce0ed7172ffcf6a074f0d755dc62f2709c10fdce7607e1989614b1ba`.
Earlier signed campaigns remain immutable historical results under their superseded contracts and
are not merged into current metrics.

The final contract keeps model authority narrow: analytical expansion, source spans, semantic
owners/types, candidate ranking, filters, joins, and proposal defaults are server-owned.
Interpretation may emit only complete `slot_id`/`option_index=1` selections plus ambiguity. Only a
strict compatible top-1 option is exposed, so a tie remains ambiguity before provider I/O.

The accepted schema-v2 campaign/ledger attestation has whole-file SHA-256
`f4fceb557b8dd0a2ddf3c05a5f670ba2ea759f32273ef515e0daf0231a117053`.
It verifies the signed campaign and exactly 16 settled interpretation reservations/audits with
identical token totals, zero expansion calls, and no missing, orphan, mismatched, or open attempt.
Its unique matching is an authenticated ordinal correlation. Because the historical campaign did
not persist one common request nonce/fingerprint, the attestation deliberately does not claim
native content-derived case-to-request identity.

The fake-mode internal-browser record covers final UI behavior and protected-data checks. The
separately authorized live-UI smoke was blocked by the browser host's URL policy before submission,
so it made no provider request and is not a live-browser evaluation result. External AI was
returned to disabled policy v86.

M27 is accepted locally for synthetic milestone evidence. Production generalization, operated
provider/browser infrastructure, real tenant data/traffic, M28–M31, and external security/
operations review remain outside this evidence.

## Failures, difficult cases, and thresholds

The semantic fixture intentionally retains one homonym false positive and one hidden-synonym false
negative. They remain detailed `observed` cases; no threshold is enforced because seven tuned
synthetic examples do not justify a release gate. Operational failures—wrong join/cardinality,
typed-intent mismatch, compile/preview failure, row mismatch, missed security rejection, or wrong
recipe compatibility—fail the deterministic run and cause the CLI to exit nonzero after writing the
report.

## Live LLM separation

The default run always records a deterministic, key-free evaluation plus a separate `live_llm`
entry marked `not_run`. `--intent-adapter live` adds a required, separately labeled live intent run;
it never replaces or merges with deterministic metrics and never falls back when credentials or a
model are unavailable. Live results remain provider/model observations for this tiny fixture, not
deterministic regression evidence. M27 follows the same separation at its own boundary:
provider-free deterministic retrieval metrics and the signed v11 provider campaign are reported
independently; superseded campaign history never populates current metrics.

## Operator mutation check

Copy `demo/ground_truth/query_cases.yml`, change one expected value, and point a temporary loader or
test fixture at the copy. Do not edit accepted ground truth in place. The affected result case must
be `failed`, the normalized expected/actual rows must both remain visible, the JSON must still be
written, and the command must exit nonzero.
