DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_roles
        WHERE rolname = 'schemabridge_observer'
    ) THEN
        RAISE EXCEPTION
            'operational observer role must exist before schema v10'
            USING ERRCODE = '42704';
    END IF;
END;
$migration$;

SET LOCAL ROLE schemabridge_observer;
ALTER ROLE schemabridge_observer
    SET default_transaction_read_only = on;
ALTER ROLE schemabridge_observer
    SET statement_timeout = '5s';
RESET ROLE;

CREATE VIEW schemabridge_control.operational_queue_snapshot (
    queue,
    depth,
    oldest_due_age_seconds
)
WITH (
    security_barrier = true,
    security_invoker = false
)
AS
SELECT
    'execution'::text AS queue,
    pg_catalog.count(*)::bigint AS depth,
    COALESCE(
        GREATEST(
            0::numeric,
            pg_catalog.floor(
                EXTRACT(
                    EPOCH FROM (
                        pg_catalog.statement_timestamp()
                        - pg_catalog.min(available_at) FILTER (
                            WHERE available_at
                                <= pg_catalog.statement_timestamp()
                        )
                    )
                )
            )
        )::bigint,
        0::bigint
    ) AS oldest_due_age_seconds
FROM schemabridge_control.execution_jobs
WHERE status IN ('queued', 'retry_wait')

UNION ALL

SELECT
    'catalog'::text AS queue,
    pg_catalog.count(*)::bigint AS depth,
    COALESCE(
        GREATEST(
            0::numeric,
            pg_catalog.floor(
                EXTRACT(
                    EPOCH FROM (
                        pg_catalog.statement_timestamp()
                        - pg_catalog.min(requested_at) FILTER (
                            WHERE requested_at
                                <= pg_catalog.statement_timestamp()
                        )
                    )
                )
            )
        )::bigint,
        0::bigint
    ) AS oldest_due_age_seconds
FROM schemabridge_control.catalog_refresh_runs
WHERE status = 'requested'

UNION ALL

SELECT
    'profile'::text AS queue,
    pg_catalog.count(*)::bigint AS depth,
    COALESCE(
        GREATEST(
            0::numeric,
            pg_catalog.floor(
                EXTRACT(
                    EPOCH FROM (
                        pg_catalog.statement_timestamp()
                        - pg_catalog.min(available_at) FILTER (
                            WHERE available_at
                                <= pg_catalog.statement_timestamp()
                        )
                    )
                )
            )
        )::bigint,
        0::bigint
    ) AS oldest_due_age_seconds
FROM schemabridge_control.semantic_join_profile_jobs
WHERE status IN ('requested', 'retry_wait')

UNION ALL

SELECT
    'reconciliation'::text AS queue,
    pg_catalog.count(*)::bigint AS depth,
    COALESCE(
        GREATEST(
            0::numeric,
            pg_catalog.floor(
                EXTRACT(
                    EPOCH FROM (
                        pg_catalog.statement_timestamp()
                        - pg_catalog.min(available_at) FILTER (
                            WHERE available_at
                                <= pg_catalog.statement_timestamp()
                        )
                    )
                )
            )
        )::bigint,
        0::bigint
    ) AS oldest_due_age_seconds
FROM schemabridge_control.semantic_change_scan_requests
WHERE status IN ('requested', 'retry_wait');

ALTER VIEW schemabridge_control.operational_queue_snapshot
    OWNER TO schemabridge_migrator;
REVOKE ALL PRIVILEGES
    ON schemabridge_control.operational_queue_snapshot
    FROM PUBLIC;

REVOKE CREATE ON SCHEMA schemabridge_control
    FROM schemabridge_observer;
REVOKE ALL PRIVILEGES ON SCHEMA schemabridge_control
    FROM schemabridge_observer;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA schemabridge_control
    FROM schemabridge_observer;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA schemabridge_control
    FROM schemabridge_observer;
REVOKE ALL PRIVILEGES ON ALL ROUTINES IN SCHEMA schemabridge_control
    FROM schemabridge_observer;

GRANT USAGE ON SCHEMA schemabridge_control
    TO schemabridge_observer;
GRANT SELECT ON
    schemabridge_control.schema_migrations,
    schemabridge_control.operational_queue_snapshot
    TO schemabridge_observer;
