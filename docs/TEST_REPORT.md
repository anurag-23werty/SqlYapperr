# Test Report: Phase 1

**Result: 156 passed, 0 failed, 0 skipped** (1 third-party deprecation warning from Starlette's TestClient).

| Suite | Tests | Passed |
|---|---|---|
| `test_ast_validator.py`: accepted / adversarial SQL, forgery, immutability | 67 | 67 |
| `test_query_limits.py`: LIMIT/OFFSET bypass regressions, row & size caps | 35 | 35 |
| `test_authorizer.py`: raw SQL against the SQLite authorizer only | 27 | 27 |
| `test_chat_pipeline.py`: correction loop, sanitization, HTTP API | 21 | 21 |
| `test_execution.py`: structured results, timeout, error leakage | 6 | 6 |

## Environment

- Python 3.13.16, SQLite 3.45.1
- sqlglot 25.34.1, FastAPI 0.143.4, Starlette 1.6.0, pydantic 2.13.5, pytest 9.1.1
- Command: `pytest -v --junitxml=reports/junit.xml`

## Mutation check (do the tests actually guard the controls?)

Each control was deliberately disabled, the suite re-run, then the code restored:

| Mutation | Result |
|---|---|
| Authorizer allows every column of an allowed table | 6 tests fail |
| `LIMIT > max_rows` check disabled | 3 tests fail |
| Validator table allowlist disabled | 4 tests fail (column checks still reject the rest: defense in depth) |

## Required coverage → tests

| Requirement | Test(s) |
|---|---|
| Valid SELECT / aggregation & joins / CTE & subquery | `test_valid_queries_are_accepted[*]` |
| Unauthorized table | `[unauthorized_table]`, `[unauthorized_table_in_subquery]`, `[unauthorized_table_in_cte]`, `[sqlite_master]` |
| Unauthorized column | `[unauthorized_column_*]`, `[alias_shadows_hidden_column]`, `[column_through_derived_table]` |
| `SELECT *` | `[select_star]`, `[qualified_star]`, `[star_in_subquery]` |
| UNION exfiltration | `[union_unauthorized_table]`, `[union_unauthorized_column]` |
| Multiple statements | `[multiple_statements]`, `[stacked_selects]` |
| Writes and DDL | `[insert]`…`[pragma]`, `test_attach_and_control_statements_rejected` |
| Dangerous functions | `[load_extension]`, `[randomblob_dos]`, `[sqlite_version]`, `[hex_function]` |
| Negative / missing / excessive LIMIT | `test_bad_limits_rejected[*]`, `test_missing_limit_gets_default` |
| Deep nesting | `[deep_nesting]`, `test_deeply_nested_parentheses_do_not_crash` |
| Malformed SQL | `[malformed]`, `[unbalanced]`, `[empty]`, `[nul_byte]` |
| SQLite authorizer denial | `test_unauthorized_access_denied_at_db_level[*]`, `test_writes_ddl_pragma_attach_denied[*]` |
| Query timeout | `test_query_timeout`, `test_timeout_stops_loop_without_retry` |
| Correction-loop exhaustion | `test_correction_loop_exhaustion`, `test_budget_cannot_exceed_two_corrections` |
| Sensitive error leakage | `test_error_messages_do_not_leak`, `test_feedback_to_model_is_sanitized`, `test_error_responses_do_not_leak` |

## Full verbose output

```
app/tests/test_ast_validator.py::test_valid_queries_are_accepted[simple_select] PASSED
app/tests/test_ast_validator.py::test_valid_queries_are_accepted[aggregation_join] PASSED
app/tests/test_ast_validator.py::test_valid_queries_are_accepted[three_way_join] PASSED
app/tests/test_ast_validator.py::test_valid_queries_are_accepted[cte] PASSED
app/tests/test_ast_validator.py::test_valid_queries_are_accepted[in_subquery] PASSED
app/tests/test_ast_validator.py::test_valid_queries_are_accepted[correlated_exists] PASSED
app/tests/test_ast_validator.py::test_valid_queries_are_accepted[derived_table] PASSED
app/tests/test_ast_validator.py::test_valid_queries_are_accepted[count_star] PASSED
app/tests/test_ast_validator.py::test_valid_queries_are_accepted[strftime_alias_group] PASSED
app/tests/test_ast_validator.py::test_valid_queries_are_accepted[case_expression] PASSED
app/tests/test_ast_validator.py::test_valid_queries_are_accepted[union_authorized] PASSED
app/tests/test_ast_validator.py::test_valid_queries_are_accepted[like] PASSED
app/tests/test_ast_validator.py::test_executable_sql_is_reserialized_not_original PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[unauthorized_table] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[sqlite_master] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[schema_qualified] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[table_valued_function] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[cte_shadows_table] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[unauthorized_table_in_subquery] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[unauthorized_table_in_cte] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[unauthorized_column_qualified] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[unauthorized_column_unqualified] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[unauthorized_column_in_where] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[unauthorized_column_in_order] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[alias_shadows_hidden_column] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[column_through_derived_table] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[unknown_alias] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[ambiguous_column] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[select_star] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[qualified_star] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[star_in_subquery] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[union_unauthorized_table] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[union_unauthorized_column] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[multiple_statements] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[stacked_selects] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[insert] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[update] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[delete] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[drop] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[alter] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[create] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[pragma] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[select_into] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[placeholder] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[natural_join] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[load_extension] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[randomblob_dos] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[sqlite_version] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[hex_function] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[unapproved_in_where] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[recursive_cte] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[deep_nesting] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[malformed] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[unbalanced] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[empty] PASSED
app/tests/test_ast_validator.py::test_adversarial_queries_are_rejected[nul_byte] PASSED
app/tests/test_ast_validator.py::test_attach_and_control_statements_rejected[ATTACH DATABASE '/tmp/x.db' AS x] PASSED
app/tests/test_ast_validator.py::test_attach_and_control_statements_rejected[DETACH DATABASE x] PASSED
app/tests/test_ast_validator.py::test_attach_and_control_statements_rejected[VACUUM] PASSED
app/tests/test_ast_validator.py::test_attach_and_control_statements_rejected[BEGIN] PASSED
app/tests/test_ast_validator.py::test_attach_and_control_statements_rejected[COMMIT] PASSED
app/tests/test_ast_validator.py::test_deeply_nested_parentheses_do_not_crash PASSED
app/tests/test_ast_validator.py::test_too_many_joins PASSED
app/tests/test_ast_validator.py::test_too_long_query PASSED
app/tests/test_ast_validator.py::test_validated_query_cannot_be_forged PASSED
app/tests/test_ast_validator.py::test_policy_is_immutable PASSED
app/tests/test_ast_validator.py::test_issue_messages_do_not_leak_existence PASSED
app/tests/test_authorizer.py::test_authorized_reads_allowed[SELECT c.name, c.city FROM customers c LIMIT 3] PASSED
app/tests/test_authorizer.py::test_authorized_reads_allowed[SELECT COUNT(*) FROM orders] PASSED
app/tests/test_authorizer.py::test_authorized_reads_allowed[SELECT o.order_id FROM orders o WHERE o.customer_id IN (SELECT c.customer_id FROM customers c) LIMIT 1] PASSED
app/tests/test_authorizer.py::test_authorized_reads_allowed[SELECT ROUND(SUM(total_amount), 2), strftime('%Y', MAX(order_date)) FROM orders] PASSED
app/tests/test_authorizer.py::test_authorized_reads_allowed[SELECT c.name FROM customers c WHERE c.name LIKE 'A%' LIMIT 1] PASSED
app/tests/test_authorizer.py::test_unauthorized_access_denied_at_db_level[SELECT email FROM customers-denied0] PASSED
app/tests/test_authorizer.py::test_unauthorized_access_denied_at_db_level[SELECT username FROM admin_users-denied1] PASSED
app/tests/test_authorizer.py::test_unauthorized_access_denied_at_db_level[SELECT COUNT(*) FROM admin_users-denied2] PASSED
app/tests/test_authorizer.py::test_unauthorized_access_denied_at_db_level[SELECT c.name AS email FROM customers c WHERE email = 'x'-denied3] PASSED
app/tests/test_authorizer.py::test_unauthorized_access_denied_at_db_level[SELECT name FROM customers UNION SELECT password_hash FROM admin_users-denied4] PASSED
app/tests/test_authorizer.py::test_unauthorized_access_denied_at_db_level[SELECT name FROM sqlite_schema-denied5] PASSED
app/tests/test_authorizer.py::test_unauthorized_access_denied_at_db_level[SELECT hex(name) FROM customers-denied6] PASSED
app/tests/test_authorizer.py::test_unauthorized_access_denied_at_db_level[SELECT load_extension('x')-denied7] PASSED
app/tests/test_authorizer.py::test_writes_ddl_pragma_attach_denied[PRAGMA table_info(customers)] PASSED
app/tests/test_authorizer.py::test_writes_ddl_pragma_attach_denied[ATTACH DATABASE ':memory:' AS x] PASSED
app/tests/test_authorizer.py::test_writes_ddl_pragma_attach_denied[DELETE FROM orders] PASSED
app/tests/test_authorizer.py::test_writes_ddl_pragma_attach_denied[INSERT INTO products (product_name, category, price) VALUES ('x','y',1)] PASSED
app/tests/test_authorizer.py::test_writes_ddl_pragma_attach_denied[UPDATE orders SET status = 'completed'] PASSED
app/tests/test_authorizer.py::test_writes_ddl_pragma_attach_denied[DROP TABLE orders] PASSED
app/tests/test_authorizer.py::test_writes_ddl_pragma_attach_denied[CREATE TEMP TABLE t (x INT)] PASSED
app/tests/test_authorizer.py::test_writes_ddl_pragma_attach_denied[BEGIN] PASSED
app/tests/test_authorizer.py::test_writes_ddl_pragma_attach_denied[WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r LIMIT 3) SELECT n FROM r] PASSED
app/tests/test_authorizer.py::test_authorizer_never_returns_ignore PASSED
app/tests/test_authorizer.py::test_authorizer_denies_other_databases PASSED
app/tests/test_authorizer.py::test_authorizer_fails_closed_on_garbage_input PASSED
app/tests/test_authorizer.py::test_connections_do_not_share_authorizer_state PASSED
app/tests/test_authorizer.py::test_readonly_connection_rejects_writes_without_authorizer PASSED
app/tests/test_chat_pipeline.py::test_demo_questions_produce_results[What are the top 10 customers by revenue?] PASSED
app/tests/test_chat_pipeline.py::test_demo_questions_produce_results[Show monthly sales for 2025.] PASSED
app/tests/test_chat_pipeline.py::test_demo_questions_produce_results[Which products have the highest return rate?] PASSED
app/tests/test_chat_pipeline.py::test_monthly_sales_has_12_months PASSED
app/tests/test_chat_pipeline.py::test_correction_succeeds_after_rejection PASSED
app/tests/test_chat_pipeline.py::test_correction_loop_exhaustion PASSED
app/tests/test_chat_pipeline.py::test_budget_cannot_exceed_two_corrections PASSED
app/tests/test_chat_pipeline.py::test_rejected_query_is_never_executed PASSED
app/tests/test_chat_pipeline.py::test_generation_failures_count_against_budget PASSED
app/tests/test_chat_pipeline.py::test_timeout_stops_loop_without_retry PASSED
app/tests/test_chat_pipeline.py::test_feedback_to_model_is_sanitized PASSED
app/tests/test_chat_pipeline.py::test_question_is_sanitized PASSED
app/tests/test_chat_pipeline.py::test_parse_candidate_rejects_bad_output PASSED
app/tests/test_chat_pipeline.py::test_health PASSED
app/tests/test_chat_pipeline.py::test_schema_exposes_only_authorized_metadata PASSED
app/tests/test_chat_pipeline.py::test_chat_endpoint PASSED
app/tests/test_chat_pipeline.py::test_validate_endpoint_never_executes PASSED
app/tests/test_chat_pipeline.py::test_request_size_limit PASSED
app/tests/test_chat_pipeline.py::test_input_validation PASSED
app/tests/test_chat_pipeline.py::test_error_responses_do_not_leak PASSED
app/tests/test_chat_pipeline.py::test_unauthorized_requests_via_chat_are_rejected PASSED
app/tests/test_execution.py::test_structured_result PASSED
app/tests/test_execution.py::test_executor_refuses_raw_strings PASSED
app/tests/test_execution.py::test_query_timeout PASSED
app/tests/test_execution.py::test_timeout_is_independent_per_request PASSED
app/tests/test_execution.py::test_error_messages_do_not_leak PASSED
app/tests/test_execution.py::test_blob_values_are_json_safe PASSED
app/tests/test_query_limits.py::test_bad_limits_rejected[negative_one] PASSED
app/tests/test_query_limits.py::test_bad_limits_rejected[negative_large] PASSED
app/tests/test_query_limits.py::test_bad_limits_rejected[negative_parenthesized] PASSED
app/tests/test_query_limits.py::test_bad_limits_rejected[double_negative] PASSED
app/tests/test_query_limits.py::test_bad_limits_rejected[expression] PASSED
app/tests/test_query_limits.py::test_bad_limits_rejected[expression_to_negative] PASSED
app/tests/test_query_limits.py::test_bad_limits_rejected[multiplication] PASSED
app/tests/test_query_limits.py::test_bad_limits_rejected[subquery] PASSED
app/tests/test_query_limits.py::test_bad_limits_rejected[cast] PASSED
app/tests/test_query_limits.py::test_bad_limits_rejected[string] PASSED
app/tests/test_query_limits.py::test_bad_limits_rejected[float] PASSED
app/tests/test_query_limits.py::test_bad_limits_rejected[scientific] PASSED
app/tests/test_query_limits.py::test_bad_limits_rejected[hex] PASSED
app/tests/test_query_limits.py::test_bad_limits_rejected[int64_overflow] PASSED
app/tests/test_query_limits.py::test_bad_limits_rejected[huge_overflow] PASSED
app/tests/test_query_limits.py::test_bad_limits_rejected[too_large] PASSED
app/tests/test_query_limits.py::test_bad_limits_rejected[way_too_large] PASSED
app/tests/test_query_limits.py::test_bad_limits_rejected[negative_offset] PASSED
app/tests/test_query_limits.py::test_bad_limits_rejected[offset_expression] PASSED
app/tests/test_query_limits.py::test_bad_limits_rejected[offset_too_large] PASSED
app/tests/test_query_limits.py::test_bad_limits_rejected[comma_form_negative_offset] PASSED
app/tests/test_query_limits.py::test_bad_limits_rejected[comma_form_negative_count] PASSED
app/tests/test_query_limits.py::test_bad_limits_rejected[comma_form_too_large] PASSED
app/tests/test_query_limits.py::test_bad_limits_rejected[negative_in_subquery] PASSED
app/tests/test_query_limits.py::test_bad_limits_rejected[negative_in_cte] PASSED
app/tests/test_query_limits.py::test_bad_limits_rejected[negative_on_union] PASSED
app/tests/test_query_limits.py::test_missing_limit_gets_default PASSED
app/tests/test_query_limits.py::test_missing_limit_on_union_gets_default PASSED
app/tests/test_query_limits.py::test_limits_within_bounds_accepted[0] PASSED
app/tests/test_query_limits.py::test_limits_within_bounds_accepted[1] PASSED
app/tests/test_query_limits.py::test_limits_within_bounds_accepted[50] PASSED
app/tests/test_query_limits.py::test_limits_within_bounds_accepted[100] PASSED
app/tests/test_query_limits.py::test_inner_limit_does_not_make_outer_query_unbounded PASSED
app/tests/test_query_limits.py::test_executor_caps_rows_even_if_limit_is_larger PASSED
app/tests/test_query_limits.py::test_response_size_cap PASSED
======================== 156 passed, 1 warning in 1.02s ========================
```
