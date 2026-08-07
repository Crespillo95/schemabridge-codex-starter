#!/bin/sh
set -eu

reader_password_file=${SCHEMABRIDGE_JUDGE_DB_READER_PASSWORD_FILE:-}

if [ ! -r "$reader_password_file" ]; then
  echo "the judge database reader secret file is unavailable" >&2
  exit 1
fi

IFS= read -r reader_password < "$reader_password_file"

case "$reader_password" in
  ''|*[!A-Za-z0-9]*)
    echo "SCHEMABRIDGE_JUDGE_DB_READER_PASSWORD must be 32-128 alphanumeric characters" >&2
    exit 1
    ;;
esac

if [ "${#reader_password}" -lt 32 ] || [ "${#reader_password}" -gt 128 ]; then
  echo "SCHEMABRIDGE_JUDGE_DB_READER_PASSWORD must be 32-128 alphanumeric characters" >&2
  exit 1
fi

psql \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set ON_ERROR_STOP=1 \
  --set reader_password="$reader_password" <<'SQL'
CREATE ROLE schemabridge_reader
    LOGIN
    PASSWORD :'reader_password'
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
SQL
