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
