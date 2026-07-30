DO $migration$
DECLARE
    backup_role_oid oid;
    backup_role_settings text[];
    default_read_only_setting text;
    statement_timeout_setting text;
    statement_timeout_match text[];
    statement_timeout_ms numeric;
BEGIN
    SELECT
        role.oid,
        role.rolconfig
    INTO
        backup_role_oid,
        backup_role_settings
    FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = 'schemabridge_backup'
      AND role.rolcanlogin
      AND NOT role.rolsuper
      AND NOT role.rolcreatedb
      AND NOT role.rolcreaterole
      AND NOT role.rolreplication
      AND NOT role.rolbypassrls
      AND NOT role.rolinherit;

    IF backup_role_oid IS NULL THEN
        RAISE EXCEPTION
            'control-plane backup role posture is invalid for schema v12'
            USING ERRCODE = '42501';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = backup_role_oid
          AND (
              membership.inherit_option
              OR membership.set_option
          )
    ) THEN
        RAISE EXCEPTION
            'control-plane backup role posture is invalid for schema v12'
            USING ERRCODE = '42501';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_db_role_setting AS role_setting
        CROSS JOIN LATERAL pg_catalog.pg_options_to_table(
            role_setting.setconfig
        ) AS option
        WHERE role_setting.setrole = backup_role_oid
          AND role_setting.setdatabase <> 0
          AND option.option_name IN (
              'default_transaction_read_only',
              'statement_timeout'
          )
    ) THEN
        RAISE EXCEPTION
            'control-plane backup role posture is invalid for schema v12'
            USING ERRCODE = '42501';
    END IF;

    SELECT pg_catalog.lower(option.option_value)
    INTO default_read_only_setting
    FROM pg_catalog.pg_options_to_table(
        COALESCE(backup_role_settings, ARRAY[]::text[])
    ) AS option
    WHERE option.option_name = 'default_transaction_read_only';

    SELECT pg_catalog.lower(option.option_value)
    INTO statement_timeout_setting
    FROM pg_catalog.pg_options_to_table(
        COALESCE(backup_role_settings, ARRAY[]::text[])
    ) AS option
    WHERE option.option_name = 'statement_timeout';

    statement_timeout_match := pg_catalog.regexp_match(
        COALESCE(statement_timeout_setting, ''),
        '^([1-9][0-9]*)(ms|s|min|h)?$'
    );
    IF default_read_only_setting IS DISTINCT FROM 'on'
       OR statement_timeout_match IS NULL THEN
        RAISE EXCEPTION
            'control-plane backup role posture is invalid for schema v12'
            USING ERRCODE = '42501';
    END IF;

    statement_timeout_ms := statement_timeout_match[1]::numeric * CASE
        WHEN statement_timeout_match[2] IS NULL
          OR statement_timeout_match[2] = 'ms' THEN 1
        WHEN statement_timeout_match[2] = 's' THEN 1000
        WHEN statement_timeout_match[2] = 'min' THEN 60000
        WHEN statement_timeout_match[2] = 'h' THEN 3600000
        ELSE 0
    END;
    IF statement_timeout_ms < 1
       OR statement_timeout_ms > 900000 THEN
        RAISE EXCEPTION
            'control-plane backup role posture is invalid for schema v12'
            USING ERRCODE = '42501';
    END IF;

END;
$migration$;

REVOKE CREATE ON SCHEMA schemabridge_control
    FROM schemabridge_backup;
REVOKE ALL PRIVILEGES ON SCHEMA schemabridge_control
    FROM schemabridge_backup;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA schemabridge_control
    FROM schemabridge_backup;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA schemabridge_control
    FROM schemabridge_backup;
REVOKE ALL PRIVILEGES ON ALL ROUTINES IN SCHEMA schemabridge_control
    FROM schemabridge_backup;

GRANT USAGE ON SCHEMA schemabridge_control
    TO schemabridge_backup;
GRANT SELECT ON ALL TABLES IN SCHEMA schemabridge_control
    TO schemabridge_backup;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA schemabridge_control
    TO schemabridge_backup;

ALTER DEFAULT PRIVILEGES IN SCHEMA schemabridge_control
    GRANT SELECT ON TABLES TO schemabridge_backup;
ALTER DEFAULT PRIVILEGES IN SCHEMA schemabridge_control
    GRANT SELECT ON SEQUENCES TO schemabridge_backup;
