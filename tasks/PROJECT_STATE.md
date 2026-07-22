# Project state

Last updated: 2026-07-22

## Accepted milestone

None yet. M00 through M16 automated development gates are complete on the operator machine; their
documented operator tests and initial commit remain unrecorded. M01 through M06 were initiated
explicitly despite that durable-state dependency gap; M07 and M08 were also initiated explicitly by
the operator, followed by the explicitly requested M09, M10, M11, M12, M13, M14, M15, and M16. This
file does not retroactively claim acceptance.

## Current capability

- Repository scaffold and architecture documents exist.
- The package installs in a clean Python 3.13.13 virtual environment.
- Package import, CLI version, doctor, bootstrap, CI-equivalent quality commands, and starter
  integrity validation are executable.
- POSIX bootstrap selects a supported Python when the default `python3` is too old; the PowerShell
  equivalent is implemented but was not run because PowerShell is unavailable.
- Git metadata is initialized without a commit, and generated/runtime/secret paths are ignored.
- A pinned PostgreSQL 16.13 container initializes a deterministic five-table synthetic dataset on
  loopback port 55433 with data checksums, UTC, locale `C`, and a hardened reader role.
- A typed application health port, psycopg adapter, composition-root binding, and CLI readiness
  command verify the reader identity, read-only defaults, and statement timeout.
- The reference north-star query is isolated under `demo/reference/` and returns exact ground truth.
- Focused immutable domain modules now define physical fields, logical concepts, mappings,
  transformations, decisions, joins, analytical requests, and validation results without I/O or
  external-system imports.
- A closed serialized transformation algebra forbids arbitrary callbacks and expressions; a pure
  identifier interpreter handles padded strings, integers, decimals, and safe integral floats with
  explicit `NULL`, empty-string, and leading-zero policies.
- Typed outcomes distinguish accepted `NULL` from invalid data and expose stable rejection codes;
  the `normalize-demo` CLI presents the complete synthetic boundary set.
- Typed mapping and join YAML fixtures round-trip deterministically and contain no raw SQL or
  executable code.
- A restricted immutable query IR covers declared scans, mapped expressions, filters, approved
  joins, aggregates, date grains, grouping, ordering, and bounded limits without a raw SQL node.
- The SQLGlot PostgreSQL adapter constructs deterministic parameterized SQL; a separate reparsing
  guard enforces statement, allowlist, join, table-count, placeholder, function, and limit policy.
- A psycopg preview adapter independently opens read-only transactions, applies transaction-local
  timeouts, caps fetched rows, and reports its observed reader identity.
- The approved north-star plan returns exact ground truth through the guarded preview CLI, and the
  malicious guard demo rejects three representative attacks without execution.
- The official DataHub Core `v1.6.0` quickstart is checksum-pinned, loopback-only, authenticated,
  and reproducible through start, health, restart, scoped reset, and stop targets.
- DataHub CLI/PostgreSQL connector `1.6.0.15` runs in isolated CPython 3.11 tooling and ingests five
  synthetic tables across CRM, legacy, bank, and reporting with descriptions and profiles.
- Logical-model UI support is enabled and verified; M07 has now created the approved synthetic
  Customer logical model and its physical/column links through an approval-gated writer.
- A dedicated one-month DataHub service-account token is held only in ignored mode-0600 state; MCP
  `0.6.0` can search and list schema fields while mutation and document tools are forced off.
- Sanitized catalog expectations support unit checks without committing tokens, raw MCP logs, or
  external/proprietary data.
- Vendor-neutral application ports expose bounded asset search/details, schema fields, governance
  metadata, lineage, query context, and decision-document lookup without MCP or DataHub SDK models
  crossing inward.
- The DataHub MCP adapter maps permissions, availability, missing tools, malformed/partial payloads,
  and offset pagination into typed application errors/results; logs contain only operation names
  and URNs.
- Contract-compatible fake and explicitly labeled recorded adapters support tests and offline demos;
  the sanitized recording contains only the three synthetic north-star representations and no
  lineage/query/document claims.
- `catalog-inspect` reads live DataHub without fallback or uses the recording only when explicitly
  selected; missing lineage/query context and disabled document tools remain visible evidence states.
- A bounded semantic candidate use case derives its catalog query from the logical concept, caps
  asset/field paging, blocks candidates by deterministic metadata, and never performs unrestricted
  all-pairs matching.
- Seven normalized deterministic signals produce typed score breakdowns, evidence, missing evidence,
  risks, and closed transformation suggestions; confidence can only prioritize human review and
  every candidate remains `needs_review`.
- Recorded synthetic evidence ranks the CRM string, legacy integer, and bank float customer keys
  first; the float candidate requires explicit finite and integral validation without truncation.
- A labeled synthetic evaluation fixture reports precision, recall, top-k recall, false positives,
  and false negatives without presenting fixture metrics as production evidence.
- Optional description interpretation is an explanation-only application port with a deterministic
  fake; it cannot alter ranking or status, and no LLM runtime dependency was added.
- Explicit review use cases edit, approve, reject, and mark mappings as a different concept while
  optimistic revisions reject stale updates and edits return affected approvals to review.
- An in-memory adapter and ignored local SQLite adapter persist versioned drafts, append-only
  semantic decisions, and publication attempts without connecting to any source database.
- A fingerprinted canonical publication contains only approved mappings; a separate exact
  `PublicationApproval` is required by both the application port and the concrete DataHub writer.
- A dedicated bounded DataHub writer publishes the synthetic Customer logical model, field
  definitions, glossary terms, structured decision property, three physical/column links, and a
  versioned decision document; the complete publication marker is written last.
- Partial DataHub failures are typed per item and remain retryable without a current marker; live
  read-back verifies actual schema, terms, dataset/field links, document, fingerprint, and four
  decision references before idempotent replay returns `already_current`.
- The Codex DataHub MCP remains mutation-disabled. The writer token stays only in ignored mode-0600
  local state and logs operations/URNs without tokens or unrestricted samples.
- Relationship discovery accepts at most two explicit north-star proposals and combines catalog
  descriptions, explicit lineage/query-context absence, declared constraints, normalized overlap,
  null/invalid quality, uniqueness, multiplicity, and row estimates without all-pairs matching.
- A read-only PostgreSQL evidence adapter allowlists the exact physical keys and closed
  transformations, returns aggregate counts only, and independently verifies reader identity,
  transaction mode, and timeout.
- Pure cardinality logic classifies Customer to AccountHolder as one-to-many from the duplicate
  normalized key and AccountHolder to Account as many-to-one from its declared foreign key plus
  consistent data evidence.
- Join candidates carry eight-signal score breakdowns, evidence, missing evidence, risks, and a
  visible Customer-123 fanout warning; confidence cannot approve, poor overlap is not recommended,
  and many-to-many execution fails closed.
- Local SQLite stores join review revisions and immutable decisions. Approved executable contracts
  include physical/logical normalized keys, closed transformation plans, join type, cardinality,
  version, evidence, risks, fanout policy, and their approval decision ID.
- A separate exact fingerprint approval gates DataHub join publication. Versioned and current
  decision documents link the three physical assets; a fresh process loads and validates the
  current contract set without relying on local draft state.
- A pure approved logical-request context defines synthetic models, canonical field types/roles,
  and logical join summaries without carrying physical dataset or column identities.
- The LLM-free guided builder converts primitive selections into an immutable
  `AnalyticalRequest`, exposes only approved logical choices and closed enums, and rejects unknown
  concepts, incompatible types/aggregates, unsupported grains/operators, missing or ambiguous
  paths, three-model overrun, and absent one-to-many fanout mitigation before planning.
- The exact north-star request requires Customer plus AccountHolder and cites only
  `customer_to_account_holder`; the active-customers-by-country control requires only Customer and
  no join. Both round-trip deterministically against the checked-in ground truth.
- A fake planner accepts only `ValidatedAnalyticalRequest` and explicitly performs no physical
  resolution or SQL execution. Ignored local SQLite drafts are parameterized, revisioned,
  idempotent for unchanged content, and revalidated against current approved context on reload.
- `request-demo` lists the explicit recorded context/closed choices, emits an entrypoint-independent
  confirmation view, supports actionable invalid overrides, and saves/reloads typed drafts without
  Streamlit or an LLM SDK.
- A pure M10 resolver fingerprints the validated logical context and selects only current approved
  mapping/contract versions, declared physical types, closed transformations, connected per-model
  datasets, and a unique shortest approved join path within the three-table limit.
- Typed resolution errors fail closed for stale, missing, unapproved, type-incompatible,
  disconnected, ambiguous, and over-limit context before compilation or execution.
- Fanout analysis applies only the contract-defined Customer `count` to `count_distinct`
  mitigation and records requested/applied operations plus its reason. Explicit
  AccountHolder relationship counts retain their different meaning.
- Application use cases coordinate planning, deterministic compilation, independent SQL guarding,
  bounded preview, and rejected-source reporting entirely through ports. Concrete SQL and
  PostgreSQL adapters remain outside application/domain modules.
- The preview adapter now verifies the exact configured reader, read-only transaction, and applied
  timeout before executing. A separate exact-allowlist source reporter enforces the same controls
  and returns only typed invalid join-key records.
- `governed-demo` exposes the complete resolved plan before optional execution. The north-star
  returns exact `2, 1, 1` ground truth plus `127.5`, `NaN`, and `NULL` rejections; the Customer-only
  control uses no relationship, and the explicit holder-relationship count returns `3, 1, 1`.
- A vendor-neutral `IntentParserPort` accepts only untrusted text, current language, and a bounded
  four-field approved logical vocabulary. The structured domain output has no SQL, physical asset,
  tool, approval, or execution field and forbids extras.
- The deterministic intent adapter resolves the Spanish north-star request without an API key and
  exposes explicit alternatives for customer-distinct versus holder-relationship count,
  count-versus-list, date meaning, and unknown role values rather than silently guessing.
- Application validation independently rejects hallucinated models/fields, unsupported enums,
  SQL-like filter values, out-of-vocabulary roles, language mismatch, instruction-like control
  text, stale confirmation fingerprints, unavailable choices, and unresolved ambiguity before
  semantic planning.
- The optional OpenAI Responses adapter uses structured output with `store=false` and no tools. SDK
  2.46.0 and the adapter contract were tested with an in-memory client; no live provider call was
  made and live failures never fall back to the fake.
- `intent-demo` previews before confirmation and never compiles or executes SQL. After the exact
  count-distinct confirmation it returns only the typed request plus request/plan fingerprints;
  acceptance proves these equal guided mode through the unchanged pure M10 planner.
- The LLM optional runtime extra is installed and contract-tested; Streamlit remains unvalidated.
- An explicit immutable M12 state machine covers context retrieval, intent resolution, typed
  decisions, semantic resolution, plan ready, independent SQL validation, execution, publication
  proposal/completion, retry, failure, and terminal states without a background scheduler.
- Human checkpoints are typed and bound to exact interpretation, plan, publication, or failure
  fingerprints. A plain resume is inert; it cannot approve, retry, query, or publish anything.
- Local SQLite persists bounded typed workflow state with optimistic revisions. A new process
  restores the same approved logical/planning versions, preview summary, decisions, checkpoint,
  and trace without persisting compiled SQL, parameters, credentials, prompts, or private reasoning.
- Every external action emits sanitized start/result/error events with operation, URN/fingerprint
  references, counts, stable codes, timing, and safety facts. Trace fact keys explicitly exclude
  prompt, token, password, secret, reasoning, and chain-of-thought surfaces.
- Execution from a resumed approval deterministically regenerates the exact stored plan, reruns the
  independent SQL guard, and persists preview completion before rejection inspection. Retrying a
  later read does not repeat the preview; retrying a preview requires an explicit bound decision.
- Optional M12 context publication is a separate port requiring exact typed approval. The live
  DataHub adapter writes only fingerprints/approval identity under the bounded workflow-document
  prefix and reads it back; replay returns `already_current`. Saved query recipes remain M13.
- `workflow-demo` starts, shows, confirms, approves/declines, retries, publishes/skips, and resumes
  durable state. Recorded/fake defaults and live catalog/publication choices are labeled explicitly.
- A composition-root-built Streamlit application now presents Overview, Semantic Models,
  Relationships, Query Studio, and Decisions without constructing adapters or moving business
  rules into page callbacks or session state.
- Immutable application view models expose integration health, interpretations, evidence,
  confidence, risks, transformations, approvals, join cardinality, fanout policy, SQL checks,
  results, rejections, decisions, and sanitized traces. Execution availability is derived only
  from typed workflow checkpoints and approvals.
- The deterministic three-action judge path loads the synthetic scenario, confirms the explicit
  distinct-customer interpretation, and approves a freshly guarded read-only preview. Missing
  approvals never expose the execute action.
- Result views always show selected assets, approved join path, `COUNT DISTINCT` mitigation,
  validation status, reader identity, limits, limitations, and downloadable rejection/validation
  reports. Recorded/fake/live modes remain visibly labeled and never fall back silently.
- Typed safe states cover invalid requests, catalog/source unavailability, unapproved joins,
  timeout, publication failure, retry, loading, and empty views without browser stack traces,
  credentials, prompts, or private reasoning.
- The clean in-app browser path completed at 1440 × 1000 and 1024 × 900 with no console errors or
  horizontal document overflow. Synthetic screenshots and the remaining operator failure drills
  are recorded under `docs/screenshots/m14/` and `docs/14_BROWSER_ACCEPTANCE.md`.
- M16 adds a strict clean-room runner that refuses a missing or dirty release `HEAD`, recreates the
  Python environment and both synthetic service stacks, executes every quality/integration/
  acceptance/evaluation/UI/persistence gate, and scans candidate sources, licenses, links, and
  secret/runtime paths.
- A separate development-only flag permits evidence gathering before the initial commit while
  emitting explicit warnings; it never converts that evidence into a release-commit claim.
- Rejected-source reporting now enforces bounded materialization with an exact total/truncation
  contract, and negative identifiers fail consistently across the compiler, join evidence, and
  rejection adapters. Preview rows/timeouts are capped independently by the concrete adapter.
- DataHub catalog reads reject cross-environment URNs; health selects the exact GMS container;
  admin/writer provisioning waits through the pinned authorization-cache window; and canonical,
  relationship, and recipe writers independently ensure their governed decision property.
- The release audit's whole-tree AST check verifies inward dependency direction, while candidate,
  disclosure, license, local/external link, generated-artifact, and secret scans retain their exact
  counts and findings in a tracked report.

## Test evidence

On macOS 26.5.1 arm64 with CPython 3.13.13:

- `python -m pip install -e ".[dev]"`: passed in a clean `.venv`;
- Ruff format check and lint: passed;
- strict `mypy src`: passed;
- unit selection: 5 passed;
- `schemabridge version`: `0.1.0`;
- `schemabridge doctor`: all required checks passed;
- `python scripts/validate_starter.py`: passed;
- `make check`: passed before M00 cleanup and twice consecutively after cleanup.

M01 PostgreSQL evidence on the same machine:

- `make demo-reset`: passed twice from a deleted Compose volume after hardening;
- Compose status: PostgreSQL 16.13 healthy on `127.0.0.1:55433`;
- `make demo-health`: passed with the expected reader and 5000 ms timeout;
- `make demo-query`: returned the exact 2, 1, 1 rows;
- `pytest -m integration -k postgres`: 14 passed after each clean reset;
- `make check`: passed with 8 unit tests and 20 strictly typed source files;
- reader write/DDL tests: insert, update, delete, create, temporary create, and alter rejected.

M02 pure-domain evidence in the existing clean development environment:

- focused domain/normalization selection: 58 passed, 8 deselected;
- domain/test Ruff check and strict domain mypy: passed;
- `schemabridge normalize-demo --json`: passed with canonical `123` equivalence and stable rejection
  codes/reasons;
- `make check`: passed with 66 unit tests, 30 strictly typed source files, and 41 formatted files;
- deterministic mapping/join YAML round trips and domain import-boundary checks: passed.

M03 query safety evidence with SQLGlot 29.0.1 and the healthy synthetic PostgreSQL service:

- editable `.[dev,postgres,sql]` installation: passed;
- focused compiler/guard/query-plan selection: 54 passed, 68 deselected;
- query/SQL integration selection: 6 passed; complete integration suite: 19 passed;
- north-star preview: exact 2, 1, 1 rows as `schemabridge_reader`, read-only, at 5000 ms timeout;
- 50 ms cancellation, 60,000 ms timeout reporting, and hard fetched-row-cap paths: passed;
- `make check`: passed with 122 unit tests, 36 strictly typed source files, and 53 formatted files;
- statement smuggling, DDL/DML/utilities and locking reads, destructive/recursive CTEs, unknown
  assets/columns, Cartesian/self/fourth-table queries, joins that ignore the new relation, unsafe
  functions, bad limits, and parameter mismatches: rejected by stable policy codes.

M04 DataHub evidence on the same machine:

- DataHub Core `v1.6.0`, CLI/connector `1.6.0.15`, MCP `0.6.0`, and uv `0.11.30` were verified;
- a complete `datahub` Compose volume reset, pinned restart, admin initialization, ingestion, scoped
  identity provision, catalog check, and MCP check passed;
- ingestion produced 61 aspects with no sink failures and profiles for all five demo tables;
- the catalog check verified four schemas, descriptions, exact fields/row profiles, and the native
  logical-model UI feature flag;
- two persistence restarts passed; the final restart preserved both metadata and the scoped token;
- MCP service-account search and `list_schema_fields` passed while known mutation tools were absent;
- the service token actor matched the dedicated account and all checked administrative/write
  platform privileges were false;
- focused M04 unit tests: 9 passed; `make check` passed with Ruff over 61 files, strict mypy over 36
  source files, and 131 unit tests; the complete 19-test PostgreSQL integration suite also passed.

M05 catalog-read evidence against the same authenticated local DataHub:

- editable `.[dev,datahub]` installation passed with MCP client `1.28.1`;
- focused catalog/DataHub unit selection: 22 passed, 122 deselected;
- the shared live DataHub adapter contract: 1 passed, 163 deselected;
- fake and recorded adapters passed the same north-star, pagination, missing-evidence, and
  unavailable-document contract;
- live `catalog-inspect crm.customers --json` returned the expected asset, four fields, descriptions,
  and explicit missing lineage/query-context states;
- `make check`: Ruff passed over 74 files, strict mypy over 43 source files, and 144 unit tests passed
  with 20 integration/acceptance tests deselected.

M06 semantic-candidate evidence using only the explicit synthetic recording:

- focused candidate/matching/scoring selection: 14 passed, 143 deselected;
- the exact module CLI ranked `crm.customers.customer_id`, `legacy.client_master.client_no`, and
  `bank.account_holders.gf_customer_id` first with all statuses `needs_review`;
- the float representation reported unsafe-float and finite/integral-validation risks;
- fixture evaluation reported precision/recall/F1 `0.750` and visibly listed one intentional false
  positive and one intentional false negative;
- `make check`: Ruff passed over 86 files, strict mypy over 51 source files, and 157 unit tests passed
  with 20 integration/acceptance tests deselected;
- starter integrity validation, `git diff --check`, and an explicit trailing-whitespace scan of all
  M06 files passed.

M07 canonical-review and DataHub write-back evidence on the same local Core stack:

- exact approval/decision/publication unit selection: 6 passed, 162 deselected; the complete M07
  unit modules are also included in the full gate;
- dedicated writer provisioning passed with an ignored mode-0600 token and three bounded policies;
- two superseded tokens from failed provisioning retries were revoked after exact metadata
  resolution; the service account now has exactly one active M07 writer token;
- the first live integration exposed a glossary write failure and a second exposed a schema-field
  link permission mismatch; both returned typed partial results and left the marker absent;
- after correcting only the verified property/read and scoped-policy paths, the same immutable
  publication recovered and the live integration passed;
- the CLI approved all four synthetic mappings, published nine tracked items, read the context back
  from DataHub, and returned `already_current` on an identical replay;
- the live context contains two canonical fields, three dataset plus four column links, two glossary
  terms, one decision document, the structured decision property, and four decision/version refs;
- `make check`: Ruff passed over 101 files, strict mypy passed over 59 source files, and 168 unit
  tests passed with 21 service/acceptance tests deselected;
- the final live integration passed 1 test with 188 deselected; starter validation and diff hygiene
  passed after documentation updates.

M08 relationship and governed-contract evidence on the same PostgreSQL/DataHub services:

- focused join/cardinality/fanout selection passed 24 tests with 157 deselected;
- the PostgreSQL relationship integration returned exact 7/9 and 9/9 row profiles, 5/9 matching
  distinct keys, maximum multiplicities 1/2 and 1/1, and the expected FK direction as the verified
  read-only reader at 5000 ms;
- the three-test join/relationship integration selection passed; the live DataHub path
  published/reused the versioned and current contract documents,
  loaded both contracts through a fresh adapter instance, and returned `already_current` on replay;
- a separate CLI process loaded contract-set version 3, two immutable decision IDs, and the three
  related physical assets from the current DataHub document;
- `make check` passed Ruff over 115 files, strict mypy over 67 source files, and 181 unit tests with
  23 service/acceptance tests deselected; starter and diff hygiene are recorded in the handoff.

M09 guided-request evidence without external services:

- the focused analytical-request/guided selection passed 22 tests with 180 deselected;
- exact north-star and Customer-only ground-truth requests round-tripped deterministically and
  carried no raw SQL or physical identifiers;
- invalid `SUM(customer_key)`, unknown filter fields, unsupported grains, absent/ambiguous paths,
  and one-to-many fanout regressions returned stable actionable findings before planner handoff;
- the exact `request-demo` module command succeeded, while expected malicious/unsupported choices
  exited nonzero; a fresh temporary SQLite draft reloaded revision 1 with the same fingerprint;
- `make check` passed Ruff over 125 files, strict mypy over 74 source files, and 202 unit tests with
  23 service/acceptance tests deselected.

M10 governed-planner evidence against the healthy synthetic PostgreSQL service:

- focused planner/resolution/fanout unit selection passed 13 tests; planner/preview integration
  passed 7 tests; the guided north-star acceptance selection passed 1 test;
- the plan-only CLI emitted the complete labeled context, selected mapping/contract versions,
  assumptions, fanout mitigation, restricted query plan, allowlist policy, parameterized SQL, and
  no database result;
- the executed CLI returned exact north-star `2, 1, 1` rows and the three typed rejected records as
  `schemabridge_reader`, read-only, with a 5000 ms timeout for both bounded reads;
- the no-join control returned `ES=4`, `FR=1`, and `PT=1` from only `crm.customers`; the explicit
  relationship count returned `3, 1, 1` without being changed to a Customer entity count;
- security regressions cover compiler float-range rejection, source-field allowlisting, independent
  guard traversal, application port direction, context approval/version/type/path failures, and
  contract-scoped fanout behavior.
- `make check` passed Ruff over 135 Python files, strict mypy over 80 source files, and 213 unit
  tests with 26 integration/acceptance tests deselected; `git diff --check` passed.

M11 natural-language intent evidence without external services:

- editable `.[dev,llm]` installation passed with OpenAI SDK 2.46.0;
- the focused intent/language/prompt selection passed 19 tests with 212 deselected;
- the natural-language acceptance selection passed 1 test with 257 deselected and proved exact
  guided/natural request and resolved-plan fingerprint equality;
- the Spanish CLI preview returned the expected dimension, distinct-customer metric, SECONDARY
  filter, and two explicit count meanings; exact confirmation remained compilation/execution-free;
- `agrupa clientes` exposed count and unavailable-list alternatives, while an injection-style DROP
  request produced no request, could not be confirmed, registered no tools, and generated no SQL;
- `make check` passed Ruff over 143 Python files, strict mypy over 85 source files, and 231 unit
  tests with 27 integration/acceptance tests deselected.

M12 orchestration evidence on the same local DataHub/PostgreSQL services:

- the focused workflow/orchestrator/trace selection passed 17 tests with 223 deselected;
- SQLite restart, typed checkpoint enforcement, safe trace structure, catalog retry without
  fallback, rejection retry without preview replay, and publication retry/idempotency passed;
- two workflow acceptance paths passed with 267 deselected: recorded catalog plus real PostgreSQL,
  and authenticated live DataHub MCP plus real PostgreSQL and approved DataHub publication;
- both acceptance paths returned exact `2, 1, 1` rows, three stable rejection codes, reader
  `schemabridge_reader`, and read-only execution; the live document read-back and fresh-adapter
  replay returned `already_current` without a duplicate document;
- a six-command CLI run proved start/show restart equivalence, intent and execution checkpoints,
  a second SQL-validation event, exactly one preview, publication proposal, and explicit skip;
- `make check` passed Ruff over 154 Python files, strict mypy over 93 source files, and 240 unit
  tests with 29 integration/acceptance tests deselected.

M13 recipe/context-reuse capability:

- immutable recipes contain the normalized intent, exact approved model/mapping/join versions,
  source-schema/compiler/plan/query fingerprints, validation evidence, limitations, provenance,
  and linked synthetic assets, but no executable SQL;
- explicit approval binds the exact recipe version and fingerprint; persistent fake and DataHub
  repositories return `already_current` for an identical replay and preserve immutable provenance;
- a fresh workflow retrieves the current recipe, compares every compatibility boundary, reports
  typed stale reasons, and still replans, recompiles, independently guards, requests execution
  approval, and runs the read-only preview instead of executing saved SQL;
- the focused recipe/fingerprint/reuse unit selection passed 9 tests, the live DataHub document
  integration passed 1, and the real PostgreSQL restart/reuse acceptance path passed 1;
- `make check` passed Ruff formatting/lint over 163 Python files, strict mypy over 99 source files,
  and 246 unit tests with 31 service/acceptance tests deselected;
- `examples/query-recipe-secondary-holders.yml`, `examples/generated-secondary-holders.sql`, and
  `examples/query-validation-report.md` were generated from the actual synthetic `2, 1, 1` run and
  clearly distinguish inspection output from executable application input.

M14 judge-ready Streamlit evidence on the same synthetic PostgreSQL service:

- editable `.[dev,ui,postgres,sql,datahub,llm]` installation passed with Streamlit 1.59.2 and
  NumPy 2.2.6;
- the focused view-model/UI selection passed 40 tests with 214 deselected;
- the Streamlit acceptance path passed and proved three primary interactions, approval gating,
  exact `2, 1, 1` results, three typed rejected records, and visible safety/context facts;
- the complete acceptance suite passed 6 tests with 280 deselected against the canonical
  `schemabridge_reader` DSN;
- `make check` passed Ruff formatting/lint over 169 Python files, strict mypy over 102 source files,
  and 254 unit tests with 32 integration/acceptance tests deselected;
- POSIX bootstrap syntax and starter integrity validation passed after aligning both bootstrap
  scripts with the optional UI dependency; PowerShell remains unavailable and unexecuted;
- a final headless Streamlit 1.59.2 startup on port 8502 returned `ok` from `/_stcore/health` and
  stopped cleanly; no deprecation warning was emitted;
- the in-app browser verified all five pages, three-action execution gating, exact results and
  rejections, downloads, labels, 1024 px responsive behavior, and an empty browser console.

M15 reproducible evaluation capability and evidence:

- a versioned manifest composes the existing synthetic semantic, query, join, mapping, approved
  context, security, and SQL-free recipe fixtures without accepting manifest-injected paths;
- the `evaluate` use case measures candidate precision/recall/F1/top-k, exact join paths and
  cardinalities, typed-intent equivalence, independently guarded compilation, bounded read-only
  execution, normalized row correctness, source rejections, 38 guard-only attacks, and current/stale
  recipe reuse through injected ports;
- JSON and Markdown reports show every raw numerator, denominator, skip, failure, false positive,
  false negative, and rejected case; the small fixture has no confidence interval or unjustified
  regression threshold;
- deterministic and optional live-LLM runs are structurally separate; the default remains key-free
  and records the live run as `not_run` rather than mixing or fabricating results;
- two independent `make evaluate` clean-volume runs produced byte-identical final artifacts;
  `reports/evaluation.json` remains ignored while `examples/evaluation-report.md` is checked-in
  synthetic evidence labeled as an uncommitted snapshot, not a release-commit claim;
- focused evaluation tests passed 7 with 252 deselected, all 26 integration tests passed, all 7
  acceptance tests passed, and `make check` passed Ruff over 178 files, strict mypy over 109 source
  files, and 259 unit tests.

M16 final uninterrupted development clean-room evidence:

- the complete `.[dev,ui,postgres,sql,datahub,llm]` environment installed into a recreated `.venv`
  on CPython 3.13.13 and `pip check` passed;
- PostgreSQL 16.13 was recreated from its named volume and passed exact reader, read-only, and 5000
  ms timeout checks;
- DataHub Core v1.6.0 was recreated from its three named volumes, initialized after observable
  authorization convergence, ingested 61 events, exposed five described/profiled datasets and the
  logical-model UI, and passed scoped MCP search/schema reads with mutation tools absent;
- `make check` passed Ruff formatting/lint over 182 files, strict mypy over 110 source files, and
  274 unit tests with 33 integration/acceptance tests deselected;
- all 26 integration tests and all 7 acceptance tests passed from the clean service state;
- deterministic evaluation passed with live LLM explicitly `not_run`; headless Streamlit health
  returned `ok`; DataHub restart preserved catalog/MCP/governed documents and 3 focused write-back
  read-backs passed;
- the uninterrupted run's release scanner passed 314 candidate files; after preserving the five
  operator-readable panel reports, the final scan passed 319 candidate files, 19 installed
  direct-dependency licenses, and 20 external links; starter validation, shell syntax, and
  `git diff --check` also passed;
- strict `make release-audit` and `make release-clean` were run and failed closed because `HEAD`
  does not exist and the tree is untracked. This is the expected release blocker, not a passed gate.

## Known risks

- The standalone Codex CLI package 0.104.0 is present but cannot start because its native arm64
  executable is missing; the active Codex desktop task is unaffected.
- PowerShell bootstrap behavior is statically reviewed but unverified on Windows.
- The folder had no Git metadata; M00 initialized `.git`, but no baseline or M00 commit was created.
- Remote GitHub Actions were updated for PostgreSQL integration and acceptance but were not
  executed from this uncommitted local folder; the same commands passed locally.
- The generic transformation algebra is serializable in M02, but only identifier normalization is
  interpreted in the pure domain; the M03 compiler implements only its explicit north-star subset
  and fails closed for other operations.
- The M03 guard deliberately rejects wildcard projection, recursive CTEs, self joins,
  `USING`/`NATURAL` joins, unallowlisted functions, and all queries above three physical tables.
- The active DataHub stack leaves approximately 5.7 GiB host disk free, below the official 13 GB
  clean-start recommendation; no cache/image prune was performed because unrelated projects exist.
- The DataHub MCP package emits an upstream experimental-SDK import warning on startup; the pinned
  read checks pass and mutation tools remain disabled.
- The physical source catalog still contains no ingested lineage or dataset-query entities. M05
  reports those states explicitly; M07 adds approved logical-parent relationships and M08 records
  lineage/query usage as missing evidence rather than inferring it from its decision documents.
- M06 pattern/overlap facts and evaluation labels are small synthetic fixtures, not live catalog
  measurements or production validation; lineage and query-usage evidence remain explicitly missing.
- DataHub Core `v1.6.0` did not expose its documented batch logical-relationship OpenAPI route
  locally; the writer uses the documented equivalent `LogicalParent` dataset/schema-field aspect
  path instead. GraphQL exposes direct `setLogicalParent`, but no proposal mutation.
- The stock local DataHub all-users policy grants the writer service account personal-token
  generation in addition to SchemaBridge's bounded privileges; SchemaBridge never calls that
  mutation, and all writer metadata policies are limited to synthetic canonical assets.
- Hosting choice remains intentionally open until M17.
- AccountHolder-to-Account seed rows happen to be unique on both sides; the `many_to_one`
  classification therefore depends on the declared FK direction, with this limitation retained as
  a contract risk instead of pretending the bounded rows demonstrate left-side duplication.
- The M09 logical context and M10 physical planning context are explicitly labeled synthetic
  ground-truth recordings. They include fields, mappings, physical types, and role-code transforms
  needed by the acceptance fixtures, but do not claim that this complete set was read back from
  live DataHub. The live workflow never falls back to these recordings implicitly.
- M11 verified the live adapter contract against the installed SDK with an in-memory client only.
  No provider/model combination was called live, so latency, refusal behavior, and model quality
  remain unverified; the deterministic fake is the explicit default and never masquerades as live.
- M12 still resolves intent and physical mappings from the explicit synthetic M11/M10 recordings;
  its separate live acceptance proves catalog asset reads and publication but does not claim the
  complete governed planning context was reconstructed from DataHub.
- M12 workflow documents intentionally contain fingerprints and approval identity only. They are
  observable audit/context markers, not M13 query-recipe documents and not a store for SQL or results.
- M13 recipes use the explicit synthetic M10 planning context for compatibility because complete
  mapping transformations are not yet reconstructed from live DataHub. Live DataHub verifies the
  recipe document write/read/provenance boundary; it does not turn recorded mappings into live
  production evidence.
- M14 intentionally defaults to the deterministic fake intent parser, recorded catalog/candidate/
  planning evidence, live read-only synthetic PostgreSQL, and fake local publication. The UI labels
  every boundary; selecting live DataHub never silently reconstructs missing planning mappings or
  falls back to the recording.
- Automated tests cover all required safe UI states, but stopping live DataHub/PostgreSQL during a
  browser session, recording the clean demo, and the second-person judge check remain unperformed
  operator acceptance work.
- M15 reports deliberately contain raw counts only. The candidate fixture is seven tuned synthetic
  cases with one retained homonym false positive and one hidden-synonym false negative; it cannot
  support production-quality or statistical-generalization claims.
- Optional live-LLM evaluation remains unrun and separate. The current generated artifacts identify
  `working-tree-uncommitted`; they must be regenerated after the reviewed release commit before any
  score is quoted as release evidence.
- DataHub publication audit facts remain distributed across immutable approval/decision/payload and
  per-target results rather than one persisted per-target actor/time old/new/result record. This is
  an explicit high M16 release blocker, not a hidden completion claim.
- A scoped writer token minted immediately before terminal provisioning failure might remain valid
  without a saved local reference; revoke it manually after such a failure.
- The clean live-browser timing/recording and second-person judge explanation remain operator work.

## Next eligible milestone

Resolve or explicitly reject the M16 governance blocker, complete the M00 through M16 operator
manual tests, create the reviewed initial release-candidate commit, run strict `make release-clean`,
and record an explicit go/no-go decision before starting M17.
