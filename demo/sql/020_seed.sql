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

INSERT INTO commerce.products (
    product_code,
    category_code,
    unit_price,
    is_active,
    created_at
)
SELECT
    LPAD(product_number::TEXT, 8, '0'),
    CASE (product_number - 1) % 4
        WHEN 0 THEN 'ELEC'
        WHEN 1 THEN 'HOME'
        WHEN 2 THEN 'BOOK'
        ELSE 'SPORT'
    END,
    (9.99 + ((product_number - 1) % 10) * 5.25)::NUMERIC(10, 2),
    product_number % 7 <> 0,
    TIMESTAMP '2025-10-01 08:00:00'
        + (product_number - 1) * INTERVAL '9 hours'
FROM GENERATE_SERIES(1, 40) AS products(product_number);

-- Forty legacy rows correspond to the padded product codes. Repeated labels,
-- heterogeneous price strings, and two orphans exercise evidence-based mapping.
INSERT INTO legacy.item_master (
    item_no,
    item_name,
    category_cd,
    price_text,
    active_flag,
    loaded_at
)
SELECT
    item_number,
    CASE
        WHEN item_number % 10 = 0 THEN 'Shared synthetic item label'
        ELSE FORMAT('Synthetic legacy item %s', item_number)
    END,
    CASE
        WHEN item_number % 13 = 0 THEN NULL
        WHEN (item_number - 1) % 4 = 0 THEN 'ELECTRONICS'
        WHEN (item_number - 1) % 4 = 1 THEN 'HOUSE'
        WHEN (item_number - 1) % 4 = 2 THEN 'BOOKS'
        ELSE 'SPORT'
    END,
    CASE
        WHEN item_number % 11 = 0 THEN NULL
        WHEN item_number % 9 = 0 THEN ''
        WHEN item_number % 7 = 0 THEN FORMAT('EUR %s.99', item_number)
        WHEN item_number % 5 = 0 THEN FORMAT('%s,50', item_number)
        ELSE FORMAT('%s.25', item_number)
    END,
    CASE WHEN item_number % 7 = 0 THEN 0 ELSE 1 END,
    TIMESTAMP '2026-02-01 06:00:00'
        + (item_number - 1) * INTERVAL '3 minutes'
FROM GENERATE_SERIES(1, 40) AS items(item_number);

INSERT INTO legacy.item_master (
    item_no,
    item_name,
    category_cd,
    price_text,
    active_flag,
    loaded_at
) VALUES
    (
        -7,
        'Synthetic negative orphan',
        NULL,
        '-7.00',
        0,
        TIMESTAMP '2026-02-01 09:00:00'
    ),
    (
        9001,
        'Synthetic unmatched orphan',
        'UNKNOWN',
        'not-a-price',
        1,
        TIMESTAMP '2026-02-01 09:03:00'
    );

INSERT INTO sales.orders (
    order_id,
    ordered_at,
    status_code,
    sales_channel,
    region_code,
    order_total
)
SELECT
    1000 + order_number,
    TIMESTAMP '2026-01-01 08:00:00'
        + (order_number - 1) * INTERVAL '18 hours',
    CASE
        WHEN order_number % 10 = 0 THEN 'X'
        WHEN order_number % 4 = 0 THEN 'P'
        WHEN order_number % 3 = 0 THEN 'C'
        ELSE 'N'
    END,
    CASE
        WHEN order_number % 11 = 0 THEN NULL
        WHEN order_number % 3 = 0 THEN 'PARTNER'
        WHEN order_number % 2 = 0 THEN 'STORE'
        ELSE 'WEB'
    END,
    CASE (order_number - 1) % 4
        WHEN 0 THEN 'N'
        WHEN 1 THEN 'S'
        WHEN 2 THEN 'E'
        ELSE 'W'
    END,
    (
        50
        + ((order_number - 1) % 12) * 37.45
        + (order_number % 3) * 0.10
    )::NUMERIC(12, 2)
FROM GENERATE_SERIES(1, 60) AS orders(order_number);

-- Every normal order has three lines. Seven rows deliberately exercise
-- whitespace, extra padding, NULL, empty, negative, malformed, and orphan keys.
INSERT INTO sales.order_lines (
    line_id,
    order_ref,
    product_no,
    quantity,
    net_amount,
    discount_amount
)
SELECT
    line_number,
    CASE line_number
        WHEN 57 THEN ' 001019 '
        WHEN 58 THEN NULL
        WHEN 118 THEN '00001040'
        WHEN 119 THEN ''
        WHEN 177 THEN '999999'
        WHEN 179 THEN '-1001'
        WHEN 180 THEN 'bad-1060'
        ELSE LPAD((1001 + ((line_number - 1) / 3))::TEXT, 6, '0')
    END,
    CASE line_number
        WHEN 55 THEN NULL
        WHEN 56 THEN -3
        WHEN 57 THEN 9999
        ELSE ((line_number * 7 - 1) % 40) + 1
    END,
    ((line_number - 1) % 5) + 1,
    (
        (((line_number - 1) % 5) + 1)
        * (8.00 + (((line_number * 7 - 1) % 40) + 1) * 2.15)
        - CASE WHEN line_number % 4 = 0 THEN (line_number % 3) * 2.50 ELSE 0 END
    )::NUMERIC(12, 2),
    CASE
        WHEN line_number % 4 = 0 THEN ((line_number % 3) * 2.50)::NUMERIC(10, 2)
        ELSE NULL
    END
FROM GENERATE_SERIES(1, 180) AS lines(line_number);

-- Rows 61-75 are second-shipment candidates. Seven contain adversarial
-- references; whitespace and extra padding normalize, while five are rejected.
INSERT INTO fulfillment.shipments (
    shipment_id,
    order_ref,
    status_code,
    shipped_at,
    delivered_at
)
SELECT
    5000 + shipment_number,
    CASE shipment_number
        WHEN 68 THEN NULL
        WHEN 69 THEN ''
        WHEN 70 THEN '-1010'
        WHEN 71 THEN 'not-an-order'
        WHEN 72 THEN '999999'
        WHEN 73 THEN ' 001013 '
        WHEN 74 THEN '00001014'
        ELSE LPAD(
            (
                1000
                + CASE
                    WHEN shipment_number <= 60 THEN shipment_number
                    ELSE shipment_number - 60
                END
            )::TEXT,
            6,
            '0'
        )
    END,
    CASE
        WHEN shipment_number % 17 = 0 THEN 'R'
        WHEN shipment_number % 11 = 0 THEN 'D'
        WHEN shipment_number % 5 = 0 THEN 'T'
        WHEN shipment_number % 3 = 0 THEN 'P'
        ELSE 'DELIVERED'
    END,
    CASE
        WHEN shipment_number % 19 = 0 THEN NULL
        ELSE
            TIMESTAMP '2026-01-03 09:00:00'
            + (shipment_number - 1) * INTERVAL '13 hours'
    END,
    CASE
        WHEN shipment_number % 17 = 0
            OR shipment_number % 5 = 0
            OR shipment_number % 3 = 0
            OR shipment_number % 23 = 0
            THEN NULL
        ELSE
            TIMESTAMP '2026-01-05 09:00:00'
            + (shipment_number - 1) * INTERVAL '13 hours'
    END
FROM GENERATE_SERIES(1, 75) AS shipments(shipment_number);

-- These names are intentionally tempting but their values are case-local
-- labels. They must never be joined through name similarity alone.
INSERT INTO support.order_cases (
    case_id,
    order_id,
    product_id,
    customer_id,
    status,
    priority,
    opened_at,
    closed_at,
    case_summary
)
SELECT
    7000 + case_number,
    CASE
        WHEN case_number = 28 THEN NULL
        WHEN case_number = 29 THEN ''
        WHEN case_number = 30 THEN '1001'
        ELSE FORMAT('CASE-ORDER-%s', LPAD(case_number::TEXT, 3, '0'))
    END,
    CASE
        WHEN case_number % 11 = 0 THEN NULL
        ELSE FORMAT('CASE-PRODUCT-%s', LPAD(((case_number - 1) % 9 + 1)::TEXT, 3, '0'))
    END,
    FORMAT('CASE-CUSTOMER-%s', LPAD(((case_number - 1) % 7 + 1)::TEXT, 3, '0')),
    CASE
        WHEN case_number % 10 = 0 THEN 'DUPLICATE'
        WHEN case_number % 4 = 0 THEN 'CLOSED'
        WHEN case_number % 3 = 0 THEN 'PENDING'
        ELSE 'OPEN'
    END,
    ((case_number - 1) % 4) + 1,
    TIMESTAMP '2026-03-01 08:00:00'
        + (case_number - 1) * INTERVAL '4 hours',
    CASE
        WHEN case_number % 4 = 0 THEN
            TIMESTAMP '2026-03-01 12:00:00'
            + (case_number - 1) * INTERVAL '4 hours'
        ELSE NULL
    END,
    CASE
        WHEN case_number % 5 = 0 THEN 'Repeated synthetic issue'
        ELSE FORMAT('Synthetic support scenario %s', case_number)
    END
FROM GENERATE_SERIES(1, 30) AS cases(case_number);
