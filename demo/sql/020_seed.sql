\set ON_ERROR_STOP on

INSERT INTO crm.customers (customer_id, registration_date, country_cd, customer_status) VALUES
    ('00000000123', DATE '2026-01-01', 'ES', 'ACTIVE'),
    ('00000000124', DATE '2026-01-01', 'ES', 'ACTIVE'),
    ('00000000125', DATE '2026-01-02', 'PT', 'ACTIVE'),
    ('00000000126', DATE '2026-01-02', 'ES', 'ACTIVE'),
    ('00000000127', DATE '2026-01-03', 'FR', 'ACTIVE'),
    ('00000000128', DATE '2026-01-03', 'ES', 'ACTIVE'),
    ('00000000129', DATE '2026-01-04', 'ES', 'INACTIVE');

INSERT INTO legacy.client_master (client_no, created_dt, country, status_code) VALUES
    (123, TIMESTAMP '2026-01-01 09:00:00', 'ESP', 1),
    (124, TIMESTAMP '2026-01-01 10:00:00', 'ESP', 1),
    (125, TIMESTAMP '2026-01-02 11:00:00', 'PRT', 1),
    (126, TIMESTAMP '2026-01-02 12:00:00', 'ESP', 1),
    (127, TIMESTAMP '2026-01-03 13:00:00', 'FRA', 1),
    (128, TIMESTAMP '2026-01-03 14:00:00', 'ESP', 1),
    (129, TIMESTAMP '2026-01-04 15:00:00', 'ESP', 0);

INSERT INTO bank.accounts (account_number, opening_date, account_status, current_balance) VALUES
    ('ACC-001', DATE '2025-01-10', 'OPEN', 1200.00),
    ('ACC-002', DATE '2025-02-11', 'OPEN', 800.00),
    ('ACC-003', DATE '2025-03-12', 'OPEN', 450.00),
    ('ACC-004', DATE '2025-04-13', 'OPEN', 920.00),
    ('ACC-005', DATE '2025-05-14', 'OPEN', 200.00),
    ('ACC-006', DATE '2025-06-15', 'OPEN', 300.00),
    ('ACC-007', DATE '2025-07-16', 'OPEN', 700.00),
    ('ACC-008', DATE '2025-08-17', 'OPEN', 100.00),
    ('ACC-009', DATE '2025-09-18', 'OPEN', 150.00);

-- Customer 123 appears twice as a secondary holder. Correct customer metrics
-- therefore need COUNT DISTINCT after the one-to-many join.
INSERT INTO bank.account_holders (
    account_number,
    gf_customer_id,
    holder_type,
    relationship_start_date,
    relationship_end_date
) VALUES
    ('ACC-001', 123.0, 'SECONDARY', DATE '2025-01-10', NULL),
    ('ACC-002', 123.0, 'SECONDARY', DATE '2025-02-11', NULL),
    ('ACC-003', 124.0, '2', DATE '2025-03-12', NULL),
    ('ACC-004', 125.0, 'CO_HOLDER', DATE '2025-04-13', NULL),
    ('ACC-005', 126.0, 'PRIMARY', DATE '2025-05-14', NULL),
    ('ACC-006', 128.0, 'SECONDARY', DATE '2025-06-15', NULL),
    ('ACC-007', 127.5, 'SECONDARY', DATE '2025-07-16', NULL), -- non-integral
    ('ACC-008', 'NaN'::DOUBLE PRECISION, 'SECONDARY', DATE '2025-08-17', NULL), -- non-finite
    ('ACC-009', NULL, 'SECONDARY', DATE '2025-09-18', NULL); -- missing key

INSERT INTO reporting.customer_accounts (
    report_date,
    customer_key_text,
    account_number,
    holder_role_normalized
) VALUES
    (DATE '2026-01-05', '123', 'ACC-001', 'SECONDARY'),
    (DATE '2026-01-05', '123', 'ACC-002', 'SECONDARY'),
    (DATE '2026-01-05', '124', 'ACC-003', 'SECONDARY'),
    (DATE '2026-01-05', '125', 'ACC-004', 'SECONDARY'),
    (DATE '2026-01-05', '126', 'ACC-005', 'PRIMARY'),
    (DATE '2026-01-05', '128', 'ACC-006', 'SECONDARY');
