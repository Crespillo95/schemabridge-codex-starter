\set ON_ERROR_STOP on

CREATE ROLE schemabridge_reader
    LOGIN
    PASSWORD 'schemabridge_reader'
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOINHERIT
    NOREPLICATION
    NOBYPASSRLS;

ALTER ROLE schemabridge_reader SET default_transaction_read_only = on;
ALTER ROLE schemabridge_reader SET statement_timeout = '5s';
ALTER ROLE schemabridge_reader SET lock_timeout = '1s';

REVOKE ALL PRIVILEGES ON DATABASE schemabridge FROM PUBLIC;
REVOKE ALL PRIVILEGES ON SCHEMA
    public, crm, legacy, bank, reporting, commerce, sales, fulfillment, support
    FROM PUBLIC;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA
    crm, legacy, bank, reporting, commerce, sales, fulfillment, support
    FROM PUBLIC;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA
    crm, legacy, bank, reporting, commerce, sales, fulfillment, support
    FROM PUBLIC;

GRANT CONNECT ON DATABASE schemabridge TO schemabridge_reader;
GRANT USAGE ON SCHEMA
    crm, legacy, bank, reporting, commerce, sales, fulfillment, support
    TO schemabridge_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA
    crm, legacy, bank, reporting, commerce, sales, fulfillment, support
    TO schemabridge_reader;

ALTER DEFAULT PRIVILEGES IN SCHEMA
    crm, legacy, bank, reporting, commerce, sales, fulfillment, support
    REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA
    crm, legacy, bank, reporting, commerce, sales, fulfillment, support
    GRANT SELECT ON TABLES TO schemabridge_reader;
