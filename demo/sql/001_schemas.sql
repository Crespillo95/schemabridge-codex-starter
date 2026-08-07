\set ON_ERROR_STOP on

ALTER DATABASE schemabridge SET timezone TO 'UTC';

CREATE SCHEMA crm;
CREATE SCHEMA legacy;
CREATE SCHEMA bank;
CREATE SCHEMA reporting;
CREATE SCHEMA commerce;
CREATE SCHEMA sales;
CREATE SCHEMA fulfillment;
CREATE SCHEMA support;

COMMENT ON SCHEMA crm IS 'Synthetic current customer source.';
COMMENT ON SCHEMA legacy IS 'Synthetic legacy customer source.';
COMMENT ON SCHEMA bank IS 'Synthetic account and holder source.';
COMMENT ON SCHEMA reporting IS 'Synthetic downstream reporting projection.';
COMMENT ON SCHEMA commerce IS 'Synthetic governed product source.';
COMMENT ON SCHEMA sales IS 'Synthetic order and order-line source with heterogeneous keys.';
COMMENT ON SCHEMA fulfillment IS 'Synthetic shipment source with deliberately inconsistent order references.';
COMMENT ON SCHEMA support IS 'Synthetic isolated support source containing non-governed homonymous fields.';
