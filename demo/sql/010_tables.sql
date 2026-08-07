\set ON_ERROR_STOP on

CREATE TABLE crm.customers (
    customer_id VARCHAR(11) PRIMARY KEY
        CONSTRAINT crm_customer_id_format CHECK (customer_id ~ '^[0-9]{11}$'),
    registration_date DATE NOT NULL,
    country_cd VARCHAR(2),
    customer_status VARCHAR(20) NOT NULL
);

COMMENT ON TABLE crm.customers IS 'CRM customer master with padded string identifiers.';
COMMENT ON COLUMN crm.customers.customer_id IS 'Customer identifier encoded as an 11-character numeric string.';
COMMENT ON COLUMN crm.customers.registration_date IS 'Date when the customer was registered in CRM.';

CREATE TABLE legacy.client_master (
    client_no BIGINT PRIMARY KEY,
    created_dt TIMESTAMP NOT NULL,
    country VARCHAR(3),
    status_code SMALLINT NOT NULL
);

COMMENT ON TABLE legacy.client_master IS 'Legacy customer representation using integer identifiers.';
COMMENT ON COLUMN legacy.client_master.client_no IS 'Legacy numeric customer identifier.';

CREATE TABLE bank.accounts (
    account_number VARCHAR(20) PRIMARY KEY,
    opening_date DATE NOT NULL,
    account_status VARCHAR(20) NOT NULL,
    current_balance NUMERIC(14, 2) NOT NULL
        CONSTRAINT bank_account_balance_nonnegative CHECK (current_balance >= 0)
);

COMMENT ON TABLE bank.accounts IS 'Bank account master.';

CREATE TABLE bank.account_holders (
    holder_link_id BIGSERIAL PRIMARY KEY,
    account_number VARCHAR(20) NOT NULL REFERENCES bank.accounts(account_number),
    gf_customer_id DOUBLE PRECISION,
    holder_type VARCHAR(20) NOT NULL,
    relationship_start_date DATE NOT NULL,
    relationship_end_date DATE,
    CONSTRAINT bank_holder_role_known
        CHECK (holder_type IN ('PRIMARY', 'SECONDARY', '2', 'CO_HOLDER')),
    CONSTRAINT bank_holder_date_order
        CHECK (relationship_end_date IS NULL OR relationship_end_date >= relationship_start_date)
);

COMMENT ON TABLE bank.account_holders IS 'Relationship between accounts and holders; legacy float customer key.';
COMMENT ON COLUMN bank.account_holders.gf_customer_id IS 'Customer identifier stored as double precision; integral values are expected.';
COMMENT ON COLUMN bank.account_holders.holder_type IS 'Holder role using heterogeneous operational codes.';

CREATE TABLE reporting.customer_accounts (
    report_date DATE NOT NULL,
    customer_key_text VARCHAR(32) NOT NULL,
    account_number VARCHAR(20) NOT NULL,
    holder_role_normalized VARCHAR(20) NOT NULL,
    PRIMARY KEY (report_date, customer_key_text, account_number)
);

COMMENT ON TABLE reporting.customer_accounts IS 'Synthetic downstream reporting table used to demonstrate lineage and normalized concepts.';
COMMENT ON COLUMN reporting.customer_accounts.customer_key_text IS 'Pre-normalized synthetic customer key for comparison only.';

CREATE TABLE commerce.products (
    product_code VARCHAR(8) PRIMARY KEY
        CONSTRAINT commerce_product_code_format CHECK (product_code ~ '^[0-9]{8}$'),
    category_code VARCHAR(12) NOT NULL
        CONSTRAINT commerce_product_category_known
        CHECK (category_code IN ('ELEC', 'HOME', 'BOOK', 'SPORT')),
    unit_price NUMERIC(10, 2) NOT NULL
        CONSTRAINT commerce_product_price_nonnegative CHECK (unit_price >= 0),
    is_active BOOLEAN NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL
);

COMMENT ON TABLE commerce.products IS 'Synthetic product master with padded text identifiers and repeated category and price values.';
COMMENT ON COLUMN commerce.products.product_code IS 'Synthetic product identifier encoded as an eight-character numeric string.';
COMMENT ON COLUMN commerce.products.category_code IS 'Governed synthetic product category code.';
COMMENT ON COLUMN commerce.products.unit_price IS 'Deterministic synthetic list price; no real commercial data.';
COMMENT ON COLUMN commerce.products.is_active IS 'Synthetic lifecycle flag.';

CREATE TABLE legacy.item_master (
    item_no BIGINT PRIMARY KEY,
    item_name VARCHAR(80) NOT NULL,
    category_cd VARCHAR(12),
    price_text VARCHAR(32),
    active_flag SMALLINT NOT NULL
        CONSTRAINT legacy_item_active_flag_known CHECK (active_flag IN (0, 1)),
    loaded_at TIMESTAMP WITHOUT TIME ZONE NOT NULL
);

COMMENT ON TABLE legacy.item_master IS 'Synthetic legacy product representation with integer keys, text prices, duplicates, and two orphans.';
COMMENT ON COLUMN legacy.item_master.item_no IS 'Synthetic legacy integer item identifier; two values intentionally lack a product match.';
COMMENT ON COLUMN legacy.item_master.item_name IS 'Synthetic label that intentionally contains duplicate values.';
COMMENT ON COLUMN legacy.item_master.price_text IS 'Heterogeneous synthetic price representation including NULL, empty, comma, and prefixed values.';

CREATE TABLE sales.orders (
    order_id BIGINT PRIMARY KEY,
    ordered_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    status_code VARCHAR(20) NOT NULL
        CONSTRAINT sales_order_status_known
        CHECK (status_code IN ('N', 'P', 'C', 'X')),
    sales_channel VARCHAR(20)
        CONSTRAINT sales_order_channel_known
        CHECK (sales_channel IS NULL OR sales_channel IN ('WEB', 'STORE', 'PARTNER')),
    region_code VARCHAR(8) NOT NULL
        CONSTRAINT sales_order_region_known CHECK (region_code IN ('N', 'S', 'E', 'W')),
    order_total NUMERIC(12, 2) NOT NULL
        CONSTRAINT sales_order_total_nonnegative CHECK (order_total >= 0)
);

COMMENT ON TABLE sales.orders IS 'Synthetic order header source spanning two months with mapped operational statuses and nullable channels.';
COMMENT ON COLUMN sales.orders.order_id IS 'Synthetic integer order identifier.';
COMMENT ON COLUMN sales.orders.ordered_at IS 'Deterministic synthetic order timestamp in January or February 2026.';
COMMENT ON COLUMN sales.orders.status_code IS 'Operational status code: N created, P paid, C completed, and X cancelled.';
COMMENT ON COLUMN sales.orders.order_total IS 'Deterministic synthetic decimal total; no real transaction data.';

CREATE TABLE sales.order_lines (
    line_id BIGINT PRIMARY KEY,
    order_ref VARCHAR(24),
    product_no BIGINT,
    quantity INTEGER NOT NULL
        CONSTRAINT sales_order_line_quantity_positive CHECK (quantity > 0),
    net_amount NUMERIC(12, 2) NOT NULL
        CONSTRAINT sales_order_line_net_nonnegative CHECK (net_amount >= 0),
    discount_amount NUMERIC(10, 2)
        CONSTRAINT sales_order_line_discount_nonnegative
        CHECK (discount_amount IS NULL OR discount_amount >= 0)
);

COMMENT ON TABLE sales.order_lines IS 'Synthetic order lines with fanout plus padded, missing, malformed, negative, and unmatched references.';
COMMENT ON COLUMN sales.order_lines.order_ref IS 'Heterogeneous text reference to integer order_id; no foreign key is declared so rejection paths remain testable.';
COMMENT ON COLUMN sales.order_lines.product_no IS 'Synthetic integer product reference including NULL, negative, and unmatched values.';
COMMENT ON COLUMN sales.order_lines.discount_amount IS 'Nullable deterministic synthetic discount amount.';

CREATE TABLE fulfillment.shipments (
    shipment_id BIGINT PRIMARY KEY,
    order_ref VARCHAR(24),
    status_code VARCHAR(20) NOT NULL
        CONSTRAINT fulfillment_shipment_status_known
        CHECK (status_code IN ('P', 'T', 'D', 'DELIVERED', 'R')),
    shipped_at TIMESTAMP WITHOUT TIME ZONE,
    delivered_at TIMESTAMP WITHOUT TIME ZONE,
    CONSTRAINT fulfillment_shipment_date_order
        CHECK (delivered_at IS NULL OR shipped_at IS NULL OR delivered_at >= shipped_at)
);

COMMENT ON TABLE fulfillment.shipments IS 'Synthetic shipment source with duplicate order relationships and adversarial order-reference formats.';
COMMENT ON COLUMN fulfillment.shipments.order_ref IS 'Synthetic order reference containing padding plus NULL, empty, negative, malformed, and orphan values.';
COMMENT ON COLUMN fulfillment.shipments.status_code IS 'Operational status code where D and DELIVERED are governed synonyms.';
COMMENT ON COLUMN fulfillment.shipments.shipped_at IS 'Nullable deterministic shipment timestamp.';
COMMENT ON COLUMN fulfillment.shipments.delivered_at IS 'Nullable deterministic delivery timestamp.';

CREATE TABLE support.order_cases (
    case_id BIGINT PRIMARY KEY,
    order_id VARCHAR(24),
    product_id VARCHAR(24),
    customer_id VARCHAR(24),
    status VARCHAR(20) NOT NULL
        CONSTRAINT support_case_status_known
        CHECK (status IN ('OPEN', 'PENDING', 'CLOSED', 'DUPLICATE')),
    priority SMALLINT NOT NULL
        CONSTRAINT support_case_priority_range CHECK (priority BETWEEN 1 AND 4),
    opened_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    closed_at TIMESTAMP WITHOUT TIME ZONE,
    case_summary VARCHAR(120) NOT NULL,
    CONSTRAINT support_case_date_order
        CHECK (closed_at IS NULL OR closed_at >= opened_at)
);

COMMENT ON TABLE support.order_cases IS 'Isolated synthetic support cases whose homonymous identifier names are not evidence of semantic equivalence.';
COMMENT ON COLUMN support.order_cases.order_id IS 'Case-local synthetic label; it is deliberately not an approved sales order key.';
COMMENT ON COLUMN support.order_cases.product_id IS 'Case-local synthetic label; it is deliberately not an approved product key.';
COMMENT ON COLUMN support.order_cases.customer_id IS 'Case-local synthetic label; it is deliberately not an approved customer key.';
COMMENT ON COLUMN support.order_cases.case_summary IS 'Repeated synthetic issue text with no person or employer information.';
