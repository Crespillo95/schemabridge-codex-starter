\set ON_ERROR_STOP on

ALTER DATABASE schemabridge SET timezone TO 'UTC';

CREATE SCHEMA crm;
CREATE SCHEMA legacy;
CREATE SCHEMA bank;
CREATE SCHEMA reporting;

COMMENT ON SCHEMA crm IS 'Synthetic current customer source.';
COMMENT ON SCHEMA legacy IS 'Synthetic legacy customer source.';
COMMENT ON SCHEMA bank IS 'Synthetic account and holder source.';
COMMENT ON SCHEMA reporting IS 'Synthetic downstream reporting projection.';
