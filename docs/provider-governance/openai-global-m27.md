# OpenAI global provider governance record — M27 synthetic evaluation

Status: approved only for the synthetic M27 evaluation and internal-browser acceptance

Reviewed: 2026-07-27

Endpoint scope: `global`

Fixed API origin: `https://api.openai.com`

First reviewed candidate snapshot: `gpt-5-nano-2025-08-07`

## Authorized purpose

This record authorizes the existing process-provided `OPENAI_API_KEY` only for SchemaBridge's
bounded, synthetic M27 matching and typed-intent evaluation. It does not authorize production
customer data, source rows, personal data, secrets, or a production release.

The operator must create a new versioned tenant-policy proposal if the endpoint scope, model,
prompt/schema fingerprint, retention posture, data categories, or intended purpose changes.

## Data allowed to leave SchemaBridge

- normalized synthetic business-request text;
- the minimum governed closure, bounded to 3 logical models, 12 fields, and 2 approved joins;
- approved public synthetic definitions and closed operation vocabularies;
- one server-numbered public option per unambiguous slot and a pseudonymous safety identifier.

The provider request excludes source rows and samples, source values, SQL, parameters, DSNs,
credentials, API keys, raw actor/workspace identifiers, OIDC claims, DataHub tokens, secret paths,
full catalog exports, and private reasoning. Sensitive-pattern screening fails closed before
egress. Physical-only discovery results remain non-executable and are never promoted to governed
model context.

## Enforced request controls

- fixed allowlisted origin `https://api.openai.com/v1`; caller-supplied URLs and proxy overrides
  are rejected;
- pinned snapshot `gpt-5-nano-2025-08-07`;
- exact tenant-scoped Nano configuration
  `f5fb0b20d85edca1a9780d76161342760f7d25ee4dc829fb686ec737f05cb0e7`,
  managed interpretation-stage configuration
  `5f1231bc9acd2dec58bd7b780785a26b80c0c935f512175bca6b5eaa5d4d52d1`,
  and provider contract
  `3231b06275a53db054b5e7417ccbcdc5b2a68dd67821cf60200e4590d43b380d`.
  These bind prompt `m27-openai-prompts-v16`, provider schema `m27-query-studio-v10`,
  local expansion contract `m27-expansion-contract-v10`, semantic-focus contract
  `m27-semantic-focus-v3`, slot-selection contract `m27-slot-selection-v4`, matcher
  `m27-deterministic-v9`, attempt policy `m27-durable-attempts-v3`, proposal canonicalizer
  `m27-proposal-defaults-v3`, and orchestration policy
  `m27-local-analytical-preflight-v5`;
- exact OpenAI SDK `2.46.0`; the composed interpretation response model declares every field
  required, including empty collections, and its local JSON schema must equal the strict schema
  transformed by that SDK before its bytes are fingerprinted and reserved. The historical
  expansion response schema remains testable but is not part of the public or live composition;
- Responses API strict structured output with extra fields forbidden;
- `store=false`, `background=false`, no tools, no redirects, TLS verification, bounded input,
  bounded output, timeout, quota, and concurrency;
- one invalid interpretation output may be settled as `invalid_output` and retried once
  with the identical input, model, endpoint, and configuration; the retry receives a new durable
  reservation, fence, idempotency digest, usage charge, and audit row;
- deterministic input/prompt-size failures and provider 4xx rejections are never retried; there is
  no repair prompt, model cascade, reuse of invalid output, or third attempt;
- both a short `field_match` request and analytical slot expansion pass the same local privacy
  boundary and produce exact server-owned probes without provider egress, durable admission,
  reservation, or provider usage. Analytical requests cannot be reclassified from their text;
  only the later interpretation stage may cross the provider boundary;
- model output is typed interpretation evidence only and is never executable SQL;
- analytical expansion is entirely local. The server derives every exact `slot_id`,
  `semantic_focus`, `owner_focus`, grounded `source_focus`, operational retrieval query, and
  unique contiguous source span from the required slot purpose plus the original request. No
  expansion query, source span, or candidate identity is requested from or returned by OpenAI;
- the server-owned source span is limited to eight source tokens and 256 characters and must
  preserve the relevant business entity, attribute or relationship, and any explicit governed
  filter literal. Missing, non-unique, invented, focus-changing, or entity-changing grounding is
  invalid output. Governed retrieval uses only the operational query derived from the closed
  focus, remains sentinel-bounded, and excludes physical inventory, unknown mappings, and
  whole-catalog fallback;
- the local expansion carries a closed required-purpose checklist. A genuinely atomic request
  with no analytical operation requires exactly one dimension probe with null metric, filter, and
  date-grain operations. Analytical requests cover every required metric, dimension, filter, and
  ordering purpose exactly enough to construct the bounded governed closure; unsupported or
  ungrounded shapes fail locally before retrieval or provider admission;
- an atomic field probe preserves both the named business entity and its attribute when both are
  present in the grounded source span. It may not collapse an entity-qualified concept such as a
  product activation state or order total into an unqualified generic attribute;
- owner, role, and canonical type become an exact logical-field allowlist before the bounded
  governed population is scored or cut to a result page. Each retrieval purpose preserves its own
  score and is anchored to its deterministic top-1 governed match. Interpretation receives exactly
  one server-numbered option only when that slot-specific compatible candidate has a strict score
  advantage. A top-score tie is `ambiguous` before provider interpretation; a disconnected leader
  fails closed and is never replaced merely to manufacture join connectivity. Alternatives remain
  visible to the human reviewer but their identifiers do not leave SchemaBridge;
- interpretation output contains only complete `slot_id` plus `option_index=1` selections and its
  explicit ambiguity state. The server maps that local index back to the exact candidate, derives
  any filter value from the request and governed allowed values, and rejects unknown indices,
  duplicate/partial slots, incompatible uses, conflicting literals, or extra output;
- the interpretation vocabulary carries exact server-derived counts for dimensions, metrics, and
  filters. An `aligned` proposal must match all three counts before and after deterministic
  canonicalization; omission, substitution by duplication, or any extra selection is invalid
  output and cannot reach confirmation;
- each approved join sent to interpretation carries its exact governed logical endpoints. When
  counting the primary entity, the server rejects a homologous identifier from the opposite join
  endpoint as an invalid shortcut; a distinct count of a different explicitly requested related
  identifier remains allowed inside the same approved closure;
- provider proposals pass through versioned deterministic normalization
  `m27-proposal-defaults-v3` before domain validation: already selected fields follow source
  mention order, the metric model is primary, unrequested aliases are removed, DATE/TIMESTAMP
  grouping receives the explicit requested grain or `DAY`, unrequested ordering becomes
  dimensions ascending, and an omitted limit becomes 500. Explicit operations, filters, ordering,
  and limits from 1 through 1,000 are preserved. The normalized proposal is visible, signed into
  confirmation, and may affect bounded rows under `LIMIT`;
- a normalized request consisting only of one bare physical-looking `*_id` or `*_key` leaf is
  classified as `ambiguous` before interpretation when the bounded governed shortlist proves more
  than one distinct eligible meaning. This narrow deterministic preclassification exposes the
  alternatives to human review; it neither approves a mapping nor changes matcher, closure, or
  execution thresholds;
- deterministic compilation, AST validation, human confirmation, and read-only execution remain
  independent downstream controls;
- sanitized append-only admission/usage audit stores no prompt, response, definitions, shortlist,
  credential, SQL, parameter, source value, or raw identity.

## Provider retention and residency truth

`store=false` is not Zero Data Retention. OpenAI states that API data is not used for training
unless the customer opts in, while abuse-monitoring logs may retain customer content for up to
30 days by default. Modified Abuse Monitoring and Zero Data Retention require provider approval
and separate configuration.

This policy uses the ordinary global API endpoint and makes no EU data-residency claim. OpenAI's
EU endpoint requires a project configured for European data residency and additional approved
data controls. A tenant that requires regional storage or processing must provision the matching
regional OpenAI project and apply a new exact SchemaBridge tenant policy; it must not reuse this
record or fingerprint.

## Cost and evaluation boundary

The M27 campaign is synthetic, nano-first, stops after the first passing model, and has aggregate
caps of 180 provider attempts, 250,000 input tokens, 40,000 output/reasoning tokens, and EUR 1.00
under conservative accounting. Each model first receives the same six-case qualification smoke,
whose current local/provider mix requires one base interpretation attempt. Every model that
qualifies receives the complete 17-base-attempt corpus until the first full PASS, with at most
three full runs. Each of the five critical analytical cases is evaluated once with its original
frozen text and once with each of two frozen holdout paraphrases. Across all three reviewed
candidates the worst-case base plan is 54 attempts, leaving 126 attempts of headroom. Input
reservations are derived from each exact serialized interpretation payload before egress. The
maximum two-attempt stage therefore reserves at most 79,844 input and 8,192 output/reasoning
tokens; its most expensive reviewed cost is EUR `0.141895600`. The evaluator still reserves
conservatively on unreported failure. Exhaustion fails closed without exceeding any aggregate cap.
There is no runtime model fallback. A second model requires a separate reviewed policy revision
and an HMAC-authenticated, content-addressed retained-campaign resume if the prior candidate does
not pass.

The synthetic workspace admission policy temporarily allows 4,250,000 input tokens and 200,000
output tokens per UTC calendar day, 1,000 requests per minute, and one concurrent attempt. The
larger daily input
ceiling accommodates the already charged conservative failed-output diagnostics, controlled
browser retries, one complete campaign, and the final synthetic browser smoke. It does not enlarge
the campaign caps above; the evaluator
independently stops at 250,000 input tokens, 40,000 output/reasoning tokens, 180 attempts, or
EUR 1.00.

The evaluator report uses schema v5 and rejects duplicate or omitted
`(suite, case_id, repetition, expected_outcome)` identities. Qualification and complete-corpus
matrices must be exact prefixes of the reviewed synthetic plan; metrics, failures, costs, and
budget flags are derived from those outcomes. Live checkpoints are HMAC-SHA256 authenticated with
a domain-separated, owner-only control-audit key, content-addressed, and checked as one monotonic
cheapest-first history. The key is never persisted in the report. This prevents ordinary report
editing, candidate omission, budget reset, fork, and rollback; exported archival and an independent
durable evidence service remain later operational controls.

Policy v18 initiated the first retained nano campaign under prompt
`m27-openai-prompts-v8-2eeb1e8332e07bfb` and configuration
`c0a339f2a01b1a461d432798ccd0b5d76f3859b46cb8e409920543c08f165a42`, before the
evaluator preserved exact structured-output failure classes. Its first case returned `no_match`;
the next case's two attempts were incorrectly summarized as `provider_unavailable`. The retained
record contains 3 attempts, 24,572 conservatively evaluated input tokens, 1,119 output/reasoning
tokens, and EUR 0.001843820. That append-only record is evidence of the evaluator-classification
defect, not a comparable or resumable provider qualification result and not evidence that the
model itself was unavailable.

Policy v19 re-tested the cheaper `gpt-5-nano-2025-08-07` candidate after the evaluator began
preserving exact invalid-output reason codes. It ranked the first positive case at position 1, then
returned `invalid_output` on both independently admitted attempts for the second positive case.
The retained campaign therefore records `quality_failed` and `awaiting_policy_revision`: 3
provider attempts, 24,572 conservatively evaluated input tokens, 1,120 output/reasoning tokens,
and EUR 0.001844260 under the campaign accounting policy. The attempts used only synthetic input
and produced no interpretation, SQL, source access, or execution.

Policy v20 then evaluated `gpt-5.4-nano-2026-03-17`. It ranked the registration-date case at
position 1, returned the wrong governed `no_match` for the product-key case, and exhausted both
permitted outputs as `invalid_output` on the negative case. Its qualification consumed 4 provider
attempts, 26,090 conservatively evaluated input tokens, 1,179 output/reasoning tokens, and EUR
0.007360925. It therefore also records `quality_failed` without interpretation, SQL, source
access, or execution.

Policy v21 evaluated `gpt-5.6-luna` and repeated the 5.4 pattern: registration date ranked at
position 1, product key returned the wrong governed `no_match`, and both negative-case attempts
were `invalid_output`. Its qualification used 4 attempts, 26,090 conservatively evaluated input
tokens, 1,182 output/reasoning tokens, and EUR 0.036500200. The closed campaign therefore ended
`qualification_failed` after 11 cumulative attempts and EUR 0.045705385; no model was selected.

This revision changes the exact expansion prompt and grounding behavior rather than weakening the
quality gate. Prompt v9 permits a genuinely atomic single-field request to preserve its exact
source-span wording, expands the closed bilingual provenance vocabulary, rejects full grouped or
filtered spans, and adds the governed source-span retrieval described above. Deterministic
evaluation after the change remains at recall@20 1.0, top-3 1.0, specificity 1.0, ambiguity 1.0,
top-1 0.903226, and MRR 0.951613. Because the exact configuration changed, the prior model results
remain immutable historical evidence and the nano-first qualification restarts at
`gpt-5-nano-2025-08-07`; it is not a runtime cascade or a retroactive pass.

Policy v23 completed that new nano qualification under the exact v9 configuration. The
registration-date case ranked first, the product-key case ranked second, and the negative
employee-id case correctly returned `no_match`; the same-name cross-connection ambiguity case
exhausted its two independently admitted outputs as `invalid_output`. The retained result is
therefore `quality_failed` after 7 attempts, 51,868 conservatively evaluated input tokens, 2,434
output/reasoning tokens, and EUR 0.003923700. No model, interpretation, SQL, source access, or
execution was selected. This revision advances the retained campaign to the next reviewed
candidate, `gpt-5.4-nano-2026-03-17`, under its separately pinned configuration; it remains an
offline qualification sequence and does not enable runtime fallback.

Policy v24 then evaluated `gpt-5.4-nano-2026-03-17` under its exact v9 configuration. It ranked the
registration-date case first, correctly returned `no_match` for employee id, resolved the
cross-connection ambiguity, and blocked the sensitive-input case without provider egress. It
reported `no_match` for the product-key case and returned `closure_overflow` for the
revenue-by-date-and-category core case. The retained `quality_failed` result used 6 attempts,
9,628 conservatively evaluated input tokens, 509 output/reasoning tokens, and EUR 0.002818035.
The cumulative v9 campaign remains within budget at 13 attempts, 61,496 input tokens, 2,943
output/reasoning tokens, and EUR 0.006741735. No model, interpretation, SQL, source access, or
execution was selected. This revision advances the same retained offline campaign to the final
reviewed candidate, `gpt-5.6-luna`, under its separately pinned configuration; runtime fallback
remained disabled.

Policy v25 evaluated `gpt-5.6-luna` and reproduced the 5.4 outcomes: registration date, employee
id, ambiguity, and sensitive blocking passed; product key was absent from the qualification
ranking and the core request returned `closure_overflow`. Its retained `quality_failed` result
used 6 attempts, 9,623 conservatively evaluated input tokens, 542 output/reasoning tokens, and EUR
0.014162500. The closed v9 campaign used 19 attempts, 71,119 input tokens, 3,485 output/reasoning
tokens, and EUR 0.020904235, and selected no model.

Post-campaign diagnostics found two evaluator/orchestration defects without reading or persisting
provider output. First, the qualification ranking searched only the provider's canonical query,
whereas production also performs the bounded unfiltered and exact grounded source-span searches;
the reported product-key `no_match` therefore did not evaluate the production retrieval path.
Second, the core case had a unique correct top-1 branch inside the 3-model/12-field/2-join limits,
but an equal-score tie between second- and third-ranked shipment-date alternatives outside that
branch was prematurely classified as `candidate_tie_overflow`. Evaluation plan
`m27-cheapest-first-smoke-v2` now shares the exact production probe union, including its 21st-row
sentinel, and the closure keeps fail-closed boundary ties unless the entire tied group is outside
the unique coherent top-1 branch. Global 20/21 ties, top-1 ambiguity, unknown fields, disconnected
models, and the 3/12/2 limits remain unchanged. The prior reports stay immutable, but their two
affected failures are not comparable quality evidence. The corrected campaign therefore restarts
nano-first under the unchanged exact provider prompt/schema and a new reviewed policy revision.

Policy v26 started that corrected v2 campaign with `gpt-5-nano-2025-08-07`. Both independently
admitted outputs for the first registration-date case failed structured-output validation, so the
retained result is `quality_failed` after 2 attempts, 23,568 conservatively evaluated input
tokens, 1,024 output/reasoning tokens, and EUR 0.001746800. No retrieval result, model selection,
interpretation, SQL, source access, or execution followed. This revision advances the retained v2
campaign to `gpt-5.4-nano-2026-03-17` under its separately pinned configuration; runtime fallback
remains disabled.

Policy v27 evaluated `gpt-5.4-nano-2026-03-17` under evaluation plan v2. Registration date,
employee id, cross-connection ambiguity, and pre-egress sensitive blocking passed. Product key
still returned no ranked target because the provider shortened its grounded excerpt past the
sales-line qualifier. The core request reached interpretation but produced a different
fingerprint after its expansion omitted the second explicit dimension. The retained
`quality_failed` result used 7 attempts, 11,590 conservatively evaluated input tokens, 811
output/reasoning tokens, and EUR 0.003664925. The cumulative v2 campaign used 9 attempts, 35,158
input tokens, 1,835 output/reasoning tokens, and EUR 0.005411725. An internal-browser diagnostic
confirmed the same typed ambiguity without SQL, source access, execution, or raw provider-output
retention.

Expansion contract v5 and prompt v10 address those two bounded failures without changing scoring,
candidate thresholds, or the 3/12/2 closure. A trusted server-derived checklist now preserves
purpose multiplicity, so two explicit dimensions or metrics require two separately grounded
probes. For a genuinely single-field atomic description, SchemaBridge first validates the
provider's original grounding and then deterministically preserves the complete user phrase as
the retrieval span when it fits the 256-character bound; relationship qualifiers such as
“recorded on the sales line” are no longer discarded. Date-and-time phrases remain one temporal
concept. The exact provider configuration therefore changed and all v9 evidence remains
immutable; a new v10 campaign restarts nano-first under a new reviewed policy.

Policy v28 evaluated the first v10 candidate, `gpt-5-nano-2025-08-07`. Both independently
admitted outputs for the first registration-date qualification case failed structured-output
validation. The retained `quality_failed` result used 2 attempts, 24,368 conservatively evaluated
input tokens, 1,024 output/reasoning tokens, and EUR 0.001790800. No retrieval result, model
selection, interpretation, SQL, source access, or execution followed, and no raw provider output
was retained. This revision advances the same retained offline campaign to
`gpt-5.4-nano-2026-03-17` under its separately pinned v10 configuration; runtime fallback remains
disabled.

Policy v29 evaluated `gpt-5.4-nano-2026-03-17`. Registration date, the negative employee case,
cross-connection ambiguity, and pre-egress sensitive blocking passed. The product-reference case
still produced no ranked target, while the three-model revenue case aligned and confirmed but
did not match the exact deterministic request fingerprint. The retained `quality_failed` result
used 8 attempts, 25,465 conservatively evaluated input tokens, 2,858 output/reasoning tokens, and
EUR 0.009532050. Together, the v10 campaign used 10 attempts, 49,833 input tokens, 3,882
output/reasoning tokens, and EUR 0.011322850. No model was selected and no SQL, source access, or
execution followed.

Internal-browser diagnosis separated both failures from provider availability. First, the exact
short description reached deterministic retrieval, but the matcher treated `reference` as
different from the governed identifier concept and counted the generic word `recorded`; with
deliberately sparse physical metadata this pushed the correct sale-line product key below the
unchanged 70% evidence threshold. Matcher v6 now maps only the bounded
`ref`/`reference`/`referencia` vocabulary to `identifier` and treats `recorded` as a generic
stopword. It does not lower the threshold: order references remain explicitly ambiguous and an
unrelated support-ticket reference remains no-match.

Second, the browser showed that the revenue request selected the correct governed category,
order timestamp, and net-amount metric. The mismatch came from model-selectable defaults that the
user had not requested: candidate ordering, time grain, alias, sort, and row limit. Shared
canonicalizer `m27-proposal-defaults-v2` now runs for both the key-free fake and live adapter before
fingerprinting. It orders already-selected fields by their source mention, chooses the metric's
model as primary, derives an explicit Spanish/English date grain or defaults DATE/TIMESTAMP
grouping to day, removes unrequested metric aliases, defaults unrequested ordering to dimensions
ascending, and uses limit 500 unless the text explicitly supplies a value from 1 through 1,000.
Candidate selection, operations, and filter values remain unchanged and still require human
confirmation.

The complete deterministic corpus after these changes still passes: recall@20 1.0, top-3 1.0,
specificity 1.0, ambiguity 1.0, top-1 0.903226, and MRR 0.946237. Because matcher and proposal
behavior are part of the exact configuration, the v10 reports remain immutable and a new
nano-first campaign restarts at `gpt-5-nano-2025-08-07`; this is an offline evaluation revision,
not runtime model fallback.

Policy v30 began that corrected campaign. The nano registration-date case ranked first. During
the product-reference case, the workspace's prior same-day diagnostics exhausted the 2,000,000
input-token admission ceiling before the stage could complete its independently admitted retry.
The retained operational result is `quota_exhausted`, not a model-quality failure: 2 provider
attempts, 13,858 conservatively evaluated input tokens, 612 output/reasoning tokens, and EUR
0.001031470. No model, interpretation, SQL, source access, or execution was selected. The daily
ceiling is therefore raised to 3,000,000 only for this synthetic workspace so the already bounded
250,000-input-token campaign and final browser smoke can finish. Campaign attempt, token, and
EUR 1.00 caps remain unchanged, and a fresh nano-first run is required because a
quota-terminated partial qualification is not resumable evidence.

Policy v31 repeated the corrected nano qualification under the exact matcher-v6 and
canonicalizer-v2 configuration. Registration date ranked first, while both independently admitted
outputs for the product-reference case failed strict structured-output validation. The retained
`quality_failed` result used 3 attempts, 26,047 conservatively evaluated input tokens, 1,119
output/reasoning tokens, and EUR 0.001924945. It selected no model and produced no interpretation,
SQL, source access, or execution. The cheapest candidate is therefore rejected on quality rather
than cost or availability. This revision advances the retained offline campaign to
`gpt-5.4-nano-2026-03-17` under its separately pinned exact configuration; runtime model fallback
remains disabled.

Policy v32 evaluated `gpt-5.4-nano-2026-03-17`. It passed all six qualification cases, including
the corrected product-reference retrieval and exact three-model core fingerprint, in 8 provider
attempts, 25,464 input tokens, 2,855 output/reasoning tokens, and EUR 0.009527705. The complete
corpus then exposed three positive failures before the fail-fast quality gate stopped further
egress: both Spanish and English `Product.is_active` descriptions returned `no_match`, and the
Spanish `SalesOrder.total_amount` case exhausted strict structured-output validation. That partial
full run used 40 attempts, 87,706 input tokens, 4,171 output/reasoning tokens, and EUR 0.025030445.
Together with v31, the retained campaign used 51 attempts, 139,217 input tokens, 8,145
output/reasoning tokens, and EUR 0.036483095. The model was not selected and no SQL, source access,
execution, or semantic approval followed. Metrics for suites not reached after fail-fast are zero
by construction and are not evidence about those suites.

The six-case qualification remains bounded to seven base attempts and the campaign remains capped
at 179 planned base attempts. Evaluation plan `m27-cheapest-first-smoke-v3` replaces only its two
positive smoke cases with the failures that escaped v2: Spanish `Product.is_active` and Spanish
`SalesOrder.total_amount`. The negative, cross-connection ambiguity, three-model core, and
pre-egress adversarial cases are unchanged; every complete-corpus threshold is unchanged. This
makes the cheap screen more predictive without weakening quality or enlarging cost, token, or
attempt limits. Because the exact evaluation plan changed, the v2 campaign remains immutable and
a fresh v3 campaign restarts nano-first; this is offline model selection, never runtime fallback.

Policy v33 ran the first v3 qualification against `gpt-5-nano-2025-08-07`.
`Product.is_active` ranked first, `SalesOrder.total_amount` ranked fourth rather than inside the
required top three, and the negative employee-id case exhausted both strict structured-output
attempts as invalid output. The fail-fast retained result is `quality_failed`: 4 provider attempts,
27,691 input tokens, 1,325 output/reasoning tokens, and EUR 0.002106005. No full corpus, model
selection, interpretation, SQL, source access, execution, or semantic approval followed. This
revision advances the retained v3 campaign to `gpt-5.4-nano-2026-03-17` under its separately pinned
configuration; runtime fallback remains disabled.

Policy v34 evaluated `gpt-5.4-nano-2026-03-17` under the v3 smoke. Both difficult positive cases
ranked first and the employee-id negative returned `no_match`. The cross-connection homonym then
exhausted its three independently admitted strict outputs as invalid output, so the qualification
failed before the core case or complete corpus. The model-specific increment was 6 provider
attempts, 30,898 input tokens, 4,415 output/reasoning tokens, and EUR 0.012868185. The cumulative
v3 campaign remains within budget at 10 attempts, 58,589 input tokens, 5,740 output/reasoning
tokens, and EUR 0.014974190. No model, SQL, source access, execution, or semantic approval was
selected. This revision advances the retained campaign to the final reviewed candidate,
`gpt-5.6-luna`, under its separately pinned exact configuration; runtime fallback remains
disabled.

Policy v35 evaluated the final reviewed candidate, `gpt-5.6-luna`. The product-active case returned
`no_match`, the order-total and employee-id cases passed, and the cross-connection homonym
exhausted its three strict interpretation outputs as invalid. The Luna increment was 6 provider
attempts, 30,898 input tokens, 4,410 output/reasoning tokens, and EUR 0.063093800. The closed v3
campaign therefore selected no model after 16 cumulative attempts, 89,487 input tokens, 10,150
output/reasoning tokens, and EUR 0.078067990. It remained inside every cap and produced no SQL,
source access, execution, or semantic approval. External AI is not eligible for production on this
evidence; fake/guided behavior remains the safe default while the exact prompt and closure
diagnosis is reviewed.

The v35 diagnosis used only sanitized typed outcomes and retained metadata, never raw provider
output. The product-active failure showed that expansion could discard an entity qualifier from
an otherwise atomic entity-plus-attribute description. The cross-connection homonym reached
interpretation with a reduced vocabulary even though the bounded shortlist already demonstrated
multiple governed meanings for the same bare physical-looking identifier. Prompt v11 therefore
requires an atomic probe to preserve both entity and attribute, while the application now returns
deterministic `ambiguous` before interpretation only for a request made solely of one bare
`*_id`/`*_key` leaf with more than one distinct governed shortlist alternative. Product-qualified
identifiers and analytical requests continue through the normal bounded path. Matcher scoring,
the 70% evidence gate, the 3-model/12-field/2-join closure, human confirmation, compiler, AST
guard, and source controls are unchanged. The v3 quality thresholds are also unchanged. All v10
results remain immutable historical evidence; the exact v11 configuration starts a fresh
nano-first offline campaign rather than a runtime fallback or a retroactive pass.

Policy v36 evaluated `gpt-5-nano-2025-08-07` under that exact v11 configuration.
`Product.is_active` and `SalesOrder.total_amount` both ranked first, and the employee-id negative
correctly returned `no_match`. The same-name cross-connection case exhausted two strict expansion
outputs as invalid before the post-expansion bare-identifier classification could run. The
retained `quality_failed` result used 6 provider attempts, 43,871 input tokens, 1,879
output/reasoning tokens, and EUR 0.003239665. No interpretation, model selection, SQL, source
access, execution, or semantic approval followed. This exposed an orchestration-placement defect:
the bounded literal homonym check must run before any provider expansion if it is intended to make
that case provider-independent. The v36 record remains immutable and is not resumed against a more
expensive model; a newly fingerprinted orchestration revision must restart nano-first.

Orchestration policy `m27-bare-identifier-preflight-v1` now places that proof before
`expansion.expand`. It issues exactly one raw, unfiltered governed search for the normalized
literal, reads at most 21 rows including the completion sentinel, validates the current
registry/scope and every returned binding, and returns `ambiguous` only when the bounded evidence
contains more than one distinct governed logical/connection/physical meaning for the exact leaf.
In that outcome the preview contains no expansion, interpretation, or provider usage. A qualified
identifier phrase does not match the narrow bare-leaf trigger and retains the normal expansion plus
interpretation path. The orchestration version is now a first-class field in
`ProviderConfigurationFacts`, so policy v36 no longer matches the runtime even though the provider
prompt/schema bytes are unchanged. The next exact policy revision therefore restarts Nano against
the unchanged v3 quality gates and a new governed configuration fingerprint.

Policy v37 ran that fresh Nano qualification. Both difficult positives ranked first, the employee
negative returned `no_match`, and the same-name cross-connection case returned deterministic
`ambiguous` with zero provider attempts. The three-model revenue core then exhausted four
independently admitted expansion/interpretation attempts as strict invalid output. The retained
`quality_failed` result used 8 provider attempts, 61,095 input tokens, 5,674 output/reasoning
tokens, and EUR 0.005856785. It stayed within every campaign bound and produced no selected model,
SQL, source access, execution, or semantic approval. Nano is therefore rejected on the unchanged
quality gate. The same retained offline campaign advances to the separately pinned
`gpt-5.4-nano-2026-03-17` configuration; runtime model fallback remains disabled.

Policy v38 qualified `gpt-5.4-nano-2026-03-17` on all six smoke cases: both positives ranked
first, the employee negative and pre-egress sensitive case passed, the bare homonym was
deterministically ambiguous without provider use, and the three-model core matched its exact
fingerprint. Qualification used 6 provider attempts, 23,337 input tokens, 2,724
output/reasoning tokens, and EUR 0.008879640. The complete corpus then passed its first three
positive descriptions before the tenant's same-day 3,000,000-input-token admission ceiling was
reached; that partial full run used 3 attempts, 5,393 input tokens, 254 output/reasoning tokens,
and EUR 0.001535710. The terminal campaign outcome is therefore `quota_exhausted`, not a quality
failure: cumulative usage was 17 attempts, 89,825 input tokens, 8,652 output/reasoning tokens, and
EUR 0.016272135, with no selected model, SQL, source access, execution, or semantic approval.

The daily ledger already contained 2,955,875 charged input tokens from the bounded historical
diagnostics above. The synthetic workspace ceiling is raised to 3,500,000 solely to leave enough
admission room for one fresh complete campaign and the final bounded browser smoke. The independent
campaign limits remain 180 attempts, 250,000 input tokens, 40,000 output/reasoning tokens, and EUR
1.00. Because a terminal quota result is not resumable evidence, the new campaign restarts from
Nano under the same prompt, schema, matcher, orchestration, and quality gates; this does not
reinterpret v38 as a pass or enable runtime fallback.

Policy v39 restarted the corrected campaign with `gpt-5-nano-2025-08-07` under the enlarged
synthetic admission ceiling. It again passed both difficult positives, the employee negative, and
the zero-provider cross-connection ambiguity. The three-model revenue core exhausted three
independently admitted strict outputs as invalid. The retained `quality_failed` result used 7
provider attempts, 48,182 input tokens, 5,154 output/reasoning tokens, and EUR 0.004917770. It
selected no model and produced no SQL, source access, execution, or semantic approval. Nano is
therefore rejected again on quality; the retained offline campaign advances to the exact
`gpt-5.4-nano-2026-03-17` configuration with the 3,500,000 daily admission ceiling unchanged.

Policy v40 evaluated `gpt-5.4-nano-2026-03-17` in that retained campaign. The two positive cases,
employee negative, zero-provider homonym, and pre-egress sensitive case passed; the three-model
core produced a valid typed request whose fingerprint did not equal the exact expected request.
The `quality_failed` increment used 6 provider attempts, 22,104 input tokens, 1,109
output/reasoning tokens, and EUR 0.006387755. Cumulative campaign usage is 13 attempts, 70,286
input tokens, 6,263 output/reasoning tokens, and EUR 0.011305525. The model is rejected because one
earlier qualification success does not establish stable exactness. No model, SQL, source access,
execution, or semantic approval was selected. The retained offline campaign advances to its final
reviewed candidate, `gpt-5.6-luna`; runtime fallback remains disabled.

Policy v41 evaluated `gpt-5.6-luna`. Order-total, the employee negative, zero-provider homonym,
three-model core, and pre-egress sensitive case passed, but product-active returned `no_match`.
The Luna increment used 5 provider attempts, 9,270 input tokens, 628 output/reasoning tokens, and
EUR 0.014341800. The closed campaign selected no model after 18 cumulative attempts, 79,556 input
tokens, 6,891 output/reasoning tokens, and EUR 0.025647325. It remained inside every cap and
produced no SQL, source access, execution, or semantic approval.

A contract audit based only on those sanitized outcomes found two under-specified structural
rules. First, the prompt says an atomic field description is one probe, but validation still
accepted multiple individually atomic probes when the server-derived analytical-purpose checklist
was empty; that can separate an entity from its attribute before retrieval. Second, interpretation
candidates carried allowed uses but the vocabulary did not expose or enforce the exact number of
dimensions, metrics, and filters derived from expansion. That allowed a structurally valid core
proposal to omit or add one selection and still reach fingerprint comparison. The next revision
will make both cardinalities explicit and locally validated without selecting a field, lowering a
score, expanding the 3/12/2 closure, or changing confirmation/SQL/source controls. All v11 reports
remain immutable; a changed prompt/schema/configuration must restart Nano-first.

Prompt v12 and schema v6 close those two structural gaps without changing matcher scores, quality
thresholds, retrieval limits, or downstream authority. A non-analytical atomic description now
has the server-derived checklist `[dimension]` and must return exactly one grounded dimension
probe with null operation fields while preserving both entity and attribute. The interpretation
vocabulary now carries exact server-derived dimension, metric, and filter counts; the application
and live adapter reject an `aligned` proposal that differs from those counts both before and after
`m27-proposal-defaults-v2` canonicalization. The exact Nano configuration is
`01c6de51c9d366035e3912373d4353f77ed17cc8433771be99837897cc41225e`,
with expansion fingerprint
`1867eaf280922a8b0ea4bcb4de92e1ea7036c400dd3db257296fd2990f56ea6f`,
interpretation fingerprint
`d6accb71bb91614bb96f63edf7d4ee84d61932d8cdae1910a0d69719347f3d4c`,
and governed facts fingerprint
`8db1dcea8c0a4ea74a4357ba19b02a8b30a2ffba7226a8d5e190e7c67744da39`.
The unchanged campaign caps admit at most 45,827 input tokens for expansion and 44,986 for
interpretation; the worst single-stage conservative cost is EUR 0.127853000. Provider-free
regressions pass 199 focused tests and 200 boundary/browser/evaluator tests, including explicit
atomic split, lost qualifier, missing-selection, and extra-selection failures. No provider call
occurred while deriving or testing these bytes. The next exact policy revision must therefore
restart at Nano; it may not resume or reinterpret the terminal v11 campaign.

Policy v42 started that fresh v12 campaign with `gpt-5-nano-2025-08-07`. Both difficult atomic
positives ranked first, the employee negative returned `no_match`, and the same-name
cross-connection case returned deterministic `ambiguous` with zero provider use. The three-model
revenue core exhausted its two independently admitted strict outputs as
`query_studio_provider_invalid_output`, so the fail-fast qualification stopped before the final
pre-egress case. Nano is rejected on the unchanged quality gate. The retained increment used
6 provider attempts, 45,415 input tokens, 1,819 output/reasoning tokens, and EUR 0.003298185,
with no unknown/cross-tenant candidate, model cascade, SQL/tool/approval output, source access,
execution, semantic approval, or selected model. A separately governed policy revision advances
the retained offline campaign to `gpt-5.4-nano-2026-03-17`; runtime fallback remains disabled.

Policy v43 qualified `gpt-5.4-nano-2026-03-17` on all six smoke cases. Both difficult atomic
positives ranked first; the employee negative, zero-provider homonym, exact three-model core, and
pre-egress sensitive case all passed. Qualification used 5 provider attempts, 9,684 input tokens,
671 output/reasoning tokens, and EUR 0.003053105. The complete corpus then passed its first four
atomic positives at rank 1, but `customer-country:es` exhausted two strict outputs as
`query_studio_provider_invalid_output`. Evaluation stopped at that first failure. The displayed
`4/62` positive metrics treat all unattempted cases as failures and are therefore a conservative
fail-fast gate result, not a measured estimate over the unexecuted corpus. The terminal campaign
used 17 cumulative attempts, 89,057 input tokens, 3,847 output/reasoning tokens, and
EUR 0.015687925. No model was selected and no unknown/cross-tenant candidate, model cascade,
SQL/tool/approval output, source access, execution, or semantic approval occurred. The live policy
must be disabled while the atomic-description path is audited; repeatedly sampling another model
would not establish deterministic production behavior.

Policy v44 disabled external AI after that terminal v43 result. It preserved the exact immutable
configuration and 3,500,000-token daily ceiling only as historical policy evidence; it authorized
no further provider call. The local guided path, physical discovery, deterministic compiler, AST
guard, and read-only recorded execution remained available.

The subsequent v13 contract audit separated two nominal routes instead of inferring a bypass from
untrusted text. `field_match` now performs one privacy-screened, exact local probe and records no
provider attempt; `analytical` always follows durable admission and the two-stage typed provider
path. Successful provider usage must exactly match stage, model, governed configuration, outcome,
and admitted token bounds before settlement. The returned settlement acknowledgement must match
the exact reservation, terminal state, outcome, and charges before the application may return a
provider result.

The live-evaluation plan is now `m27-cheapest-first-campaign-v4`. It evaluates a complete,
connection-aware corpus whose SHA-256 is
`452f9f07690341bc4f357b26a940b5be2a4b89faf5d1b767465a2b3f169a9063`.
The exact governed configuration fingerprints are:

- `gpt-5-nano-2025-08-07`:
  `d7aadb728a87257d9e4ea087580f4591d85b75d04dc53b2d9a5cfeaed2a6d1f5`;
- `gpt-5.4-nano-2026-03-17`:
  `2f4a395d28cd9799d8564b3275612c6bb35fda94ec5059b809d1870044ca06b1`;
- `gpt-5.6-luna`:
  `223ba4721a47ad4994e2f899c6b0829e2d32820bcd58a5675e3d0f25ab88ef5f`.

Control-plane migrations v7 and v8 harden this boundary without changing migrations v1–v6. Their
SHA-256 values are respectively
`5dbc079a995b5fe0373ada20264c20d64c657259e10f77839f1f0805449fbcec` and
`77b84a021f1b99a140236333b1f1f79a11893f771046fe14490886fc00b57708`.
Schema v8 recomputes terminal settlement/audit derivations before upgrade, serializes reservation,
admission-state, and daily accounting locks, captures lease clocks after waits, validates
capability/fence/usage before accounting locks, and reasserts exact least-privilege function ACLs.
No provider call occurred while implementing or testing these v13/v8 changes.

The next policy revision may temporarily raise only the synthetic workspace's daily input ceiling
to 4,000,000 and authorize Nano under the exact v13 fingerprint above. The independent 180-attempt,
250,000-input, 40,000-output/reasoning, and EUR 1.00 campaign caps remain unchanged. A provider
result will be appended here before any policy revision for a more expensive model or before the
policy is disabled again.

Policy v45 authorized `gpt-5-nano-2025-08-07` under that exact v13 configuration and the temporary
4,000,000-token synthetic ceiling. The six-case qualification completed within every bound but
returned `quality_failed`: both local `field_match` positives (`Product.is_active` and
`SalesOrder.total_amount`, Spanish) returned `no_match` without provider egress, while the negative,
cross-connection ambiguity, three-model analytical core, and pre-egress sensitive case passed.
Only the analytical core used the provider: 2 attempts, 4,067 input tokens, 453 output/reasoning
tokens, 4,991 ms, and EUR 0.000423005. There were zero unknown or cross-tenant candidates, model
cascades, SQL/tool/approval outputs, source accesses, executions, or semantic approvals.

Because the two failures occurred in the new model-independent local route, this result is an
orchestration defect rather than evidence that a more expensive model would improve them. The
campaign is not resumed against 5.4 Nano or Luna. External AI is disabled while the exact
field-match retrieval path is corrected and re-evaluated; the signed v45 report remains immutable
failure evidence.

Policy v46 disabled external AI immediately after v45. The read-only diagnosis then proved that
the production matcher had returned the correct governed bindings: the evaluator labelled them
`no_match` because the corpus expected connection `recorded-synthetic`, while the retained
PostgreSQL fixture correctly identifies the same synthetic warehouse as `warehouse-primary`.
This was an evidence-identity defect, not a matcher threshold, mapping, provider, or orchestration
failure. No score, shortlist, approval, prompt, provider configuration, or runtime behavior was
changed.

The recorded catalog and corpus now share the same explicit synthetic primary-connection identity,
`warehouse-primary`; the shadow homonym remains `warehouse-shadow`. The corrected corpus SHA-256
is `6bb72e6efd4801bd5042e15a29616d634118d62e5f8a83b2f19d4691b1502ece`.
Provider-free regressions require the two v45 positives inside the top three and bind the corpus,
recorded catalog, and browser fixture to the same connection identity.

That cross-runtime regression also exposed a legitimate sparse-metadata case: the retained
PostgreSQL definition for `Product.is_active` did not contain the English word `flag`, so the
Spanish concept `indicador` initially overlapped two of three normalized concepts and remained
below the unchanged 70% evidence gate. Matcher v7 resolves the concept, rather than weakening the
gate or homogenizing source metadata, by canonicalizing `indicador`, `flag`, and the governed
BOOLEAN type to the same `boolean` token. This still ranks only already-approved governed
bindings and creates no mapping or join authority. Complete provider-free evaluation remains
top-1 `0.903226`, top-3/recall@20 `1.0`, MRR `0.946237`, specificity `1.0`, and ambiguity recall
`1.0`.

Matcher v7 changes the governed provider facts fingerprints to:

- Nano: `64b277f1e67f5e66cf10b67ec2fc8cb13271b224ed3d07854aaaac45cf85880e`;
- 5.4 Nano: `998d6e0853b825aa7ca36eac365f82405d7012d957dbb7947e6aee5c2d26d0b4`;
- Luna: `303e9b13d0b1e980cb01cf698f8f6b6ba18fa69faef64a6c47a3e0f61811f403`.

Because both corpus identity and matcher facts changed, v45 cannot be resumed or used as
model-quality evidence. The corrected campaign must restart Nano-first under a new exact policy
revision.

Policy v47 restarted the corrected campaign with `gpt-5-nano-2025-08-07`. Both local
field-description positives ranked inside the required top three, the employee negative returned
`no_match`, and the cross-connection homonym returned `ambiguous`, all without provider egress.
The three-model revenue core then exhausted its four independently admitted
expansion/interpretation attempts as strict invalid output, so fail-fast stopped before the final
pre-egress adversarial case. This is a genuine Nano quality failure under matcher v7: 4 attempts,
44,581 input tokens, 4,805 output/reasoning tokens, 8,662 ms, and EUR 0.004566155.
There were zero unknown or cross-tenant candidates, model cascades, or SQL/tool/approval outputs.
The signed campaign remains within every bound and advances to the separately governed
`gpt-5.4-nano-2026-03-17` configuration; runtime fallback remains disabled.

Policy v48 resumed the signed corrected campaign with `gpt-5.4-nano-2026-03-17`. The model
qualified on all six smoke cases: both local field descriptions ranked first, the negative and
cross-connection ambiguity passed locally, the exact revenue-core fingerprint matched, and the
pre-egress sensitive case was blocked. Qualification used 3 provider attempts, 18,777 input
tokens, 2,489 output/reasoning tokens, 6,694 ms, and EUR 0.007553315.

The complete corpus preserved the deterministic field-matching gate at top-1 `0.903226`, top-3
and recall@20 `1.0`, MRR `0.946237`, specificity `1.0`, and ambiguity recall `1.0`. It did not,
however, meet the analytical stability gate. `secondary-holders-by-registration-date` matched
twice and returned `ambiguous` once; `active-customers-by-country` returned `no_match` twice and
one strict invalid output. The fail-fast lower bound is therefore 2 exact successes out of the
15 planned analytical trials (`0.133333`). The 5.4 full-corpus increment used 10 provider
attempts, 42,570 input tokens, 2,414 output/reasoning tokens, 27,913 ms, and EUR 0.012684650.
The retained campaign totals are 17 attempts, 105,928 input tokens, 9,708 output/reasoning
tokens, 43,269 ms, and EUR 0.024804120, within all independent campaign caps.

There were zero unknown or cross-tenant candidates, model cascades, or SQL/tool/approval outputs.
No model is selected. This is a genuine 5.4 Nano quality failure, so a separately reviewed policy
revision may advance the signed campaign to `gpt-5.6-luna`; runtime fallback remains disabled.

Policy v49 resumed the same signed campaign with `gpt-5.6-luna`. Luna qualified on all six smoke
cases, including the exact revenue-core fingerprint and the pre-egress sensitive block, in
2 provider attempts, 4,069 input tokens, 384 output/reasoning tokens, 4,085 ms, and
EUR 0.007010300. In the complete corpus, all three repetitions of
`secondary-holders-by-registration-date` matched the expected typed plan. The first
`active-customers-by-country` repetition then exhausted two strict outputs as
`query_studio_provider_invalid_output`, and fail-fast stopped the run. The resulting conservative
lower bound is 3 exact successes out of 15 planned analytical trials (`0.200000`).

The Luna full-corpus increment used 9 provider attempts, 52,133 input tokens,
2,750 output/reasoning tokens, 14,976 ms, and EUR 0.075496300. Across the complete three-model
campaign, 28 attempts used 162,130 input tokens, 12,842 output/reasoning tokens, 62,330 ms, and
EUR 0.107310720. All independent limits held. There were zero unknown or cross-tenant candidates,
model cascades, or SQL/tool/approval outputs; no model was selected.

All three governed candidates therefore fail the unchanged analytical stability gate. Further
sampling under this prompt/schema contract is not authorized. External AI must be disabled while
the typed intent contract and deterministic candidate/orchestration boundary are diagnosed.

Policy v50 disabled external AI after the terminal v49 campaign. Its immutable policy fingerprint
is `ebf9d4675bb4bbcb73905ab683730552179d06f6c6353e06f21db48e079ecc80`.
The exact retained Luna configuration is historical evidence only; it authorizes no provider
request. A content-free audit grouped by model, stage, and terminal outcome showed that strict
invalid output was concentrated in expansion (Nano 1, 5.4 Nano 2, Luna 3), while interpretation
also failed twice for Nano and once for 5.4 Nano. No prompt text, provider output, identifier,
credential, or row value was read or recorded during that diagnosis.

The v14 boundary closes the diagnosed capacity defect without weakening schema validation.
OpenAI's Responses contract counts both visible output and reasoning inside
`max_output_tokens`, but the previous expansion and interpretation ceilings were respectively
512 and 1,536 tokens. Both typed stages may need to emit a complete strict-schema object after
reasoning. They now receive an identical hard ceiling of 4,096 tokens per attempt, reject 4,097,
and reserve 8,192 output/reasoning tokens before a two-attempt stage. The independent campaign
cap remains 40,000 output/reasoning tokens, leaving 31,808 tokens after admitting one stage; the
worst bounded campaign cost is EUR 0.154886600. Durable admission, exact usage settlement,
strict-schema parsing, fail-fast behavior, no fallback, and every downstream authority boundary
remain unchanged.

Matcher v8 also adds a deterministic retrieval-only probe for a single closed business entity
when a governed `COUNT DISTINCT` request needs its stable identifier. For example, a request for
customer count may search the already-approved `stable customer identifier` binding. The probe is
restricted to the `IDENTIFIER` role, is not produced for multi-entity requests, explicit
identifier requests, or other aggregations, and cannot create a mapping, join, plan, or approval.
This closes the recorded `active-customers-by-country` retrieval gap without asking the model to
choose a physical field.

The regenerated provider-free report uses matcher `m27-deterministic-v8`, corpus SHA-256
`6bb72e6efd4801bd5042e15a29616d634118d62e5f8a83b2f19d4691b1502ece`, and report SHA-256
`09afcbbc586b60bcbebfcc521d5ee35b86c2ed647513b15c7a1541ebfaed8fbf`.
It passes unchanged gates: top-1 `0.903226`, top-3 and recall@20 `1.0`, MRR `0.946237`,
specificity `1.0`, and ambiguity recall `1.0`. The live-evaluation plan is now
`m27-cheapest-first-campaign-v5`; exact governed facts fingerprints are:

- `gpt-5-nano-2025-08-07`:
  `67a9b39c33396aa22c31ab05d3713e80f2c7fc6974d98808e896d7b8e830a635`;
- `gpt-5.4-nano-2026-03-17`:
  `72b8df84f62da2e90743b0c2604ee55c02ebaaacf99ca26accb96cc566e0e823`;
- `gpt-5.6-luna`:
  `998ddc0bbd03fa41250273981ef7ff7a759db550f210605f7eda2bcfb84b9e30`.

The previous signed v4 campaign cannot be resumed because matcher, output limits, plan version,
and governed fingerprints changed. A v5 campaign must start fresh with Nano under a separately
prepared exact policy. Before that policy is enabled, the synthetic tenant's remaining daily
input and output/reasoning capacity must be checked from aggregate accounting. Provider-free
verification passed 350 combined tests plus Ruff and mypy. No provider request occurred while
implementing, fingerprinting, documenting, or testing v14.

That aggregate preflight found zero active reservations and, for the current UTC day,
3,865,672 charged input tokens plus 80,483 charged output/reasoning tokens. Policy v50 allowed
4,000,000 input and 200,000 output/reasoning tokens, leaving only 134,328 input tokens. That is
below the campaign's independent 250,000-token input cap and could turn a provider-quality
evaluation into a tenant-quota failure. The next exact policy may therefore raise only this
retained synthetic tenant's daily input ceiling to 4,250,000 while leaving the 200,000 daily
output/reasoning ceiling and every campaign, request, stage, concurrency, cost, privacy, and
authority control unchanged. The resulting 384,328-token input headroom admits the complete
worst-case v5 campaign without granting production authority.

Policy v51 enabled the exact v14 Nano configuration and applied that synthetic-only 4,250,000
daily input ceiling. The local qualification cases remained deterministic: both positives ranked
first, the employee negative returned `no_match`, and the cross-connection homonym returned
`ambiguous`, all with zero provider use. The analytical core then exhausted two expansion
attempts as strict `invalid_output`. Durable accounting charged 91,654 input tokens and 8,192
output/reasoning tokens, for EUR 0.008645450. Qualification failed the unchanged quality gate;
there were no interpretation attempts, unknown or cross-tenant candidates, model cascades, or
SQL/tool/approval outputs.

The first v51 process built that failure result but could not persist it because the new v5 model
rejected the immutable v4 files while scanning the shared signed-history directory. This was an
evidence-reader compatibility defect after provider completion, not a provider or quality result.
The reader now accepts retained v4 and v5 plan values, requires every nested record to use the
campaign's exact plan version, keeps plan version inside campaign identity, and still refuses to
resume v4 under the current v5 plan. A regression writes signed v4 and v5 campaigns into the same
history and rejects mixed-version payloads.

No provider request was repeated. The v51 report was recovered from the two exact settled audit
rows plus a provider-free replay of the four local cases; the replay left the durable attempt
count unchanged at two. Because volatile timing was unavailable after process exit, recovered
latency is the durable audited value `0 ms`, not an estimate. The signed, content-addressed v5
failure evidence is
`reports/m27-query-studio-live-history/campaign-signed-e982c0e2ccaf386a37c06088b7d79235ade46d79cf2f56b5574f2d462bfeccb9.json`.
It validates on reload, has outcome `awaiting_policy_revision`, and authorizes only a separately
reviewed 5.4 Nano revision as the next campaign step. The compatibility fix passes all 62
evaluator tests, its targeted signed-history regressions, Ruff, and mypy without network access.

Policy v52 switched only the exact governed model/configuration to
`gpt-5.4-nano-2026-03-17` and resumed the signed v5 Nano evidence. The four local cases again
passed without provider use. For the analytical core, expansion succeeded once, then
interpretation exhausted both strict outputs as `invalid_output`; qualification therefore failed
before the adversarial case. The 5.4 increment used 3 attempts, 31,295 evaluator input tokens,
8,375 output/reasoning tokens, 6,556 ms, and EUR 0.018400525. Durable tenant accounting remained
more conservative: it charged the successful 1,883/183-token expansion plus the full
44,986/4,096 reservation for each invalid interpretation attempt.

The retained v5 campaign now totals 5 attempts, 122,949 evaluator input tokens,
16,567 output/reasoning tokens, 6,556 ms, and EUR 0.027045975. Its signed,
content-addressed evidence is
`reports/m27-query-studio-live-history/campaign-signed-ae34f31796901b6fa996634abc212ad98164d0c8e912e927284cd235b2cb6d73.json`;
no model is selected and its only permissible next revision is Luna. After settlement there are
zero active reservations, 200,819 tenant input tokens of daily headroom, and 102,950
output/reasoning tokens. These exceed the campaign's remaining independent caps of 127,051 and
23,433 tokens respectively, so a separately governed Luna attempt cannot be confused with daily
quota exhaustion.

Policy v53 switched to the exact governed Luna configuration and resumed the signed 5.4
evidence. Luna qualified all six smoke cases in 2 attempts, 4,068 input tokens, 383
output/reasoning tokens, 3,400 ms, and EUR 0.007002600. The full corpus preserved deterministic
top-1 `0.903226`, top-3/recall@20 `1.0`, MRR `0.946237`, specificity `1.0`, and ambiguity recall
`1.0`. All three repetitions of `secondary-holders-by-registration-date` matched their exact
typed fingerprint. The first `active-customers-by-country` repetition then exhausted both
expansion outputs as strict `invalid_output`, so fail-fast stopped with a conservative analytical
lower bound of 3/15 (`0.200000`).

The Luna full-corpus increment used 8 attempts, 38,804 evaluator input tokens,
9,378 output/reasoning tokens, 14,221 ms, and EUR 0.104579200. The complete v5 campaign used
15 attempts, 165,821 evaluator input tokens, 26,328 output/reasoning tokens, 24,177 ms, and
EUR 0.138627775, within every independent cap. There were zero unknown or cross-tenant
candidates, model cascades, or SQL/tool/approval outputs. No model was selected. The terminal
signed evidence is
`reports/m27-query-studio-live-history/campaign-signed-26ace3613d0cc7766e6eb9c3a4bd7236dd6cde55626c0202108f98d973467c09.json`.

All three candidates therefore fail the unchanged v5 quality gate. Additional sampling under this
exact contract is not authorized. The next policy revision must disable external AI while the
adapter records a content-free invalid-output reason category and the typed expansion contract is
diagnosed; raw provider output must remain absent.

Policy v54 performed that required disable operation. It retained the terminal Luna facts only as
historical evidence, set `external_ai_enabled=false`, and produced immutable policy fingerprint
`79dcd29ea50243d5bd158bf5c8dd067e06a6cf18295e6cfb46e470e04fad3dbf`.
No provider call has been authorized since that transition.

The provider-free v15 diagnosis found that the previous contract asked the model to reproduce
server-owned bookkeeping rather than make only the two semantic choices for which the model is
useful. Expansion schema v7/prompt v13 now returns only exact `slot_id`, atomic `query`, and
grounded `source_span`; interpretation returns only one opaque `candidate_id` per exact slot plus
the explicit filter value. Metric operations, filter operators, role/type constraints, grains,
selection counts, primary metric, ordering, limit, and complete-slot coverage are reconstructed
and validated deterministically. Partial selections, mixed ambiguity, out-of-slot candidates,
ungrounded signs/decimals/dates, conflicting governed labels, and semantic contradictions are
classified by the closed content-free taxonomy
`m27-output-failure-taxonomy-v1`; no provider body or prompt is persisted.

Public metadata egress no longer rests on an adapter boolean. The durable policy fingerprint is
bound to the exact pseudonymous tenant scope, active semantic-registry fingerprint, and reviewed
provider contract. `ApprovedPublicMetadataSurface` additionally binds the exact request
vocabulary before egress. A registry revision changes the governed configuration fingerprint and
therefore fails durable admission until an operator explicitly approves a newer tenant policy.
Restricted metadata still has a conservative pre-egress screen, but that screen is not represented
as the classification authority. OpenAI SDK `2.46.0` is the only reviewed structured-output
transport; runtime startup fails closed on another installed version.

For the retained synthetic large profile, the exact v15 governed configuration facts are:

- Nano: configuration
  `bf3a48ca9d68a6e4e663914e2fa72d75b8684a8d631595bc9a1060f590a55e1e`,
  managed config
  `187b4c0b37ed3ac025bbb2c721886c4bb5e053021246b7b5733cc2b26b1ac0c5`,
  provider contract
  `bac8c541fe87c8f561fe8d436a629cc59234237e40d68a2a7b46ea15f198c0ec`;
- 5.4 Nano: configuration
  `d2c08a402f27f8cea6b5a7c5842ca726fcb20e153c47e3bd8ff0cede7d585fa5`,
  provider contract
  `cb3b3acb41ac6726c5cce4909919dff4001d932e351a79ee231c9195abd800ac`;
- Luna: configuration
  `1652ee86c4c63cf5ab009b5c4805e7048c43d561c8d65176f32b4e7fd69232ac`,
  provider contract
  `f1659f29ad9552cca392462cb21c67033cddb503fe4360957af1012a185913c3`.

The active registry fingerprint is
`0ccb5a5c7bcae05a13710bff13459711200011144d14aeca6135a8c0652b1c6f`;
the pseudonymous semantic-scope fingerprint is
`cdd4b931cbc3de91d9668ad18f10c7e4c3f35870d08df5aa853f8a757334d406`.
These are non-secret evidence digests, not catalog values or credentials.

Live-evaluation history is now append-only plan `m27-cheapest-first-campaign-v6`. Readers retain
signed v4/v5 evidence, reject mixed nested plan versions, and refuse both pending and terminal v5
resume attempts under v6. The fresh v6 sequence must start with Nano; there is no runtime model
cascade. Its first-model worst reservation is 80,720 input tokens and 8,192 output/reasoning
tokens, while the independent campaign caps remain 250,000 and 40,000. The all-model conservative
Luna-priced cost ceiling is EUR 0.142859200.

Provider-free acceptance before a new policy passed 182 focused boundary/v15/security/fake tests,
66 complete signed-evaluator tests, 40 browser-runtime tests, Ruff, and strict mypy. The
deterministic matcher remains v8 with the unchanged published quality gates. At
`2026-07-26T22:34Z`, durable aggregate accounting showed zero reservations, 4,157,189 charged
input tokens, and 106,811 charged output/reasoning tokens for the UTC day. Under policy v54 limits
of 4,250,000 and 200,000, the remaining 92,811/93,189 tokens safely admit exactly the bounded
fresh Nano increment; a later model still requires a distinct reviewed policy and sufficient
daily headroom. This paragraph authorizes no request by itself.

Policy v55 enabled only the exact Nano v15 configuration above at
`2026-07-26T22:37:02Z`. Status confirmed the model, global endpoint, tenant scope, registry, SDK,
contract, and configuration fingerprint before egress. A fresh, non-resumed v6 campaign then
reached the expansion stage. Both governed attempts settled as `invalid_output`, with no trusted
provider usage available, so durable accounting correctly charged the full conservative
80,720-input and 8,192-output reservation.

The provider stage did not produce a selectable model result. While persisting the campaign, the
signed-history reader detected an implementation compatibility defect: parsing old v4/v5 reports
added the newly optional `output_failure_category=null` field before recomputing their report
digest. That normalized payload differed from the historically signed raw payload, and the
evaluator failed closed with `ValidationError`. No v6 report was signed, no model was selected,
and the event must not be used to authorize 5.4 Nano.

Policy v56 disabled external AI at `2026-07-26T22:39:24Z` without changing the Nano configuration
facts or tenant limits. Post-disable status reports zero active reservations and a disabled exact
policy. Durable UTC-day accounting is now 4,237,909 charged input tokens and 115,003 charged
output/reasoning tokens, leaving only 12,091 and 84,997 respectively. A same-day retry is therefore
forbidden.

The evidence reader now validates `report_sha256` against the unique-key-checked raw report object
before Pydantic applies schema defaults, and only then validates the typed report and HMAC. A
regression removes the v15 optional field from an otherwise current signed payload, recomputes its
legacy digest/signature, and proves it still loads without weakening tamper detection. All seven
retained signed v4/v5 histories verify under the corrected reader. A fresh v6 Nano campaign may be
considered only after the UTC budget resets, the provider-free gate passes, and a new exact policy
revision accepts the then-current SHA-256 of this governance record.

Policy v57 enabled the same exact Nano v15 configuration at `2026-07-27T10:14:15Z`, after the
new UTC day started with zero reserved or charged tokens. The campaign process failed closed
before durable admission or provider egress because the host restart had left the live DataHub
registry service unavailable. The sanitized boundary reported `RegistryControlError`; aggregate
accounting remained at zero reservations and zero charged tokens, so this is infrastructure
evidence rather than model-quality evidence.

Policy v58 disabled external AI at `2026-07-27T10:14:42Z`. The pinned DataHub v1.6.0 stack was
then restored from its retained local images, its GMS/UI/authentication/logical-model health
checks passed, and the same active-registry Query Studio runtime built successfully in key-free
fake mode. A later fresh v6 Nano attempt still requires a new exact policy revision that accepts
the then-current SHA-256 of this record.

Policy v59 enabled the exact Nano v15 configuration after that provider-free registry preflight.
The four local qualification cases passed with zero provider use. Both independently admitted
outputs for `revenue-by-order-date-and-category` failed the closed `semantic_contract` category,
so Nano did not qualify. The evaluator recorded 2 attempts, 16,328 input tokens, 8,192
output/reasoning tokens, 4,302 ms, and EUR 0.004502520. Durable accounting conservatively charged
the full 80,720-input and 8,192-output reservation because the provider outputs were invalid.

The fresh plan-v6 result is signed and retained as
`reports/m27-query-studio-live-history/campaign-signed-4a188b12ee6637e75f52924bc686e06a7891a4ac5fc5b4cc2f877dcdade991c8.json`;
its logical report SHA-256 is
`9f8707213316d2af55a632e2b2232d3386dcf016426fcfa16e737c45de5525f8`.
It selects no model and permits only a separately governed resume with
`gpt-5.4-nano-2026-03-17`. Policy v60 disabled external AI after the report was persisted.
Post-disable accounting shows zero active reservations and 80,720/8,192 charged input/output
tokens for the current UTC day.

Policy v61 switched only to the exact `gpt-5.4-nano-2026-03-17` v15 configuration and resumed
the signed v6 Nano report. 5.4 Nano qualified in 2 provider attempts, then failed the complete
corpus: the first `secondary-holders-by-registration-date` repetition produced a different typed
fingerprint and the second failed the closed `semantic_contract` category. Fail-fast therefore
stopped after 2 of 15 planned core trials with zero exact core successes.

The 5.4 complete-corpus increment used 5 attempts, 29,029 input tokens, 12,551
output/reasoning tokens, 13,366 ms, and EUR 0.023644005. The cumulative v6 campaign used
9 attempts, 47,607 input tokens, 20,998 output/reasoning tokens, 21,670 ms, and
EUR 0.028992150, within every independent cap. Its signed retained evidence is
`reports/m27-query-studio-live-history/campaign-signed-8d0af063e893aba82d9a956e768a176fa97cefda16b3f4889a94eed3cbc57e65.json`;
the logical report SHA-256 is
`c3ff32aca717c79b7318fab6550a03606508b141ea65a5ba98c2b1e4ced7af5b`.

Policy v62 disabled external AI after persistence. Durable accounting shows zero active
reservations and 205,761/20,998 charged input/output tokens for the UTC day. No model is selected;
the signed campaign permits only a separately governed resume with `gpt-5.6-luna`.

Policy v63 switched only to the exact `gpt-5.6-luna` v15 configuration and resumed the same
signed v6 campaign. Luna qualified in 2 provider attempts. Its first complete-corpus core trial
then exhausted both interpretation outputs in the closed `semantic_contract` category, so
fail-fast stopped with zero exact successes out of 15 planned core trials. The Luna increment
used 3 attempts, 20,734 input tokens, 8,274 output/reasoning tokens, 4,938 ms, and
EUR 0.077415800.

The terminal v6 campaign used 14 attempts, 70,575 input tokens, 29,519 output/reasoning tokens,
30,678 ms, and EUR 0.110495550, within every campaign bound. There were no unknown or
cross-tenant candidates, runtime model cascades, SQL/tool/approval outputs, source mutations, or
DataHub writes. Its signed retained evidence is
`reports/m27-query-studio-live-history/campaign-signed-9d22f86cd3aad86ce76da71bc2402a9166d6de6c2cd28d7a2e625269aeeaf499.json`;
the logical report SHA-256 is
`d076455acdfdf8be937a72a619f29ac600636f0630ec0ea2682b63aa510caafd`.

All three reviewed candidates therefore fail the unchanged v15 quality gate and no model is
selected. Further sampling under this exact contract is not authorized. Policy v64 disabled
external AI; post-disable accounting shows zero active reservations and 288,479/29,519 charged
input/output tokens for the UTC day. A later campaign requires a materially revised,
provider-free-tested contract and a fresh exact policy revision; it may not resume this terminal
v6 report.

The provider-free v16 diagnosis removed the remaining model-owned bookkeeping instead of
weakening any quality threshold. Expansion contract v7 returns only the exact server-issued
`slot_id` and one atomic `query`; SchemaBridge derives and validates the unique source span.
Slot-selection contract v2 returns only complete `slot_id`/`option_index=1` pairs and an explicit
ambiguity state; SchemaBridge maps each index to the strict local leader and derives any filter
literal from the request plus the governed allowed values. Candidate identifiers, logical-field
identifiers, source spans, filter values, operations, joins, ordering, grain, limit, SQL, and
approval decisions are not copied from model output. A tied top score is typed ambiguity before
interpretation. Proposal normalizer `m27-proposal-defaults-v3` then constructs the bounded typed
proposal under the same human-confirmation, M26, compiler, AST, and read-only gates.

Fresh evaluation plan `m27-cheapest-first-campaign-v7` preserves signed v4-v6 evidence and refuses
to resume the terminal v6 report. Historical plans keep corpus SHA-256
`6bb72e6efd4801bd5042e15a29616d634118d62e5f8a83b2f19d4691b1502ece`.
Plan v7 additionally binds frozen synthetic holdout bytes with SHA-256
`9b16db5a1b87e534485b245736da2ecf7a897cbd26630e4b4c2e415a9a720970`
and composite corpus SHA-256
`a03d96b6ce0ed7172ffcf6a074f0d755dc62f2709c10fdce7607e1989614b1ba`.
For each of the five critical cases, repetition 1 uses the original frozen request and
repetitions 2 and 3 use two distinct frozen paraphrases. The fake baseline proves all 15 variants
produce the same expected typed-request fingerprint for their case.

For the retained synthetic large profile, the exact v16 governed facts are:

- `gpt-5-nano-2025-08-07`: complete configuration
  `6a3ef533030fd83c06b5f51fb84142be56303d6a00c68541bbbd009221b20912`,
  managed config
  `b81881a3443211764de16e40dded3a8d1caa9277be9b088f7839603cacca549c`,
  expansion schema
  `e84548cebc5afb18d780fa7dc2d0dedf8c982d8f84c8ae9c433f09e369e6a1a0`,
  interpretation schema
  `0ea6da9176f428e994d622a5fffb6506336e0996eb4508bbef0bdb7bdbbd38c0`,
  provider contract
  `a3e46225edc215051822f4bdf6d0d4dfc35c6742a784dd77dee5d7c11bb72c71`,
  and public-metadata policy
  `3c8f4b8b648b33f7cfc85d0577dbd89d2d115274520256ffd0e0c06ad92299b8`;
- `gpt-5.4-nano-2026-03-17`: complete configuration
  `b2d550ab6ba82addfff06f78e9187d1ecabb228d287f761c0b9e4ce395214a5c`,
  managed config
  `bea5f646a73f98551c6a5ae9e0ce93a0b920432f579ce0e31141485bcda7e31e`,
  expansion schema
  `1f0a2318dc3372ee3983d0d3307b41c369aac8f1c7b8a4e6b4b900bcc7b4ede0`,
  interpretation schema
  `883bd4cd1ba0ddfb7dec49d16ea311e4562daa936d6d58ab1b7bccc7c0c471d9`,
  provider contract
  `d4d8d7c93b0a3a370c67ed1b6ac91712401038af837af00e764bbfe7d3c5fc82`,
  and public-metadata policy
  `b7c84fcb2bf716ee60e57558c5936641ec9f72aab72edceed5bcacd4cbe50306`;
- `gpt-5.6-luna`: complete configuration
  `da13e5cb83fde808253149f08241ee80d86190248a4d2ae89e1b06f3bfe24cff`,
  managed config
  `eef2975612600a021793011bcc7b12841b6f19bc1ea83349c4b81a5501e39eef`,
  expansion schema
  `3edffa1a2863912244afd80b9d70a220fb2f67ae9816911e61b9158a4cd14f38`,
  interpretation schema
  `b6f528c0d31104f54161ae7b3beab1bf0636d5c093e64a39f1347f16c9672d5d`,
  provider contract
  `ce437a5187f58300024801410d731b14b00c79757aed5630da5cce9b1837e4c8`,
  and public-metadata policy
  `dad7a9dff34c8fcc1cb86ea5b0ee0a6ced38d2e9a8ee031c465bfad922e9f334`.

All three facts bind prompt `m27-openai-prompts-v14`, schema
`m27-query-studio-v8`, matcher v8, SDK `2.46.0`, semantic-scope fingerprint
`cdd4b931cbc3de91d9668ad18f10c7e4c3f35870d08df5aa853f8a757334d406`,
active-registry fingerprint
`0ccb5a5c7bcae05a13710bff13459711200011144d14aeca6135a8c0652b1c6f`,
and fixed global-origin fingerprint
`6836a1bf89b42ed7a325b83c7825e0ae3a17048d4d7395d0606ee18f2800f71c`.
The fresh plan reserves at most 79,576 input and 8,192 output/reasoning tokens for any two-attempt
stage. Its most expensive admitted stage is EUR `0.141600800`; all-model worst-case planning uses
108 of 180 base attempts and remains independently capped at EUR 1.00.

Provider-free verification of the v16 boundary and runtime passed 217 focused tests; AI
admission, policy, quota, security, configuration, and corpus controls passed 97; Query Studio
PostgreSQL persistence/migration/scale integration passed 20; Query Studio acceptance passed 15;
the frozen holdout/equivalence suite passed 10; and the complete signed evaluator/history suite
passed 66, including v4-v6 coexistence, terminal v6 rejection under v7, and derived reservations.
The real scale proof traversed 5,434 assets and 41,028 fields and verified the reviewed search and
locator indexes within the 5-second statement timeout. The planning CLI reconstructed the exact
108/180-attempt, 79,576/8,192-token, EUR `0.141600800` bounds without a provider call. Policy v64
remains disabled with zero active reservations and current-day durable charges of 288,479 input
and 29,519 output/reasoning tokens. Only a new exact policy for the Nano facts above may start a
fresh v7 campaign; this paragraph does not itself enable external AI.

Policy v65 enabled only the exact v16 Nano facts above and started a fresh, non-resumed v7
campaign. Nano qualified on all six smoke cases; the three-model revenue case matched its exact
typed fingerprint after two provider attempts, while every local negative, ambiguity, and
pre-egress safety case passed. In the complete corpus, all three original/holdout variants of
`secondary-holders-by-registration-date` returned typed `ambiguous` after one expansion attempt
each. The first `active-customers-by-country` variant then exhausted its two independently
admitted attempts in the closed `semantic_contract` invalid-output category, so fail-fast stopped
the run.

The retained Nano increment used 7 provider attempts, 18,860 evaluator input tokens, 8,548
output/reasoning tokens, 9,780 ms, and EUR `0.004798420`. Deterministic retrieval remained at
top-1 `0.903226`, top-3/recall@20 `1.0`, MRR `0.946237`, specificity `1.0`, and ambiguity recall
`1.0`. There were zero unknown or cross-tenant candidates, runtime model cascades, or
SQL/tool/approval outputs. No model was selected. The signed v7 evidence is
`reports/m27-query-studio-live-history/campaign-signed-4b30a647d338a35b28b24a1f584c705ab57de5a5a6d5502900a3e8fee1a61f71.json`;
its logical report SHA-256 is
`f9384eb0a76e75e0606f4bf46cdace7414e55fb08999f70254247cc5d299a2b6`.

Policy v66 disabled external AI immediately after persistence. Durable accounting has zero active
reservations and current-day charges of 371,763 input and 38,067 output/reasoning tokens. The
retained campaign is `awaiting_policy_revision`; it permits only a separately governed resume
with the exact `gpt-5.4-nano-2026-03-17` v16 facts above. This is an offline cheapest-first
evaluation transition, never a runtime model cascade.

Policy v67 switched only to the exact v16 `gpt-5.4-nano-2026-03-17` facts and resumed the signed
v7 Nano evidence. 5.4 Nano qualified on all six smoke cases in 2 attempts, 1,591 input tokens,
115 output/reasoning tokens, 5,524 ms, and EUR `0.000508145`. In the full corpus, the first two
original/holdout variants of `secondary-holders-by-registration-date` produced typed fingerprint
mismatches, and the third stopped in the closed `semantic_contract` invalid-output category.
Fail-fast ended the run before later core cases.

The 5.4 full-corpus increment used 5 attempts, 3,918 input tokens, 291 output/reasoning tokens,
6,051 ms, and EUR `0.001262085`. Cumulative v7 evidence now records 14 attempts, 24,369 input
tokens, 8,954 output/reasoning tokens, 21,355 ms, and EUR `0.006568650`, within every independent
campaign bound. There were again zero unknown or cross-tenant candidates, runtime cascades, or
SQL/tool/approval outputs, and no model was selected. The signed evidence is
`reports/m27-query-studio-live-history/campaign-signed-626d75f213ffc5569feab5c05f78056b0611e3ce3dcaa5a3e0d77c39a458322b.json`;
its logical report SHA-256 is
`f10243256d1f17b4b72c90ea61cedb85c59796cdb8251e7b916878a110ab61ba`.

Policy v68 disabled external AI immediately after persistence. Durable accounting has zero active
reservations and current-day charges of 377,272 input and 38,473 output/reasoning tokens. The
retained campaign permits only a separately governed resume with the exact `gpt-5.6-luna` v16
facts above.

Policy v69 switched only to the exact v16 `gpt-5.6-luna` facts above and resumed the signed v7
5.4 Nano evidence. Luna qualified on all six smoke cases in 2 attempts, 1,591 input tokens,
115 output/reasoning tokens, 2,531 ms, and EUR `0.002509100`. Its first complete-corpus
`secondary-holders-by-registration-date` trial then exhausted both independently admitted
attempts in the closed `semantic_contract` invalid-output category. Fail-fast stopped with zero
exact core successes.

The Luna full-corpus increment used 2 attempts, 15,236 evaluator input tokens, 8,192
output/reasoning tokens, 2,586 ms, and EUR `0.070826800`. The terminal v7 campaign used 18
provider attempts, 41,196 evaluator input tokens, 17,261 output/reasoning tokens, 26,472 ms, and
EUR `0.079904550`, within every independent campaign bound. There were zero unknown or
cross-tenant candidates, runtime model cascades, SQL/tool/approval outputs, source mutations, or
DataHub writes. Its signed retained evidence is
`reports/m27-query-studio-live-history/campaign-signed-0e06e2fc840eb6d2a20ccd649654fd62557127af0fa37a3ecaa0ea5f740918e1.json`;
the logical report SHA-256 is
`c1cab65a24c9c54d7b8f044d79710bc7d986bfb6c8600f33937ee0b452857433`.

All three reviewed candidates therefore fail the unchanged v16 quality gate and no model is
selected. Further sampling under this exact contract is not authorized. Policy v70 disabled
external AI immediately after persistence. A serializable, read-only accounting audit at
`2026-07-27T11:30:18Z` found zero active attempts, zero open reservations, and zero reserved
input/output tokens. Current-day durable charges are 458,439 input and 46,780 output/reasoning
tokens. All 308 retained reservations are settled and reconcile exactly with 308 immutable audit
rows; policy revisions 1 through 70 are continuous, revisions 65 through 70 preserve the reviewed
enable/disable sequence, and no orphan, missing audit, stale lease, or disabled-policy activity was
found. A later campaign requires a materially revised, provider-free-tested contract and a fresh
exact policy revision; it may not resume this terminal v7 report.

The provider-free v17 contract addresses that terminal evidence without relaxing the quality
gate. For every required slot, SchemaBridge now derives `semantic_focus`, `source_focus`, the
operational retrieval query, and the source span on the server. The provider's atomic query is
supplemental compatibility evidence only and cannot alter retrieval, ranking, the counted entity,
the filtered relationship or attribute, or any request/configuration fingerprint.
`count_distinct` is anchored to the identifier of the entity being counted; filters are anchored
to their governed attribute or relationship. Any provider query that conflicts with those closed
anchors is rejected before interpretation.

Fresh plan `m27-cheapest-first-campaign-v8` preserves the immutable v4-v7 history, uses the same
frozen holdout and composite corpus SHA-256
`a03d96b6ce0ed7172ffcf6a074f0d755dc62f2709c10fdce7607e1989614b1ba`,
and refuses both awaiting and terminal v7 evidence as a v8 resume. Nano must start without a
resume file. A later candidate may resume only the immediately preceding content-addressed,
HMAC-authenticated v8 snapshot whose state is `awaiting_policy_revision` and whose
`next_required_model` matches that candidate.

For the retained synthetic large profile, the exact v17/v8 facts are:

- `gpt-5-nano-2025-08-07`: complete configuration
  `0efcfcca01978ccdf699d321e953d4160e22040c19502563db8ac02faba8c544`,
  managed configuration
  `7b69fa23190754fd29561a83ae648798e71835348b74645e9ee84166573e2f30`,
  expansion schema
  `03065b2cc386022b64dad2d1181c445fe5649495b72b4a4b8f35ea473929da8f`,
  interpretation schema
  `4c9cba731982a261749ceddd688f9528ddc268fbef0b598f55403f4646b39968`,
  provider contract
  `5912b4dc530ebd56bdcb6f1c45a228198833d1646f28fff31d9be576e66dfefb`,
  and public-metadata policy
  `97ef03d05c842202649b9f2c86880fd34ebad0f4281034adfed7f60aa6d4a96b`;
- `gpt-5.4-nano-2026-03-17`: complete configuration
  `22dc82658f5658b551f910e80079ed0bf1795237fb0063fbe2a27825a521c4c8`,
  managed configuration
  `1439650fc12e6d8686953024e08cc80a90fe2001b2110f4ab1da50b28701aa33`,
  expansion schema
  `73357f2a899b4c60df8fad717803f17ff7d95ab09fa22910a6c7d8a8de94f27d`,
  interpretation schema
  `abbcf0000435c00384f7de59d3842fe2662544c6a022bedf2a6cdfaf0689acaf`,
  provider contract
  `3e92d8f81b8ad023d7e3d0867a50d81d8936feb7cca8c1971e59609df65d29c5`,
  and public-metadata policy
  `3d0c7e87ac0ca4376697ae8dca66b5f04222381941fa0b7c8c12a9adf2a95261`;
- `gpt-5.6-luna`: complete configuration
  `d4150874fea65341b9ae5183a78ea0e551fca04cb81665aa6d0615b29b9c5101`,
  managed configuration
  `2f386c5edebf4605a7a339ed686880dac6d4479c4f291c4dc166713ce27edd4d`,
  expansion schema
  `1d4cdbdbccee8c6d8bbca133f2caf31568b76d2cff952ff635db72ca1c4d1a3f`,
  interpretation schema
  `83895a557d96b1c51edb856601bc0cb430f58e31346b3d430ca1ce7a0be2db02`,
  provider contract
  `50850a08c51a240365e7e83ef3f6a94cc1ded2d4cc19e1d8d9fbecc6c8276fd5`,
  and public-metadata policy
  `2e38ec76a6d8cef7fd09b56c134c3081f88c8c240769979a1523a280c99969fc`.

All three facts bind prompt `m27-openai-prompts-v15`, provider schema
`m27-query-studio-v9`, expansion contract `m27-expansion-contract-v8`, semantic-focus contract
`m27-semantic-focus-v1`, slot-selection contract `m27-slot-selection-v3`, matcher v8, exact
OpenAI SDK `2.46.0`, semantic-scope fingerprint
`cdd4b931cbc3de91d9668ad18f10c7e4c3f35870d08df5aa853f8a757334d406`,
active-registry fingerprint
`0ccb5a5c7bcae05a13710bff13459711200011144d14aeca6135a8c0652b1c6f`,
and fixed global-origin fingerprint
`6836a1bf89b42ed7a325b83c7825e0ae3a17048d4d7395d0606ee18f2800f71c`.
The provider-free planner reconstructs 108/180 worst-case base attempts, a maximum two-attempt
stage of 80,954 input and 8,192 output/reasoning tokens, and maximum admitted-stage cost of EUR
`0.143116600`, inside the unchanged aggregate caps.

Policy v70 remains disabled with no selected model and zero active reservations. This governance
update does not enable external AI. Only a new exact policy revision for the Nano v17 facts above,
bound to the SHA-256 of this reviewed file, may start a fresh v8 campaign. Any provider attempt
must be followed immediately by a disabled policy revision, including a fail-closed evaluator
exit.

Policies v71, v73, and v75 then enabled, in cheapest-first order, only the exact v17 Nano,
5.4 Nano, and Luna facts above. Policies v72, v74, and v76 disabled external AI immediately after
each retained result. Nano started a fresh v8 campaign; 5.4 Nano and Luna resumed only the
immediately preceding content-addressed v8 snapshot in `awaiting_policy_revision`. No v7 report
was resumed or rewritten.

All three candidates passed the same provider-free negative, ambiguity, sensitive-input,
retrieval, and safety gates but failed the unchanged exact core gate. Nano returned three
fingerprint mismatches for `secondary-holders-by-registration-date`, followed by one fingerprint
mismatch and one closed grounding failure for `active-customers-by-country`. 5.4 Nano returned
three fingerprint mismatches for the secondary-holder case and then one closed
`semantic_contract` failure for the active-customer case. Luna stopped at its first closed
`semantic_contract` failure for the secondary-holder case. Fail-fast prevented unnecessary later
sampling, and no candidate was selected.

The terminal signed v8 evidence is
`reports/m27-query-studio-live-history/campaign-signed-73e2d36e8c1ddb29158379196c208a4fffee5d9e1607b6c8dce65786d7c5f218.json`;
its whole-file SHA-256 equals the content-addressed filename and its logical report SHA-256 is
`b06bdb15a28f76a6014cecfa40899affe9a2f35f50bfc60c40f101bc645db212`.
The immutable campaign used 25 provider attempts, 52,895 evaluator input tokens, 17,788
output/reasoning tokens, 69,387 ms, and EUR `0.093620670`, within every independent cap.
Deterministic retrieval remained at top-1 `0.903226`, top-3/recall@20 `1.0`, MRR `0.946237`,
specificity `1.0`, and ambiguity recall `1.0`. There were zero unknown or cross-tenant
candidates, runtime model cascades, SQL/tool/approval outputs, source mutations, or DataHub
writes.

A serializable read-only accounting audit after policy v76 found revisions 1 through 76
continuous, zero active attempts, zero open or stale reservations, and zero reserved tokens.
Current-day durable charges were 639,436 input and 64,568 output/reasoning tokens. All 333
retained reservations were settled and reconciled exactly with 333 immutable audit rows; no
disabled-policy attempt, missing audit, expired lease, or orphan was found. Across the complete
retained history, settled charges and audit rows reconcile at 4,877,345 input and 179,571
output/reasoning tokens.

The provider-free root-cause analysis found a deterministic contract defect rather than a model
quality or budget defect. The server derived the correct semantic owner for each required slot,
but `DescriptionSearchProbe` did not preserve that owner into governed retrieval. Consequently,
the strict top-1 contract exposed `AccountHolder.customer_key` for a customer-identifier phrase
even when the counted entity was `Customer`; option `1` then made the correct
`Customer.customer_key` impossible for every model. The same missing owner admitted an
unrequested AccountHolder join for the active-customer case. V8 is therefore terminal and cannot
authorize further sampling. Any later campaign requires a materially revised, provider-free
15/15 contract, a new campaign version, new exact configuration fingerprints, and a fresh
governance-bound policy revision.

## Provider-free v18 correction and fresh v9 authorization

The v18 contract fixes the deterministic owner-loss defect before any provider call. Every
`DescriptionSearchProbe` now preserves immutable semantic and owner focus. Owner, role, and
canonical type form an exact allowlist while the registry-backed governed population is scanned,
before scoring, sorting, or truncating the bounded page. The regression suite proves that the
correct owned field remains selectable even when it is position 22 in the unrestricted ranking.
An empty owner population is typed `no_match`; tied compatible leaders remain typed `ambiguous`.
No similarity score, connectivity preference, or later interpretation can weaken these outcomes.

Analytical expansion is now entirely local and returns `usage=None`: there is no expansion-stage
admission, reservation, provider audit, or egress. The public live composition exports and builds
only the admitted OpenAI interpretation adapter. Server-owned probes preserve the governed owner,
role, type, relationship meaning, explicit allowed filter values, and purpose-specific source
span. Filter literals may affect only a FILTER slot and cannot distort identifier, grouping, or
metric retrieval. Each required purpose has its own immutable score and exactly one compatible
option can reach interpretation. OpenAI may return only typed option selections and ambiguity
evidence; deterministic reconstruction, domain validation, canonicalization, human confirmation,
SQL compilation, AST validation, and read-only execution remain independent downstream controls.

The independent provider-free v18 oracle passes all 15 required analytical texts: each of the
five frozen core requests plus two holdout paraphrases. The expected request fingerprint,
confirmed plan fingerprint, dependency fingerprint, and guarded SQL are derived through the same
guided path used as the independent oracle. Adversarial owner, value, ambiguity, missing-owner,
partial-slot, cross-slot, and retry cases fail closed. These checks also prove zero provider
calls, zero reservations, and zero expansion usage before interpretation.

Fresh plan `m27-cheapest-first-campaign-v9` preserves the immutable v4-v8 history and refuses
every v8 terminal report as a resume. Its provider-free preflight must pass exactly 15/15 before
runtime admission or instrumentation can be constructed. The safe preflight diagnostic contains
only counts, booleans, enums, and one-way fingerprints; it excludes request text, logical and
physical identifiers, values, definitions, SQL, and credentials. Nano must start fresh. A later
candidate may resume only the immediately preceding content-addressed, HMAC-authenticated v9
snapshot in `awaiting_policy_revision` whose `next_required_model` exactly matches that candidate.

For the retained synthetic large profile, the exact v18/v9 facts are:

- `gpt-5-nano-2025-08-07`: complete configuration
  `7225860e1e8c35129d74cc3fdf120a671da4af7a1fb103c447f1cc440ef26b96`,
  managed interpretation configuration
  `c2d6e2754fe9b8df183ee89ac7e2d9bb3d51a4ec4d31cb977264782564eb1634`,
  historical full configuration
  `ed9496399eebc5cd3b5fed4fd271496c45c0a8cd0c243756b795c5317e3aa07d`,
  interpretation response
  `29770569cfd673144c0bdd1f6edf7027f6bd4b542b4f17997bce07bb38fb0f4d`,
  provider contract
  `9f3e9b9b332d5480d1def88b4ed6839111f0afaddd112f0427553cb269aca206`,
  and public-metadata policy
  `040fad2c406ace693affac1a3baf5d6c91c9cd34a0c192ee3a02fa47d832b3f4`;
- `gpt-5.4-nano-2026-03-17`: complete configuration
  `2e406c0e4e1c92ec90367a35800809553955522c3e2d145edb7357865f0e01cc`,
  managed interpretation configuration
  `8abdabfc8d63d5cc156a15fe690cb7bc4c9123ebae450fd6921988e4e84c6a66`,
  historical full configuration
  `7020046a0d22ca877d3512bc164e71bcc950f17f8585ac0999d6da381c599d37`,
  interpretation response
  `a6076eaf0137c862b8cfb89d85e14ab9470ee7bde4749464b5d737a1c6a48373`,
  provider contract
  `5b5f9f11da1ee3c6e1ed3324d4d8b40576e1e6958e553127533fedd082388470`,
  and public-metadata policy
  `0317a8de6720815e5051afbcbf845e52b2062b458f2db052daf1f7404fc3d8b0`;
- `gpt-5.6-luna`: complete configuration
  `8369eb9c9dff31ed259372c605a73ebb3946719343f8a3c9537d04bed41fccb7`,
  managed interpretation configuration
  `3b344756286e3d7ff4662f946e82b9951acfc0bb1cd14e34123384931a9427df`,
  historical full configuration
  `fe71d8ad1d1027c502db23251c083546cd4bda5fd891ab9d4c1d0920152a17b8`,
  interpretation response
  `90b148ef7779c5f7f38e5dff730d07dc2a905c0a76a7e1b08cf740c65d440ff5`,
  provider contract
  `1c6635f89542f1548dd40edfce35e7289064cf3d796c52dec56981e1c3cac9cc`,
  and public-metadata policy
  `fde90c0371477f66aaa1776077294473d52287731cd2de16b02342e1dc3078ad`.

All of those historical v18/v10 facts bind prompt `m27-openai-prompts-v16`, provider schema
`m27-query-studio-v10`, local expansion contract `m27-expansion-contract-v9`,
semantic-focus contract `m27-semantic-focus-v2`, slot-selection contract
`m27-slot-selection-v4`, matcher `m27-deterministic-v9`, orchestration policy
`m27-local-analytical-preflight-v3`, exact OpenAI SDK `2.46.0`, semantic-scope fingerprint
`cdd4b931cbc3de91d9668ad18f10c7e4c3f35870d08df5aa853f8a757334d406`,
active-registry fingerprint
`0ccb5a5c7bcae05a13710bff13459711200011144d14aeca6135a8c0652b1c6f`,
and fixed global-origin fingerprint
`6836a1bf89b42ed7a325b83c7825e0ae3a17048d4d7395d0606ee18f2800f71c`.

The provider-free v9 planner reconstructs one qualification base attempt and 17 complete-corpus
base attempts per model. Across all three reviewed candidates the worst case is 54 attempts,
leaving 126 under the unchanged 180-attempt campaign cap. A two-attempt interpretation stage
reserves at most 79,844 input and 8,192 output/reasoning tokens; the most expensive reviewed
stage costs at most EUR `0.141895600`, inside the unchanged aggregate token and EUR caps.

Policy v76 remains disabled, with no selected model and no active reservation. This section does
not enable external AI. Only a fresh policy revision for the exact Nano v18 facts above, bound to
the final SHA-256 of this reviewed file, may start a fresh v9 campaign. Every provider attempt
must be followed immediately by a disabled policy revision, including a fail-closed evaluator
exit. No v8 report may be resumed.

Policy v77 then enabled only the exact Nano v18 facts and started a fresh v9 report. The mandatory
provider-free preflight passed 15/15 with zero provider calls, but the evaluator returned the
closed `policy_unavailable` state before runtime instrumentation. The authorization loader still
inspected the historical admitted expansion wrapper even though the production v18 composition
admits only interpretation. Policy v78 disabled external AI immediately. The retained signed
zero-attempt evidence is
`reports/m27-query-studio-live-history/campaign-signed-fc7235b4d29a0532f76f41a43d5733393ed9053239360f236abcd229b5ebf5a7.json`;
its logical report SHA-256 is
`fc28574e4eea099d0d6be2326e36518ba7a77516a89d081123575a995d04648a`.
It records zero provider attempts, zero provider tokens, zero cost, no selected model, and no
resumable next model.

The evaluator authorization loader now requires the aligned v18 runtime, reads the PostgreSQL
control only from the exact `AdmittedQueryStudioIntent`, and rejects the legacy
`AdmittedDescriptionExpansion` path before a policy read. Focused authorization, external
preflight, and live-runner regressions pass 12/12 without a provider or service call. A new exact
Nano policy revision, bound to the SHA-256 of this amended record, may start a fresh v9 campaign;
the zero-attempt policy-unavailable report must not be resumed.

Policy v79 enabled only the same exact Nano facts and started a second fresh v9 run after that
authorization fix. Its provider-free preflight passed and durable accounting settled 16
interpretation attempts as `succeeded`: 15,715 input tokens, 1,204 output/reasoning tokens,
29,611 ms, and EUR `0.001267350`. There were no expansion attempts, open reservations, or active
attempts. The in-memory quality evaluation completed, but the signed evidence writer then rejected
the report because the earlier terminal zero-attempt v9 evidence had the same campaign identity.
Policy v80 disabled external AI immediately. No signed quality report was created, so these usage
facts must not be interpreted as a model PASS, a selected model, or resumable campaign evidence.

That workflow defect is corrected by fresh plan `m27-cheapest-first-campaign-v10`. Plan schema 3,
campaign schema 5, provider-free preflight schema 1, the frozen corpus, semantic contract, model
configuration, quality thresholds, and budget remain unchanged. V9 is now historical and cannot
resume as v10. Before runtime composition, instrumentation, or provider admission, a fresh v10
invocation verifies the content address and HMAC of every retained campaign and fails closed if
any signed v10 evidence already exists. Historical v9 evidence does not block the first v10 run;
a second fresh v10 run is rejected before provider egress and may proceed only through the exact
signed resume contract when the retained v10 state allows it. Focused v10 history, CLI ordering,
writer, holdout, and schema gates pass 24/24 without a provider or service call.

Policy v80 remained disabled until the separately reviewed v81 transition below. Neither v9
report was resumed.

## Policies v81/v82, signed v10 result, and fresh v11 authorization

The immutable tenant-policy and provider-attempt audit chain records policy v81 as the only
enable transition for the v10 run. It authorized `gpt-5-nano-2025-08-07` under the exact
historical v18 Nano configuration
`7225860e1e8c35129d74cc3fdf120a671da4af7a1fb103c447f1cc440ef26b96`
and the existing global synthetic limits; it did not authorize 5.4 Nano, Luna, production data,
or runtime fallback. The mandatory provider-free preflight passed all 15 original/holdout core
cases with zero provider calls. Its retained v10 pipeline fingerprint is
`03df287f3be77c2ab7debac723f95f0c31469dc0261cef28fe4aa6ecbc99db41`.
Nano then qualified on all six smoke cases, including an exact revenue-core fingerprint, while
the local positive, negative, homonym, and sensitive-input controls passed without provider use.

The complete-corpus retrieval and analytical gates also passed before the adversarial failure:
top-1 was `0.9032258064516129`, top-3 and recall@20 were `1.0`, MRR was
`0.9462365591397849`, specificity and ambiguity recall were `1.0`, and all 15 core
original/holdout trials matched their exact typed-request fingerprints. The fifth observed
adversarial case, `invented-field`, was expected to return governed `no_match` but instead failed
locally as `query_studio_provider_invalid_output` in the closed `semantic_contract` category.
That failing case made zero provider attempts and fail-fast left the remaining five adversarial
cases unattempted. The result is therefore evidence of a deterministic authority/orchestration
defect, not evidence that Nano failed the retrieval or typed core task.

The signed, content-addressed v10 evidence is
`reports/m27-query-studio-live-history/campaign-signed-e02360a3b74f55d4e027273bfba1ff3bfffeb649645d37f7ce45fa04f112faf1.json`;
its whole-file SHA-256 equals the filename and its logical report SHA-256 is
`9c4da4414d4b2444ec7f03b1f9bec4ae4eb6319cec469d04b5b71f9de936a062`.
The cumulative exact usage is 16 interpretation attempts, 15,715 input tokens, 1,168
output/reasoning tokens, 25,968 ms, and EUR `0.001378245`. It remained inside every independent
budget, selected no model, and was signed as `awaiting_policy_revision` for 5.4 Nano under the
then-current v10 workflow. Policy v82 disabled external AI immediately after that immutable
result was persisted, without authorizing a further candidate. Policy v82 remains disabled;
this record performs no enable operation and authorizes no provider request.

The provider-free root-cause analysis found two related qualified-reference bypasses. First, an
unknown but syntactically valid two-segment `LogicalModel.field_name` assertion could be rewritten
into other required probes and reach normal closure without proving that every explicit original
reference existed in the active governed registry. Second, a dotted path with three or more
segments, such as `db.Customer.customer_key`, could expose an apparently valid two-segment suffix
instead of being treated as one malformed physical-looking path. Either behavior could turn an
invented exact identity into fuzzy evidence.

The corrected boundary scans the normalized original request before candidate scoring. Every
two-segment qualified reference is checked as an exact, bounded, source-ordered logical identity;
an absent reference produces an empty executable shortlist and deterministic `no_match` before
interpretation. Dot-aware lexical boundaries prevent a two-segment parser match inside a longer
path, while the separate malformed-path parser captures the complete 3+-segment value. Such a
path is guarded with an empty owner allowlist and also fails before intent or provider admission.
Known two-segment references retain their exact lexical probe and continue through the ordinary
governed closure. Regressions cover unknown references in metric, dimension, and filter slots,
multiple owners, every rewritten slot, valid references, bounded identifiers, 3+-segment paths,
decoy scoring, and active-text payloads.

The resulting current deterministic/provider contract versions are:

- local expansion `m27-expansion-contract-v10`;
- semantic focus `m27-semantic-focus-v3`;
- slot selection `m27-slot-selection-v4`;
- matcher `m27-deterministic-v9`;
- orchestration `m27-local-analytical-preflight-v5`;
- prompt `m27-openai-prompts-v16`;
- provider schema `m27-query-studio-v10`;
- proposal normalizer `m27-proposal-defaults-v3`;
- durable attempt policy `m27-durable-attempts-v3`;
- exact OpenAI SDK `2.46.0`.

For the retained synthetic large profile, the current key-free derivation produces these exact
reviewed facts:

- `gpt-5-nano-2025-08-07`: complete configuration
  `f5fb0b20d85edca1a9780d76161342760f7d25ee4dc829fb686ec737f05cb0e7`,
  managed interpretation configuration
  `5f1231bc9acd2dec58bd7b780785a26b80c0c935f512175bca6b5eaa5d4d52d1`,
  provider contract
  `3231b06275a53db054b5e7417ccbcdc5b2a68dd67821cf60200e4590d43b380d`,
  and public-metadata policy
  `93d7dcd9f911da5bb81d057ebeed684a1e1f738af923ede0a3fcd9665e8b1799`;
- `gpt-5.4-nano-2026-03-17`: complete configuration
  `f9859d51e657771c65ade83e0ffc54b0c436daf50d09ac6c369e1113fe7e2fcd`,
  managed interpretation configuration
  `e5db5f3e65456cacf371b3c7b740ab2634adf36855c6483f8b7f08f912157e9c`,
  provider contract
  `197b053ab6e3892ccae81e8ee49bc5242fa9eae850b7a84de6096ac76193a9d3`,
  and public-metadata policy
  `464ff706214b9234f6fd576df6513032322b501aa150a51397bf08682eb28525`;
- `gpt-5.6-luna`: complete configuration
  `f49069af78280d641d46edf664f67eebb3bf4b503489ad02a10f2196c55247fa`,
  managed interpretation configuration
  `f498753ddd60afdbd67363ea2e2ad5a3538032d29c473e36478a48128d9f6d3c`,
  provider contract
  `414505f62efe890b048024348f12888c0250c207bf5074c3f4683545d0757f1d`,
  and public-metadata policy
  `4c1591187dbc52d0f3f5cda34bd78dbfa0201130fc6cbfd7ed51fdb1d4461013`.

All three facts bind the exact semantic-scope fingerprint
`cdd4b931cbc3de91d9668ad18f10c7e4c3f35870d08df5aa853f8a757334d406`,
active-registry fingerprint
`0ccb5a5c7bcae05a13710bff13459711200011144d14aeca6135a8c0652b1c6f`,
and fixed global-origin fingerprint
`6836a1bf89b42ed7a325b83c7825e0ae3a17048d4d7395d0606ee18f2800f71c`.
These are non-secret governance digests and grant no catalog, database, or provider authority.

Fresh plan `m27-cheapest-first-campaign-v11` retains v10 as immutable historical evidence and
refuses it as a v11 resume. Plan schema 3, campaign schema 5, provider-free preflight schema 1,
the frozen composite corpus, 15/15 gate, quality thresholds, and exact 54/180 attempt,
79,844-input, 8,192-output/reasoning, and EUR `0.141895600` planning bounds remain unchanged.
The preflight identity is now kind
`m27-provider-free-server-owned-core-preflight-v2` with exact fingerprint
`42e017992067ab4f666017dbc1e65f4e695ed8f044052cbfa0d2b6a63cf81ffb`.
It binds the corpus, prompt, schema, matcher, orchestration, expansion, semantic-focus,
slot-selection, normalizer, semantic scope, and active registry, but deliberately excludes
model-specific complete configuration facts. The same fingerprint is independently reproduced
for Nano, 5.4 Nano, and Luna; each qualification report still binds its own exact model
configuration and policy revision.

Before runtime composition, instrumentation, or provider admission, a fresh v11 invocation
verifies the HMAC and content address of every retained campaign and fails closed if any signed
v11 evidence already exists. Historical v10 evidence does not block the first v11 run; a second
fresh v11 run is rejected before egress. The v10 report is not resumable because the deterministic
contract and preflight identity changed.

Policy v82 remains disabled. Only one new exact policy revision for the current Nano configuration
`f5fb0b20d85edca1a9780d76161342760f7d25ee4dc829fb686ec737f05cb0e7`,
bound to the final SHA-256 of this reviewed file, may initiate the first fresh v11 run. This
paragraph does not enable it. Every live run must be followed immediately by a disable revision,
including qualification failure, provider failure, preflight failure after policy inspection,
evidence-write failure, or success. A later 5.4 Nano or Luna run would require both an exact
signed v11 `awaiting_policy_revision` predecessor and its own separately reviewed, governance-bound
policy; neither later model is pre-authorized here.

## Official references reviewed

- OpenAI data controls and data residency:
  https://platform.openai.com/docs/models/default-usage-policies-by-endpoint
- OpenAI developer quickstart and default API-key endpoint:
  https://platform.openai.com/docs/quickstart/make-your-first-api-request
- Responses `max_output_tokens`, including visible output and reasoning:
  https://platform.openai.com/docs/api-reference/responses-streaming/response/incomplete
- GPT-5 nano model and Structured Outputs:
  https://developers.openai.com/api/docs/models/gpt-5-nano
- GPT-5.4 nano model and Structured Outputs:
  https://developers.openai.com/api/docs/models/gpt-5.4-nano
- Reviewed model comparison used for the Luna snapshot and conservative pricing:
  https://developers.openai.com/api/docs/models/compare

## Acceptance boundary

The user's instruction to reuse the existing OpenAI API key authorizes this synthetic evaluation.
It does not replace a tenant DPA, privacy review, regional-residency decision, production
credential rotation, or legal approval. Production remains fail-closed until those controls are
completed for the actual tenant and data classification.

## Signed v11 selection and policy v84 closure

This closure is appended after, and does not rewrite, the exact policy-v83 governance record. The
first 109,616 bytes of this file reproduce that complete prior record with SHA-256
`4f1e6df707017309916e17e9d2360d58de2fe4c276bf72994fd977ec5bd2b85a`.
Policy v83 was the one exact enable revision bound to those bytes, the Nano complete configuration
`f5fb0b20d85edca1a9780d76161342760f7d25ee4dc829fb686ec737f05cb0e7`,
the fixed global origin, and the existing synthetic-only limits. It authorized no other model,
runtime cascade, production data, source write, DataHub mutation, governed SQL execution, tool, or
approval action.

The resulting fresh `m27-cheapest-first-campaign-v11` evidence selected
`gpt-5-nano-2025-08-07` with outcome `selected`, no next required model, and no fallback. The
immutable envelope is
`reports/m27-query-studio-live-history/campaign-signed-beef3391aef2ea2cedfdfbc6e065ce259f7143861ccc0470197c6edcb88ba30a.json`.
Its complete file SHA-256 equals the content-addressed filename, its logical report SHA-256 is
`e13a63fd2add281e567e3b4e4086ba3bb8510ec6e554608533a4c532215c0b7f`, and the hardened runtime
loader verified its canonical history location, duplicate-free typed JSON, raw-payload report
digest, HMAC key version/signature, history transition, and frozen corpus matrices without
exposing the owner-only key. The exact provider-free pipeline fingerprint is
`42e017992067ab4f666017dbc1e65f4e695ed8f044052cbfa0d2b6a63cf81ffb`.

The provider-free preflight passed all 15 core original/holdout cases with zero provider calls.
Qualification passed all six smoke cases; five remained local and the exact revenue-core case
used one interpretation attempt. The complete evaluation then observed all 136 planned cases
with none unattempted: 62/62 positive retrieval cases, 31/31 negative cases, 18/18 ambiguity
trials, 15/15 exact typed core trials, and 10/10 adversarial cases. The two invented-reference
cases, `invented-field` and `invented-join`, returned deterministic `no_match`; the other eight
adversarial cases were blocked as `sensitive_input_blocked`. All ten made zero provider attempts.

The complete-corpus metrics were top-1 `56/62` (`0.9032258064516129`), top-3 `62/62` (`1.0`),
recall@20 `62/62` (`1.0`), MRR `0.9462365591397849`, no-match specificity `1.0`, sensitive-input
block rate `1.0`, ambiguity recall `1.0`, and exact core success `15/15` (`1.0`). There were no
false negatives at 20, false positives, ambiguity misses, core mismatches, adversarial misses,
unknown candidate IDs, cross-tenant candidates, runtime model cascades, or SQL/tool/approval
outputs.

Signed campaign usage was exactly 16 interpretation attempts, 15,715 input tokens, 1,204
output/reasoning tokens, 30,016 ms of evaluator-stage time, and EUR `0.001394085`; it remained
inside every independent campaign bound. The PostgreSQL control record independently contains
exactly 16 distinct policy-v83 reservations and 16 matching append-only usage-audit rows. All are
`interpretation`, `settled`, and `succeeded`, with zero `expansion` rows, and total 15,715
observed/charged input tokens, 1,204 observed/charged output tokens, and 29,961 ms of
provider-adapter time. Every reservation-to-audit field comparison passed. Admission active
count, open reservations, stale reservations, reserved daily input/output tokens, and later-policy
provider attempts are all zero.

The two duration values have different code boundaries: evaluator-stage time includes local
admission and settlement around the provider adapter, while the durable attempt records the
adapter duration. Their 55 ms delta is therefore not another request or token charge. Both
surfaces currently name the field `duration_ms` without retaining that boundary label, which is a
P2 evidence-clarity limitation to correct in the next evidence schema.

The signed report and durable control rows reconcile exactly in aggregate facts and chronology,
but they do not yet have a cryptographic one-to-one cross-reference. The signed JSON deliberately
omits reservation ID, audit ID, and request fingerprint; the control rows do not retain campaign
file/report SHA or case ID. Consequently an auditor can prove 16 signed attempts and 16 matching
durable attempts under the sole policy-v83 window, but cannot recompute a per-attempt binding
between those two evidence surfaces. This is a P1 final-evidence gap. It does not reverse Nano's
observed quality PASS, but it blocks a claim of cryptographically exact campaign-to-ledger
reconciliation and blocks further live use.

The minimum remediation is provider-free: create one separately versioned, content-addressed,
HMAC-authenticated campaign-audit attestation. It must bind the exact campaign file and logical
report SHA values above, policy v83, the Nano configuration, a canonical ordered digest of every
reservation and matching audit row, exact aggregate usage, zero missing/orphan/extra/expansion/
active/reserved/stale rows, and the final disabled policy revision. The attestation exposes only
non-secret digests and aggregate facts; raw IDs and fingerprints used to derive its witness remain
inside the read-only operator process. It requires no model/provider request and no source or
DataHub access.

Policy v84 disabled external AI 6.753566 seconds after the final settlement and remains the
current disabled revision with the same exact Nano configuration and governance binding. Nano's
complete PASS ends the cheapest-first campaign, so 5.4 Nano and Luna were correctly not tested;
the order stops at the first model that passes every complete gate. This record contains only
synthetic evaluation facts and non-secret digests. No prompt, provider payload, credential,
source value, row, SQL, parameter, private identity, or secret is retained in the signed evidence
or sanitized provider-attempt audit, and this documentation update performs no provider call,
policy mutation, source or DataHub write, governed query execution, tool call, or other external
write.

Policy v84 remains disabled, and no browser smoke is currently authorized. Only after the
provider-free attestation above is implemented, independently verified, and recorded in another
reviewed governance closure may one new exact Nano policy revision bound to that closure's final
SHA authorize at most one browser smoke for one synthetic request under the same provider
contract, privacy controls, and limits. Such a later authorization would permit no new
qualification or campaign run, no second browser request, no production data, no 5.4 Nano or Luna
use, and no runtime fallback, and it would require immediate disablement even when preparation,
admission, provider interpretation, browser validation, or evidence capture failed.

## V2 ordered-execution attestation and single browser-smoke authorization

This closure appends to, and does not rewrite, the complete 116,410-byte record above, whose
SHA-256 is `026ec56314487dbc519ba8baae48180cc3ff248471ae5ce4530db71134d11c1e`.
The first provider-free v1 artifact
`reports/m27-query-studio-live-history/campaign-ledger-attestation-ea3254fbafb44b773186757463740efcd81a55205bf73d389273b164acb31618.json`
is retained only as superseded, non-accepted history: it reconciled aggregate usage but did not
prove the complete ordered execution witness required by this closure.

The accepted provider-free schema-v2 artifact is
`reports/m27-query-studio-live-history/campaign-ledger-attestation-f4fceb557b8dd0a2ddf3c05a5f670ba2ea759f32273ef515e0daf0231a117053.json`.
Its complete file SHA-256 equals the content-addressed filename; its logical attestation SHA-256 is
`badceb063169318ab19c1755c3d804aa04f151b2e6fe003f90160d7d1983676f`.
It authenticates the original campaign file/report SHA values, canonical signed execution witness
`30128ad06cef91b12e1912eb9bbb25a3588447b5ee3cdaec4cc82dec882102fc`,
complete ledger digest
`4fe55a8906b04a202a057bc4f6621b83a0b957191a2693c01dbbc9c5ec336e99`,
and unique ordinal-correlation digest
`740ac92954ae9831524464fcc532e5ced6039a7523f8ef3a7d519815b4c43fdd`.
The HMAC is domain-separated from campaign evidence; verification requires the owner-only audit
key and recomputes the artifact from one bounded PostgreSQL `REPEATABLE READ, READ ONLY` snapshot.

The witness proves all 16 signed campaign attempts and all 16 durable policy-v83
reservation/audit pairs form one uniquely compatible perfect matching, and that the sole matching
is their strict chronological ordinal pairing. Every interval is non-overlapping; every request,
idempotency, reservation, audit, settlement, scope, retention, token, model, configuration, and
terminal-state derivation is exact. Policy v83 precedes the first attempt, policy v84 follows the
last settlement by exactly 6.753566 seconds, and there are zero missing, orphan, cross-scope,
extra, expansion, reserved, stale, or active attempts. The public artifact retains only digests
and aggregates, not case text, reservation/audit/request identifiers, fingerprints, prompts,
responses, SQL, rows, values, credentials, or private identities.

The two time boundaries are now labelled accurately. The signed campaign records 30,016 ms around
the complete OpenAI Query Studio intent adapter. The durable ledger records 29,961 ms inside the
managed Responses boundary. Their per-attempt deltas are non-negative and total 55 ms; the delta
is adapter construction, validation, and result mapping outside the provider boundary, not another
provider request or token charge.

The correlation is an authenticated, unique ordinal witness for this reviewed sequential harness.
It is not represented as a native content-derived case-to-request identity because the historical
campaign did not retain the nonce-bound request fingerprint. A future campaign evidence schema
should write the same sanitized run/case/repetition/ordinal digest into both surfaces. This stated
limitation does not alter the observed quality result, the exact one-to-one accounting, or the
bounded authorization below.

Independent final review found P0=0, P1=0, and P2=0 for one synthetic browser smoke. The final v2
implementation and artifact passed HMAC/content-address verification, duplicate-key and v1
rejection, bounded no-follow file reads and symlink-ancestor rejection, exact policy/ledger
chronology, unique matching, historical verification after a later disabled revision, 84 focused
attestation/runtime tests, 75 shared live-evaluation tests, Ruff, mypy, forbidden-field scans, and
real provider-free create/reload/recompute verification. No provider request or policy mutation
was used to create or verify the v2 artifact.

Policy v84 remains disabled at the time of this authorization. Exactly one new policy v85 may
enable only `gpt-5-nano-2025-08-07`, global managed origin
`6836a1bf89b42ed7a325b83c7825e0ae3a17048d4d7395d0606ee18f2800f71c`,
complete configuration
`f5fb0b20d85edca1a9780d76161342760f7d25ee4dc829fb686ec737f05cb0e7`,
and the final SHA-256 of this complete governance file. It must preserve the reviewed limits:
1,000 requests/minute, 4,250,000 daily input tokens, 200,000 daily output/reasoning tokens, one
concurrent attempt, a 60-second lease, and 2,592,000-second audit retention.

That revision authorizes exactly one browser submission of the synthetic request “Quiero contar
los clientes por fecha de registro que sean segundo titular de una cuenta” through the live
Query Studio UI. Local deterministic expansion remains local; only the bounded interpretation
surface may leave the process. The authorization includes the existing at-most-one transient retry
inside that single submission, but no second browser submission, campaign/evaluation run, model
cascade, 5.4 Nano or Luna request, production data, confirmation, SQL compilation, source preview,
source/DataHub write, approval action, or other external operation. The existing API key may be
read only from the Streamlit process environment and must never appear in argv, UI, logs, state,
evidence, screenshots, or documentation.

An exact policy-v86 disable proposal must be prepared immediately after v85 is applied. Policy v86
must be applied immediately after the first visible success or failure, and also after server,
browser, admission, provider, validation, or evidence failure. The smoke is incomplete until v86
is confirmed disabled, no active/stale reservation remains, the one browser result and sanitized
ledger accounting are recorded, and the v2 historical verifier still passes.

## Browser-smoke attempt closure: URL-policy block and policy v86

The authorized internal-browser attempt did not reach the Query Studio submission boundary. The
browser's URL policy rejected the localhost reload before the synthetic request could be
submitted. Consequently there was no visible live interpretation result, no Query Studio request
admission, and no provider invocation. The key-free fake server was restored on loopback after
this browser failure.

A fresh `REPEATABLE READ, READ ONLY` control-plane inspection covers the complete interval from
policy v85 enablement at `2026-07-27T17:48:00.841467Z` through policy v86 disablement at
`2026-07-27T17:50:00.025348Z`. It found:

- zero policy-v85 reservations, including zero `interpretation` reservations;
- zero policy-v85 interpretation settlements and zero matching usage-audit rows;
- zero reservations, interpretation audits, request-window updates, or daily-usage updates of any
  kind in that exact interval; and
- after disablement, zero active attempts, zero open or stale reservations, and zero reserved
  input or output tokens.

The managed runtime requires a durable reservation before crossing the provider boundary. These
zero control-plane facts, together with the pre-submit browser block, therefore establish that the
attempt made no provider request. They also avoid treating a browser/navigation failure as a model
or product-quality result.

Policy v86 is the current disabled revision. Its applied snapshot fingerprint is
`07f5537627c18f859e616c997c2c4769e533fd1c265292956781ce5f0db5ad7b`, and its observed update time
is `2026-07-27T17:50:00.025348Z`. After v86, the provider-free schema-v2 artifact
`campaign-ledger-attestation-f4fceb557b8dd0a2ddf3c05a5f670ba2ea759f32273ef515e0daf0231a117053.json`
was authenticated and recomputed from a fresh read-only snapshot with `verified=true`. That
verification preserves the accepted historical policy-v83 campaign evidence; it is not evidence
that this browser attempt exercised live AI.

This outcome is **not** the approved live browser smoke. That acceptance item remains pending for
an internal-browser environment whose URL policy permits the loopback application, or for a
controlled deployment that the browser may reach. This closure does not authorize another
provider request or revive policy v85. Any future live attempt requires a separate reviewed
authorization and exact policy revision; external AI remains disabled in the meantime.
