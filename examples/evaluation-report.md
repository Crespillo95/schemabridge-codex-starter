# SchemaBridge synthetic evaluation report

> Generated evidence for the small synthetic fixture only. Raw counts are shown; no confidence interval or production-quality claim is justified.

- Overall required-run status: **PASS**
- Source revision: `9e8b69e1adce8e144b345d3b0d33482558804dc6 (dirty/uncommitted; not a release-commit claim)`
- Source fingerprint: `99df0d6023c19f4878db5df3bb851edc79e8d5a4d2764f643aa9448535a692cb`
- Ground-truth version: `2`
- Fixture fingerprint: `61c6930b16d1c477fbf1e0caef54b69a362d65998f8dbab63a14a0e782e03138`
- Package: `0.1.0`
- Regression thresholds: none; the measured fixture is too small to justify one.

## deterministic

Adapter: `recorded fixtures + deterministic fake + read-only PostgreSQL`.

Status: **completed**

### Semantic candidate matching

Section status: **completed**

| Metric | Raw ratio | Value | Evaluated | Skipped | Failures |
|---|---:|---:|---:|---:|---:|
| `candidate_precision` | 3/4 | 0.750 | 7 | 0 | 1 |
| `candidate_recall` | 3/4 | 0.750 | 7 | 0 | 1 |
| `candidate_f1` | 6/8 | 0.750 | 7 | 0 | 2 |
| `candidate_top_1_recall` | 1/4 | 0.250 | 7 | 0 | 0 |
| `candidate_top_3_recall` | 2/4 | 0.500 | 7 | 0 | 0 |
| `candidate_top_5_recall` | 3/4 | 0.750 | 7 | 0 | 0 |

- `candidate_precision`: Recommendation-threshold precision on the labeled synthetic fixture.
- `candidate_recall`: Recommendation-threshold recall on the labeled synthetic fixture.
- `candidate_f1`: Exact 2TP / (2TP + FP + FN) count formula.
- `candidate_top_1_recall`: Positive labels found in the first 1 ranked cases.
- `candidate_top_3_recall`: Positive labels found in the first 3 ranked cases.
- `candidate_top_5_recall`: Positive labels found in the first 5 ranked cases.

| Case | Status | Expected | Actual | Detail |
|---|---|---|---|---|
| `candidate_crm_customer_id_true` | observed | positive | positive | true_positive |
| `candidate_legacy_client_no_true` | observed | positive | positive | true_positive |
| `candidate_holder_float_customer_id_true` | observed | positive | positive | true_positive |
| `candidate_holder_link_id_negative` | observed | negative | negative | true_negative |
| `candidate_customer_status_negative` | observed | negative | negative | true_negative |
| `candidate_support_customer_key_homonym` | observed | negative | positive | false_positive |
| `candidate_archive_subject_ref_hidden_synonym` | observed | positive | negative | false_negative |

### Join path and cardinality

Section status: **completed**

| Metric | Raw ratio | Value | Evaluated | Skipped | Failures |
|---|---:|---:|---:|---:|---:|
| `join_path_accuracy` | 5/5 | 1.000 | 5 | 0 | 0 |
| `join_cardinality_accuracy` | 2/2 | 1.000 | 2 | 0 | 0 |

- `join_path_accuracy`: Exact query join-path matches, including the no-join control.
- `join_cardinality_accuracy`: Exact expected cardinality classifications for both governed contracts.

| Case | Status | Expected | Actual | Detail |
|---|---|---|---|---|
| `join_path_secondary_holders_by_registration_date` | passed | customer_to_account_holder | customer_to_account_holder | Exact ordered approved join-contract path comparison. |
| `join_path_active_customers_by_country` | passed | no_join | no_join | Exact ordered approved join-contract path comparison. |
| `join_path_active_products_by_category` | passed | no_join | no_join | Exact ordered approved join-contract path comparison. |
| `join_path_delivered_orders_by_month` | passed | sales_order_to_shipment | sales_order_to_shipment | Exact ordered approved join-contract path comparison. |
| `join_path_commerce_revenue_by_category_month` | passed | product_to_sale_line,sales_order_to_sale_line | product_to_sale_line,sales_order_to_sale_line | Exact ordered approved join-contract path comparison. |
| `cardinality_customer_to_account_holder` | passed | one_to_many | one_to_many | Cardinality classified from declared and aggregate read evidence. |
| `cardinality_account_holder_to_account` | passed | many_to_one | many_to_one | Cardinality classified from declared and aggregate read evidence. |

### Natural-language intent equivalence

Section status: **completed**

| Metric | Raw ratio | Value | Evaluated | Skipped | Failures |
|---|---:|---:|---:|---:|---:|
| `intent_equivalence` | 1/1 | 1.000 | 1 | 4 | 0 |

- `intent_equivalence`: Exact typed-request equality after explicit ambiguity confirmation.

| Case | Status | Expected | Actual | Detail |
|---|---|---|---|---|
| `intent_secondary_holders_by_registration_date` | passed | d94f4317d638a514abfa960208aada13b021ba10f42d6a89ba1c06fba05383ce | d94f4317d638a514abfa960208aada13b021ba10f42d6a89ba1c06fba05383ce | Confirmed typed request fingerprint equivalence; no SQL involved. |
| `intent_active_customers_by_country` | skipped | typed request equivalence | not_evaluated | The deterministic M11 parser intentionally has no active-customer control phrase; this query remains evaluated for planning, execution, and result correctness. |
| `intent_active_products_by_category` | skipped | typed request equivalence | not_evaluated | The deterministic M11 parser is intentionally Customer-focused until M27; this typed Product request remains evaluated for planning, execution, and exact results. |
| `intent_delivered_orders_by_month` | skipped | typed request equivalence | not_evaluated | The deterministic M11 parser is intentionally Customer-focused until M27; this typed fulfillment request remains evaluated for planning, fanout, execution, and exact results. |
| `intent_commerce_revenue_by_category_month` | skipped | typed request equivalence | not_evaluated | The deterministic M11 parser is intentionally Customer-focused until M27; this typed three-table commerce request remains evaluated end to end. |

### Compilation, execution, and results

Section status: **completed**

| Metric | Raw ratio | Value | Evaluated | Skipped | Failures |
|---|---:|---:|---:|---:|---:|
| `compile_success` | 5/5 | 1.000 | 5 | 0 | 0 |
| `execution_success` | 5/5 | 1.000 | 5 | 0 | 0 |
| `result_correctness` | 5/5 | 1.000 | 5 | 0 | 0 |
| `source_rejection_correctness` | 5/5 | 1.000 | 5 | 0 | 0 |

- `compile_success`: Cases producing independently guarded final SQL.
- `execution_success`: Cases completing with the exact reader, read-only mode, and timeout.
- `result_correctness`: Normalized result-row equality; SQL text equality is not used.
- `source_rejection_correctness`: Exact stable rejection-code sequence, including the zero-rejection control.

| Case | Status | Expected | Actual | Detail |
|---|---|---|---|---|
| `compile_secondary_holders_by_registration_date` | passed | independently_guarded_query | accepted | Restricted plan compiled and final SQL passed the independent guard. |
| `execution_secondary_holders_by_registration_date` | passed | schemabridge_reader\|read_only=true\|timeout_ms=5000 | schemabridge_reader\|read_only=true\|timeout_ms=5000 | Bounded preview safety facts observed from PostgreSQL. |
| `result_secondary_holders_by_registration_date` | passed | [[{"column":"registration_date","value":"date:2026-01-01"},{"column":"secondary_holder_customers","value":"int:2"}],[{"column":"registration_date","value":"date:2026-01-02"},{"column":"secondary_holder_customers","value":"int:1"}],[{"column":"registration_date","value":"date:2026-01-03"},{"column":"secondary_holder_customers","value":"int:1"}]] | [[{"column":"registration_date","value":"date:2026-01-01"},{"column":"secondary_holder_customers","value":"int:2"}],[{"column":"registration_date","value":"date:2026-01-02"},{"column":"secondary_holder_customers","value":"int:1"}],[{"column":"registration_date","value":"date:2026-01-03"},{"column":"secondary_holder_customers","value":"int:1"}]] | Type-tagged, column-keyed, order-independent normalized row multiset. |
| `rejections_secondary_holders_by_registration_date` | passed | non_integral_identifier,non_finite_identifier,null_join_key | non_integral_identifier,non_finite_identifier,null_join_key | Stable rejected-source codes; source values are not retained here. |
| `compile_active_customers_by_country` | passed | independently_guarded_query | accepted | Restricted plan compiled and final SQL passed the independent guard. |
| `execution_active_customers_by_country` | passed | schemabridge_reader\|read_only=true\|timeout_ms=5000 | schemabridge_reader\|read_only=true\|timeout_ms=5000 | Bounded preview safety facts observed from PostgreSQL. |
| `result_active_customers_by_country` | passed | [[{"column":"active_customers","value":"int:1"},{"column":"country_code","value":"str:FR"}],[{"column":"active_customers","value":"int:1"},{"column":"country_code","value":"str:PT"}],[{"column":"active_customers","value":"int:4"},{"column":"country_code","value":"str:ES"}]] | [[{"column":"active_customers","value":"int:1"},{"column":"country_code","value":"str:FR"}],[{"column":"active_customers","value":"int:1"},{"column":"country_code","value":"str:PT"}],[{"column":"active_customers","value":"int:4"},{"column":"country_code","value":"str:ES"}]] | Type-tagged, column-keyed, order-independent normalized row multiset. |
| `rejections_active_customers_by_country` | passed | none | none | Stable rejected-source codes; source values are not retained here. |
| `compile_active_products_by_category` | passed | independently_guarded_query | accepted | Restricted plan compiled and final SQL passed the independent guard. |
| `execution_active_products_by_category` | passed | schemabridge_reader\|read_only=true\|timeout_ms=5000 | schemabridge_reader\|read_only=true\|timeout_ms=5000 | Bounded preview safety facts observed from PostgreSQL. |
| `result_active_products_by_category` | passed | [[{"column":"active_products","value":"int:8"},{"column":"category","value":"str:BOOKS"}],[{"column":"active_products","value":"int:9"},{"column":"category","value":"str:ELECTRONICS"}],[{"column":"active_products","value":"int:9"},{"column":"category","value":"str:HOME"}],[{"column":"active_products","value":"int:9"},{"column":"category","value":"str:SPORTS"}]] | [[{"column":"active_products","value":"int:8"},{"column":"category","value":"str:BOOKS"}],[{"column":"active_products","value":"int:9"},{"column":"category","value":"str:ELECTRONICS"}],[{"column":"active_products","value":"int:9"},{"column":"category","value":"str:HOME"}],[{"column":"active_products","value":"int:9"},{"column":"category","value":"str:SPORTS"}]] | Type-tagged, column-keyed, order-independent normalized row multiset. |
| `rejections_active_products_by_category` | passed | none | none | Stable rejected-source codes; source values are not retained here. |
| `compile_delivered_orders_by_month` | passed | independently_guarded_query | accepted | Restricted plan compiled and final SQL passed the independent guard. |
| `execution_delivered_orders_by_month` | passed | schemabridge_reader\|read_only=true\|timeout_ms=5000 | schemabridge_reader\|read_only=true\|timeout_ms=5000 | Bounded preview safety facts observed from PostgreSQL. |
| `result_delivered_orders_by_month` | passed | [[{"column":"delivered_orders","value":"int:11"},{"column":"ordered_at","value":"date:2026-02-01"}],[{"column":"delivered_orders","value":"int:22"},{"column":"ordered_at","value":"date:2026-01-01"}]] | [[{"column":"delivered_orders","value":"int:11"},{"column":"ordered_at","value":"date:2026-02-01"}],[{"column":"delivered_orders","value":"int:22"},{"column":"ordered_at","value":"date:2026-01-01"}]] | Type-tagged, column-keyed, order-independent normalized row multiset. |
| `rejections_delivered_orders_by_month` | passed | malformed_identifier,negative_identifier,malformed_identifier,null_join_key | malformed_identifier,negative_identifier,malformed_identifier,null_join_key | Stable rejected-source codes; source values are not retained here. |
| `compile_commerce_revenue_by_category_month` | passed | independently_guarded_query | accepted | Restricted plan compiled and final SQL passed the independent guard. |
| `execution_commerce_revenue_by_category_month` | passed | schemabridge_reader\|read_only=true\|timeout_ms=5000 | schemabridge_reader\|read_only=true\|timeout_ms=5000 | Bounded preview safety facts observed from PostgreSQL. |
| `result_commerce_revenue_by_category_month` | passed | [[{"column":"category","value":"str:BOOKS"},{"column":"net_revenue","value":"decimal:1496.45"},{"column":"ordered_at","value":"date:2026-01-01"},{"column":"units","value":"int:25"}],[{"column":"category","value":"str:BOOKS"},{"column":"net_revenue","value":"decimal:988"},{"column":"ordered_at","value":"date:2026-02-01"},{"column":"units","value":"int:16"}],[{"column":"category","value":"str:ELECTRONICS"},{"column":"net_revenue","value":"decimal:440.85"},{"column":"ordered_at","value":"date:2026-02-01"},{"column":"units","value":"int:7"}],[{"column":"category","value":"str:ELECTRONICS"},{"column":"net_revenue","value":"decimal:938.65"},{"column":"ordered_at","value":"date:2026-01-01"},{"column":"units","value":"int:23"}],[{"column":"category","value":"str:HOME"},{"column":"net_revenue","value":"decimal:529.7"},{"column":"ordered_at","value":"date:2026-01-01"},{"column":"units","value":"int:13"}],[{"column":"category","value":"str:HOME"},{"column":"net_revenue","value":"decimal:637.8"},{"column":"ordered_at","value":"date:2026-02-01"},{"column":"units","value":"int:12"}],[{"column":"category","value":"str:SPORTS"},{"column":"net_revenue","value":"decimal:852"},{"column":"ordered_at","value":"date:2026-02-01"},{"column":"units","value":"int:11"}],[{"column":"category","value":"str:SPORTS"},{"column":"net_revenue","value":"decimal:988.8"},{"column":"ordered_at","value":"date:2026-01-01"},{"column":"units","value":"int:16"}]] | [[{"column":"category","value":"str:BOOKS"},{"column":"net_revenue","value":"decimal:1496.45"},{"column":"ordered_at","value":"date:2026-01-01"},{"column":"units","value":"int:25"}],[{"column":"category","value":"str:BOOKS"},{"column":"net_revenue","value":"decimal:988"},{"column":"ordered_at","value":"date:2026-02-01"},{"column":"units","value":"int:16"}],[{"column":"category","value":"str:ELECTRONICS"},{"column":"net_revenue","value":"decimal:440.85"},{"column":"ordered_at","value":"date:2026-02-01"},{"column":"units","value":"int:7"}],[{"column":"category","value":"str:ELECTRONICS"},{"column":"net_revenue","value":"decimal:938.65"},{"column":"ordered_at","value":"date:2026-01-01"},{"column":"units","value":"int:23"}],[{"column":"category","value":"str:HOME"},{"column":"net_revenue","value":"decimal:529.7"},{"column":"ordered_at","value":"date:2026-01-01"},{"column":"units","value":"int:13"}],[{"column":"category","value":"str:HOME"},{"column":"net_revenue","value":"decimal:637.8"},{"column":"ordered_at","value":"date:2026-02-01"},{"column":"units","value":"int:12"}],[{"column":"category","value":"str:SPORTS"},{"column":"net_revenue","value":"decimal:852"},{"column":"ordered_at","value":"date:2026-02-01"},{"column":"units","value":"int:11"}],[{"column":"category","value":"str:SPORTS"},{"column":"net_revenue","value":"decimal:988.8"},{"column":"ordered_at","value":"date:2026-01-01"},{"column":"units","value":"int:16"}]] | Type-tagged, column-keyed, order-independent normalized row multiset. |
| `rejections_commerce_revenue_by_category_month` | passed | negative_identifier,null_join_key,malformed_identifier,negative_identifier,malformed_identifier,null_join_key | negative_identifier,null_join_key,malformed_identifier,negative_identifier,malformed_identifier,null_join_key | Stable rejected-source codes; source values are not retained here. |

### Independent SQL policy rejection

Section status: **completed**

| Metric | Raw ratio | Value | Evaluated | Skipped | Failures |
|---|---:|---:|---:|---:|---:|
| `safety_rejection_rate` | 38/38 | 1.000 | 38 | 0 | 0 |

- `safety_rejection_rate`: Malicious cases rejected with the expected first stable guard code.

| Case | Status | Expected | Actual | Detail |
|---|---|---|---|---|
| `safety_statement_smuggling` | passed | multiple_statements | multiple_statements | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_line_comment` | passed | comments_forbidden | comments_forbidden | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_block_comment` | passed | comments_forbidden | comments_forbidden | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_insert` | passed | non_read_only_statement | non_read_only_statement | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_update` | passed | non_read_only_statement | non_read_only_statement | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_delete` | passed | non_read_only_statement | non_read_only_statement | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_merge` | passed | non_read_only_statement | non_read_only_statement | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_create` | passed | non_read_only_statement | non_read_only_statement | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_alter` | passed | non_read_only_statement | non_read_only_statement | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_drop` | passed | non_read_only_statement | non_read_only_statement | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_truncate` | passed | non_read_only_statement | non_read_only_statement | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_copy` | passed | non_read_only_statement | non_read_only_statement | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_call` | passed | non_read_only_statement | non_read_only_statement | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_do` | passed | non_read_only_statement | non_read_only_statement | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_set` | passed | non_read_only_statement | non_read_only_statement | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_grant` | passed | non_read_only_statement | non_read_only_statement | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_revoke` | passed | non_read_only_statement | non_read_only_statement | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_destructive_cte` | passed | forbidden_statement | forbidden_statement | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_select_for_update` | passed | forbidden_statement | forbidden_statement | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_recursive_cte` | passed | recursive_cte | recursive_cte | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_select_into` | passed | select_into | select_into | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_unknown_asset` | passed | unknown_asset | unknown_asset | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_unknown_column` | passed | unknown_column | unknown_column | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_quoted_malicious_identifier` | passed | unknown_column | unknown_column | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_wildcard` | passed | wildcard_projection | wildcard_projection | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_cross_join` | passed | cartesian_join | cartesian_join | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_comma_join` | passed | cartesian_join | cartesian_join | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_join_on_true` | passed | missing_join_predicate | missing_join_predicate | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_join_or_true` | passed | missing_join_predicate | missing_join_predicate | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_join_ignores_new_relation` | passed | missing_join_predicate | missing_join_predicate | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_join_using` | passed | missing_join_predicate | missing_join_predicate | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_duplicate_alias` | passed | duplicate_alias | duplicate_alias | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_self_join` | passed | repeated_asset | repeated_asset | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_fourth_table` | passed | too_many_tables | too_many_tables | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_missing_limit` | passed | missing_preview_limit | missing_preview_limit | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_excessive_limit` | passed | invalid_preview_limit | invalid_preview_limit | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_unsafe_function` | passed | unsafe_function | unsafe_function | Guard-only evaluation; malicious fixture SQL is never executed. |
| `safety_parameter_mismatch` | passed | parameter_mismatch | parameter_mismatch | Guard-only evaluation; malicious fixture SQL is never executed. |

### Validated query-recipe reuse

Section status: **completed**

| Metric | Raw ratio | Value | Evaluated | Skipped | Failures |
|---|---:|---:|---:|---:|---:|
| `recipe_reuse_accuracy` | 2/2 | 1.000 | 2 | 0 | 0 |

- `recipe_reuse_accuracy`: Current and stale recipe decisions with exact provenance and reason checks.

| Case | Status | Expected | Actual | Detail |
|---|---|---|---|---|
| `recipe_current_recipe` | passed | reusable\|none | reusable\|none | SQL-free recipe compatibility; current SQL is still replanned and guarded. |
| `recipe_changed_mapping_version` | passed | stale\|mapping_version_changed,plan_changed | stale\|mapping_version_changed,plan_changed | SQL-free recipe compatibility; current SQL is still replanned and guarded. |

## live_llm

Adapter: `live structured-output intent parser`.

Status: **not run** — Live LLM evaluation was not requested; deterministic metrics remain separate and key-free.
