\set ON_ERROR_STOP on

REVOKE ALL ON DATABASE schemabridge_control FROM PUBLIC;

CREATE ROLE schemabridge_migrator
  LOGIN
  NOSUPERUSER
  NOCREATEDB
  NOCREATEROLE
  NOINHERIT
  PASSWORD 'schemabridge_migrator';

CREATE ROLE schemabridge_runtime
  LOGIN
  NOSUPERUSER
  NOCREATEDB
  NOCREATEROLE
  NOINHERIT
  PASSWORD 'schemabridge_runtime';

CREATE ROLE schemabridge_reconciler
  LOGIN
  NOSUPERUSER
  NOCREATEDB
  NOCREATEROLE
  NOINHERIT
  PASSWORD 'schemabridge_reconciler';

CREATE ROLE schemabridge_api
  LOGIN
  NOSUPERUSER
  NOCREATEDB
  NOCREATEROLE
  NOINHERIT
  PASSWORD 'schemabridge_api';

CREATE ROLE schemabridge_worker
  LOGIN
  NOSUPERUSER
  NOCREATEDB
  NOCREATEROLE
  NOINHERIT
  PASSWORD 'schemabridge_worker';

CREATE ROLE schemabridge_catalog
  LOGIN
  NOSUPERUSER
  NOCREATEDB
  NOCREATEROLE
  NOINHERIT
  PASSWORD 'schemabridge_catalog';

CREATE ROLE schemabridge_observer
  LOGIN
  NOSUPERUSER
  NOCREATEDB
  NOCREATEROLE
  NOINHERIT
  PASSWORD 'schemabridge_observer';

GRANT schemabridge_observer TO schemabridge_migrator
  WITH ADMIN FALSE, INHERIT FALSE, SET TRUE;

ALTER DATABASE schemabridge_control OWNER TO schemabridge_migrator;

GRANT CONNECT ON DATABASE schemabridge_control
  TO schemabridge_migrator,
     schemabridge_runtime,
     schemabridge_reconciler,
     schemabridge_api,
     schemabridge_worker,
     schemabridge_catalog,
     schemabridge_observer;
