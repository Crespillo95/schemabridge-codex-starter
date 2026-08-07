LOCK TABLE
    schemabridge_control.execution_jobs,
    schemabridge_control.semantic_join_profile_jobs,
    schemabridge_control.catalog_refresh_runs
IN SHARE ROW EXCLUSIVE MODE;

DO $migration$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM schemabridge_control.execution_jobs AS job
        WHERE job.status IN (
            'queued',
            'leased',
            'cancel_requested',
            'retry_wait'
        )
    ) THEN
        RAISE EXCEPTION
            'non-terminal legacy execution jobs must be drained before schema v9'
            USING ERRCODE = '55000';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM schemabridge_control.semantic_join_profile_jobs AS job
        WHERE job.status IN ('requested', 'leased', 'retry_wait')
    ) THEN
        RAISE EXCEPTION
            'non-terminal legacy profile jobs must be drained before schema v9'
            USING ERRCODE = '55000';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM schemabridge_control.catalog_refresh_runs AS refresh
        WHERE refresh.status IN ('requested', 'leased', 'staging')
    ) THEN
        RAISE EXCEPTION
            'non-terminal legacy catalog refreshes must be drained before schema v9'
            USING ERRCODE = '55000';
    END IF;
END;
$migration$;

CREATE OR REPLACE FUNCTION
    schemabridge_control.resolve_query_studio_physical_type(
        p_approved_type varchar,
        p_native_type varchar
    )
RETURNS varchar(32)
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY INVOKER
SET search_path = pg_catalog
AS $$
    WITH bounded AS (
        SELECT CASE
            WHEN p_native_type IS NOT NULL
             AND length(p_native_type) BETWEEN 1 AND 200
             AND octet_length(p_native_type) <= 400
             AND btrim(p_native_type) = p_native_type
             AND p_native_type !~ '[[:cntrl:]]'
                THEN lower(
                    regexp_replace(
                        p_native_type,
                        '[[:space:]]+',
                        ' ',
                        'g'
                    )
                )
            ELSE NULL
        END AS native_type
    ),
    unqualified AS (
        SELECT CASE
            WHEN native_type LIKE 'pg_catalog.%'
                THEN substring(native_type FROM 12)
            ELSE native_type
        END AS native_type
        FROM bounded
    ),
    scalar_candidate AS (
        SELECT
            native_type,
            btrim(
                regexp_replace(
                    regexp_replace(
                        native_type,
                        '[[:space:]]*\([[:space:]]*[0-9]+'
                            || '([[:space:]]*,[[:space:]]*[0-9]+)?'
                            || '[[:space:]]*\)[[:space:]]*',
                        ' '
                    ),
                    '[[:space:]]+',
                    ' ',
                    'g'
                )
            ) AS scalar_type
        FROM unqualified
    ),
    classified AS (
        SELECT CASE
            WHEN native_type IS NULL THEN 'unknown'
            WHEN native_type = 'array'
              OR native_type ~ '([[:space:]]*\[[[:space:]]*\])+$'
              OR (
                    left(native_type, 1) = '_'
                    AND substring(native_type FROM 2) IN (
                        'bigint', 'bigserial', 'bit', 'bit varying', 'bool',
                        'boolean', 'bpchar', 'bytea', 'char', 'character',
                        'character varying', 'citext', 'date', 'decimal',
                        'double precision', 'float', 'float4', 'float8',
                        'hstore', 'int', 'int2', 'int4', 'int8', 'integer',
                        'json', 'jsonb', 'money', 'name', 'numeric', 'real',
                        'serial', 'serial2', 'serial4', 'serial8', 'smallint',
                        'smallserial', 'text', 'timestamp',
                        'timestamp with time zone',
                        'timestamp without time zone', 'timestamptz', 'uuid',
                        'varbit', 'varchar'
                    )
                )
                THEN 'array'
            WHEN scalar_type IN (
                'bpchar', 'char', 'character', 'character varying', 'citext',
                'name', 'text', 'uuid', 'varchar'
            )
                THEN 'string'
            WHEN scalar_type IN (
                'bigint', 'bigserial', 'int', 'int2', 'int4', 'int8',
                'integer', 'serial', 'serial2', 'serial4', 'serial8',
                'smallint', 'smallserial'
            )
                THEN 'integer'
            WHEN scalar_type IN ('decimal', 'money', 'numeric')
                THEN 'decimal'
            WHEN scalar_type IN (
                'double precision', 'float', 'float4', 'float8', 'real'
            )
                THEN 'float'
            WHEN scalar_type IN ('bool', 'boolean')
                THEN 'boolean'
            WHEN scalar_type = 'date'
                THEN 'date'
            WHEN scalar_type IN (
                'timestamp', 'timestamp with time zone',
                'timestamp without time zone', 'timestamptz'
            )
                THEN 'timestamp'
            WHEN scalar_type IN ('bit', 'bit varying', 'bytea', 'varbit')
                THEN 'binary'
            WHEN scalar_type IN ('hstore', 'json', 'jsonb')
                THEN 'struct'
            ELSE 'unknown'
        END::varchar(32) AS normalized_type
        FROM scalar_candidate
    )
    SELECT CASE
        WHEN classified.normalized_type IN (
            'binary',
            'struct',
            'array',
            'unknown'
        )
            THEN NULL
        WHEN p_approved_type IN ('binary', 'struct', 'array', 'unknown')
            THEN NULL
        WHEN p_approved_type IN (
            'string',
            'integer',
            'decimal',
            'float',
            'boolean',
            'date',
            'timestamp'
        )
            THEN p_approved_type
        WHEN classified.normalized_type IN (
            'string',
            'integer',
            'decimal',
            'float',
            'boolean',
            'date',
            'timestamp'
        )
            THEN classified.normalized_type
        ELSE NULL
    END::varchar(32)
    FROM classified;
$$;

CREATE FUNCTION schemabridge_control.connector_canonical_cost(
    p_value numeric
)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
PARALLEL SAFE
SECURITY INVOKER
SET search_path = pg_catalog
AS $$
    SELECT CASE
        WHEN p_value = 0 THEN '0'
        WHEN position('.' IN p_value::text) = 0 THEN p_value::text
        ELSE trim(
            trailing '.'
            FROM trim(trailing '0' FROM p_value::text)
        )
    END;
$$;

CREATE FUNCTION schemabridge_control.connector_cost_budget_fingerprint(
    p_explain_timeout_ms integer,
    p_max_response_bytes integer,
    p_max_total_cost numeric,
    p_max_estimated_rows bigint,
    p_max_plan_nodes integer,
    p_max_plan_depth integer,
    p_max_plan_width integer
)
RETURNS char(64)
LANGUAGE sql
IMMUTABLE
STRICT
PARALLEL SAFE
SECURITY INVOKER
SET search_path = pg_catalog, schemabridge_control
AS $$
    SELECT encode(
        sha256(
            convert_to(
                '{"explain_timeout_ms":'
                || p_explain_timeout_ms::text
                || ',"fingerprint_version":"m28-query-cost-budget-v1"'
                || ',"max_estimated_rows":'
                || p_max_estimated_rows::text
                || ',"max_plan_depth":'
                || p_max_plan_depth::text
                || ',"max_plan_nodes":'
                || p_max_plan_nodes::text
                || ',"max_plan_width":'
                || p_max_plan_width::text
                || ',"max_response_bytes":'
                || p_max_response_bytes::text
                || ',"max_total_cost":"'
                || schemabridge_control.connector_canonical_cost(
                    p_max_total_cost
                )
                || '","version":1}',
                'UTF8'
            )
        ),
        'hex'
    )::char(64);
$$;

CREATE FUNCTION schemabridge_control.connector_target_fingerprint(
    p_workspace_id varchar,
    p_connection_id varchar,
    p_route_revision bigint,
    p_route_fingerprint char,
    p_expected_reader varchar,
    p_type_contract_fingerprint char,
    p_source_identity_fingerprint char,
    p_catalog_identity_fingerprint char,
    p_explain_timeout_ms integer,
    p_max_response_bytes integer,
    p_max_total_cost numeric,
    p_max_estimated_rows bigint,
    p_max_plan_nodes integer,
    p_max_plan_depth integer,
    p_max_plan_width integer,
    p_cost_budget_fingerprint char
)
RETURNS char(64)
LANGUAGE sql
IMMUTABLE
STRICT
PARALLEL SAFE
SECURITY INVOKER
SET search_path = pg_catalog, schemabridge_control
AS $$
    SELECT encode(
        sha256(
            convert_to(
                '{"catalog_identity_fingerprint":"'
                || p_catalog_identity_fingerprint || '"'
                || ',"connection_id":"' || p_connection_id || '"'
                || ',"connector_kind":"postgresql"'
                || ',"cost_budget":{"explain_timeout_ms":'
                || p_explain_timeout_ms::text
                || ',"max_estimated_rows":'
                || p_max_estimated_rows::text
                || ',"max_plan_depth":'
                || p_max_plan_depth::text
                || ',"max_plan_nodes":'
                || p_max_plan_nodes::text
                || ',"max_plan_width":'
                || p_max_plan_width::text
                || ',"max_response_bytes":'
                || p_max_response_bytes::text
                || ',"max_total_cost":"'
                || schemabridge_control.connector_canonical_cost(
                    p_max_total_cost
                )
                || '","version":1}'
                || ',"cost_budget_fingerprint":"'
                || p_cost_budget_fingerprint || '"'
                || ',"dialect":"postgresql"'
                || ',"expected_reader":"' || p_expected_reader || '"'
                || ',"fingerprint_version":'
                || '"m28-governed-execution-target-v1"'
                || ',"route_fingerprint":"' || p_route_fingerprint || '"'
                || ',"route_revision":' || p_route_revision::text
                || ',"source_identity_fingerprint":"'
                || p_source_identity_fingerprint || '"'
                || ',"type_contract_fingerprint":"'
                || p_type_contract_fingerprint || '"'
                || ',"version":1'
                || ',"workspace_id":"' || p_workspace_id || '"}',
                'UTF8'
            )
        ),
        'hex'
    )::char(64);
$$;

CREATE TABLE schemabridge_control.connector_contract_revisions (
    workspace_id varchar(200) NOT NULL
        CHECK (
            workspace_id ~ '^[a-z0-9][a-z0-9_-]{2,199}$'
            AND octet_length(workspace_id) <= 200
        ),
    connection_id varchar(200) NOT NULL
        CHECK (connection_id ~ '^[a-z][a-z0-9_-]{2,199}$'),
    contract_version bigint NOT NULL CHECK (contract_version >= 1),
    connector_kind varchar(32) NOT NULL
        CHECK (connector_kind = 'postgresql'),
    sql_dialect varchar(32) NOT NULL
        CHECK (sql_dialect = 'postgresql'),
    expected_reader varchar(63) NOT NULL
        CHECK (expected_reader ~ '^[a-z_][a-z0-9_]{0,62}$'),
    type_contract_version integer NOT NULL
        CHECK (type_contract_version = 1),
    type_contract_fingerprint char(64) NOT NULL
        CHECK (
            type_contract_fingerprint
                = '07e1d8336a0dab019b080f114a1d31e082aed2b434ab84a1be59d98e2d000589'
        ),
    source_identity_fingerprint char(64) NOT NULL
        CHECK (source_identity_fingerprint ~ '^[0-9a-f]{64}$'),
    catalog_identity_fingerprint char(64) NOT NULL
        CHECK (catalog_identity_fingerprint ~ '^[0-9a-f]{64}$'),
    cost_budget_version integer NOT NULL
        CHECK (cost_budget_version = 1),
    explain_timeout_ms integer NOT NULL
        CHECK (explain_timeout_ms BETWEEN 1 AND 60000),
    max_response_bytes integer NOT NULL
        CHECK (max_response_bytes BETWEEN 1 AND 4194304),
    max_total_cost numeric(24, 6) NOT NULL
        CHECK (
            max_total_cost >= 0
            AND max_total_cost <= 999999999999999999
        ),
    max_estimated_rows bigint NOT NULL
        CHECK (max_estimated_rows BETWEEN 0 AND 9223372036854775807),
    max_plan_nodes integer NOT NULL
        CHECK (max_plan_nodes BETWEEN 1 AND 100000),
    max_plan_depth integer NOT NULL
        CHECK (max_plan_depth BETWEEN 1 AND 256),
    max_plan_width integer NOT NULL
        CHECK (max_plan_width BETWEEN 0 AND 2147483647),
    cost_budget_fingerprint char(64) NOT NULL
        CHECK (cost_budget_fingerprint ~ '^[0-9a-f]{64}$'),
    contract_fingerprint char(64) NOT NULL
        CHECK (contract_fingerprint ~ '^[0-9a-f]{64}$'),
    approval_id varchar(200) NOT NULL
        CHECK (approval_id ~ '^[a-z][a-z0-9_-]{2,199}$'),
    approval_fingerprint char(64) NOT NULL
        CHECK (approval_fingerprint ~ '^[0-9a-f]{64}$'),
    approved_by_actor_id varchar(200) NOT NULL
        CHECK (
            approved_by_actor_id ~ '^[a-z0-9][a-z0-9_-]{2,199}$'
            AND octet_length(approved_by_actor_id) <= 200
        ),
    approved_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, connection_id, contract_version),
    UNIQUE (workspace_id, connection_id, contract_fingerprint),
    CONSTRAINT connector_contract_connection_fk
        FOREIGN KEY (workspace_id, connection_id)
        REFERENCES schemabridge_control.catalog_connections (
            workspace_id,
            connection_id
        ),
    CONSTRAINT connector_contract_cost_fingerprint_shape
        CHECK (
            cost_budget_fingerprint
                = schemabridge_control.connector_cost_budget_fingerprint(
                    explain_timeout_ms,
                    max_response_bytes,
                    max_total_cost,
                    max_estimated_rows,
                    max_plan_nodes,
                    max_plan_depth,
                    max_plan_width
                )
        ),
    CONSTRAINT connector_contract_time_shape
        CHECK (created_at >= approved_at)
);

CREATE TABLE schemabridge_control.connector_route_revisions (
    workspace_id varchar(200) NOT NULL,
    connection_id varchar(200) NOT NULL,
    route_revision bigint NOT NULL CHECK (route_revision >= 1),
    contract_version bigint NOT NULL CHECK (contract_version >= 1),
    route_fingerprint char(64) NOT NULL
        CHECK (route_fingerprint ~ '^[0-9a-f]{64}$'),
    target_fingerprint char(64) NOT NULL
        CHECK (target_fingerprint ~ '^[0-9a-f]{64}$'),
    proposal_fingerprint char(64) NOT NULL
        CHECK (proposal_fingerprint ~ '^[0-9a-f]{64}$'),
    approval_id varchar(200) NOT NULL
        CHECK (approval_id ~ '^[a-z][a-z0-9_-]{2,199}$'),
    approval_fingerprint char(64) NOT NULL
        CHECK (approval_fingerprint ~ '^[0-9a-f]{64}$'),
    created_by_actor_id varchar(200) NOT NULL
        CHECK (
            created_by_actor_id ~ '^[a-z0-9][a-z0-9_-]{2,199}$'
            AND octet_length(created_by_actor_id) <= 200
        ),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, connection_id, route_revision),
    UNIQUE (
        workspace_id,
        connection_id,
        contract_version,
        route_revision,
        route_fingerprint,
        target_fingerprint
    ),
    UNIQUE (workspace_id, connection_id, target_fingerprint),
    CONSTRAINT connector_route_contract_fk
        FOREIGN KEY (workspace_id, connection_id, contract_version)
        REFERENCES schemabridge_control.connector_contract_revisions (
            workspace_id,
            connection_id,
            contract_version
        )
);

CREATE TABLE schemabridge_control.connector_private_route_revisions (
    workspace_id varchar(200) NOT NULL,
    connection_id varchar(200) NOT NULL,
    route_revision bigint NOT NULL CHECK (route_revision >= 1),
    capability varchar(16) NOT NULL
        CHECK (capability IN ('preflight', 'catalog', 'execution', 'profile')),
    credential_binding_ref varchar(200) NOT NULL
        CHECK (
            credential_binding_ref ~ '^[a-z][a-z0-9._:-]{2,199}$'
            AND credential_binding_ref !~ '://'
            AND credential_binding_ref !~ '@'
        ),
    created_by_actor_id varchar(200) NOT NULL
        CHECK (
            created_by_actor_id ~ '^[a-z0-9][a-z0-9_-]{2,199}$'
            AND octet_length(created_by_actor_id) <= 200
        ),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (
        workspace_id,
        connection_id,
        route_revision,
        capability
    ),
    UNIQUE (
        workspace_id,
        connection_id,
        route_revision,
        credential_binding_ref
    ),
    CONSTRAINT connector_private_route_revision_fk
        FOREIGN KEY (workspace_id, connection_id, route_revision)
        REFERENCES schemabridge_control.connector_route_revisions (
            workspace_id,
            connection_id,
            route_revision
        )
);

CREATE TABLE schemabridge_control.connector_route_heads (
    workspace_id varchar(200) NOT NULL,
    connection_id varchar(200) NOT NULL,
    head_revision bigint NOT NULL CHECK (head_revision >= 1),
    contract_version bigint NOT NULL CHECK (contract_version >= 1),
    route_revision bigint NOT NULL CHECK (route_revision >= 1),
    route_fingerprint char(64) NOT NULL
        CHECK (route_fingerprint ~ '^[0-9a-f]{64}$'),
    target_fingerprint char(64) NOT NULL
        CHECK (target_fingerprint ~ '^[0-9a-f]{64}$'),
    status varchar(16) NOT NULL CHECK (status IN ('enabled', 'disabled')),
    head_fingerprint char(64) NOT NULL
        CHECK (head_fingerprint ~ '^[0-9a-f]{64}$'),
    updated_by_actor_id varchar(200) NOT NULL
        CHECK (
            updated_by_actor_id ~ '^[a-z0-9][a-z0-9_-]{2,199}$'
            AND octet_length(updated_by_actor_id) <= 200
        ),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, connection_id),
    CONSTRAINT connector_route_head_revision_fk
        FOREIGN KEY (
            workspace_id,
            connection_id,
            contract_version,
            route_revision,
            route_fingerprint,
            target_fingerprint
        )
        REFERENCES schemabridge_control.connector_route_revisions (
            workspace_id,
            connection_id,
            contract_version,
            route_revision,
            route_fingerprint,
            target_fingerprint
        ),
    CONSTRAINT connector_route_head_time_shape
        CHECK (updated_at >= created_at)
);

ALTER TABLE schemabridge_control.catalog_generations
    ADD COLUMN source_identity_fingerprint char(64)
        CHECK (
            source_identity_fingerprint IS NULL
            OR source_identity_fingerprint ~ '^[0-9a-f]{64}$'
        ),
    ADD COLUMN catalog_identity_fingerprint char(64)
        CHECK (
            catalog_identity_fingerprint IS NULL
            OR catalog_identity_fingerprint ~ '^[0-9a-f]{64}$'
        ),
    ADD COLUMN type_contract_fingerprint char(64)
        CHECK (
            type_contract_fingerprint IS NULL
            OR type_contract_fingerprint
                = '07e1d8336a0dab019b080f114a1d31e082aed2b434ab84a1be59d98e2d000589'
        ),
    ADD CONSTRAINT catalog_generation_semantic_identity_shape
        CHECK (
            (
                source_identity_fingerprint IS NULL
                AND catalog_identity_fingerprint IS NULL
                AND type_contract_fingerprint IS NULL
            )
            OR
            (
                source_identity_fingerprint IS NOT NULL
                AND catalog_identity_fingerprint IS NOT NULL
                AND type_contract_fingerprint IS NOT NULL
            )
        );

CREATE TABLE schemabridge_control.catalog_refresh_semantic_bindings (
    workspace_id varchar(200) NOT NULL,
    connection_id varchar(200) NOT NULL,
    refresh_id varchar(200) NOT NULL,
    contract_version bigint NOT NULL CHECK (contract_version >= 1),
    route_revision bigint NOT NULL CHECK (route_revision >= 1),
    route_fingerprint char(64) NOT NULL
        CHECK (route_fingerprint ~ '^[0-9a-f]{64}$'),
    target_fingerprint char(64) NOT NULL
        CHECK (target_fingerprint ~ '^[0-9a-f]{64}$'),
    source_identity_fingerprint char(64) NOT NULL
        CHECK (source_identity_fingerprint ~ '^[0-9a-f]{64}$'),
    catalog_identity_fingerprint char(64) NOT NULL
        CHECK (catalog_identity_fingerprint ~ '^[0-9a-f]{64}$'),
    type_contract_fingerprint char(64) NOT NULL
        CHECK (
            type_contract_fingerprint
                = '07e1d8336a0dab019b080f114a1d31e082aed2b434ab84a1be59d98e2d000589'
        ),
    bound_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, connection_id, refresh_id),
    CONSTRAINT catalog_refresh_semantic_binding_refresh_fk
        FOREIGN KEY (workspace_id, connection_id, refresh_id)
        REFERENCES schemabridge_control.catalog_refresh_runs (
            workspace_id,
            connection_id,
            refresh_id
        ),
    CONSTRAINT catalog_refresh_semantic_binding_target_fk
        FOREIGN KEY (
            workspace_id,
            connection_id,
            contract_version,
            route_revision,
            route_fingerprint,
            target_fingerprint
        )
        REFERENCES schemabridge_control.connector_route_revisions (
            workspace_id,
            connection_id,
            contract_version,
            route_revision,
            route_fingerprint,
            target_fingerprint
        )
);

CREATE TABLE schemabridge_control.connector_route_audit (
    audit_id varchar(96) PRIMARY KEY
        CHECK (audit_id ~ '^connector_route_audit_[0-9a-f]{64}$'),
    workspace_id varchar(200) NOT NULL,
    connection_id varchar(200) NOT NULL,
    operation varchar(16) NOT NULL
        CHECK (operation IN ('create', 'rotate', 'disable')),
    expected_head_revision bigint NOT NULL
        CHECK (expected_head_revision >= 0),
    resulting_head_revision bigint NOT NULL
        CHECK (resulting_head_revision = expected_head_revision + 1),
    previous_contract_version bigint CHECK (previous_contract_version >= 1),
    previous_route_revision bigint CHECK (previous_route_revision >= 1),
    previous_target_fingerprint char(64)
        CHECK (
            previous_target_fingerprint IS NULL
            OR previous_target_fingerprint ~ '^[0-9a-f]{64}$'
        ),
    contract_version bigint NOT NULL CHECK (contract_version >= 1),
    route_revision bigint NOT NULL CHECK (route_revision >= 1),
    route_fingerprint char(64) NOT NULL
        CHECK (route_fingerprint ~ '^[0-9a-f]{64}$'),
    target_fingerprint char(64) NOT NULL
        CHECK (target_fingerprint ~ '^[0-9a-f]{64}$'),
    resulting_status varchar(16) NOT NULL
        CHECK (resulting_status IN ('enabled', 'disabled')),
    head_fingerprint char(64) NOT NULL
        CHECK (head_fingerprint ~ '^[0-9a-f]{64}$'),
    proposal_fingerprint char(64) NOT NULL
        CHECK (proposal_fingerprint ~ '^[0-9a-f]{64}$'),
    approval_id varchar(200) NOT NULL
        CHECK (approval_id ~ '^[a-z][a-z0-9_-]{2,199}$'),
    approval_fingerprint char(64) NOT NULL
        CHECK (approval_fingerprint ~ '^[0-9a-f]{64}$'),
    idempotency_digest char(64) NOT NULL
        CHECK (idempotency_digest ~ '^[0-9a-f]{64}$'),
    actor_id varchar(200) NOT NULL
        CHECK (
            actor_id ~ '^[a-z0-9][a-z0-9_-]{2,199}$'
            AND octet_length(actor_id) <= 200
        ),
    occurred_at timestamptz NOT NULL,
    audit_fingerprint char(64) NOT NULL
        CHECK (audit_fingerprint ~ '^[0-9a-f]{64}$'),
    UNIQUE (workspace_id, idempotency_digest),
    UNIQUE (workspace_id, connection_id, resulting_head_revision),
    UNIQUE (workspace_id, connection_id, audit_fingerprint),
    CONSTRAINT connector_route_audit_revision_fk
        FOREIGN KEY (
            workspace_id,
            connection_id,
            contract_version,
            route_revision,
            route_fingerprint,
            target_fingerprint
        )
        REFERENCES schemabridge_control.connector_route_revisions (
            workspace_id,
            connection_id,
            contract_version,
            route_revision,
            route_fingerprint,
            target_fingerprint
        ),
    CONSTRAINT connector_route_audit_previous_shape
        CHECK (
            (
                operation = 'create'
                AND expected_head_revision = 0
                AND previous_contract_version IS NULL
                AND previous_route_revision IS NULL
                AND previous_target_fingerprint IS NULL
                AND resulting_status = 'enabled'
            )
            OR
            (
                operation IN ('rotate', 'disable')
                AND expected_head_revision >= 1
                AND previous_contract_version IS NOT NULL
                AND previous_route_revision IS NOT NULL
                AND previous_target_fingerprint IS NOT NULL
            )
        ),
    CONSTRAINT connector_route_audit_operation_shape
        CHECK (
            (operation IN ('create', 'rotate') AND resulting_status = 'enabled')
            OR (operation = 'disable' AND resulting_status = 'disabled')
        )
);

CREATE INDEX connector_route_audit_scope_idx
    ON schemabridge_control.connector_route_audit (
        workspace_id,
        connection_id,
        occurred_at DESC,
        audit_id
    );

ALTER TABLE schemabridge_control.execution_jobs
    ADD COLUMN connector_workspace_id varchar(200),
    ADD COLUMN connector_connection_id varchar(200),
    ADD COLUMN connector_contract_version bigint,
    ADD COLUMN connector_route_revision bigint,
    ADD COLUMN connector_route_fingerprint char(64),
    ADD COLUMN connector_target_fingerprint char(64);

ALTER TABLE schemabridge_control.execution_jobs
    ADD CONSTRAINT execution_jobs_connector_target_shape
    CHECK (
        (
            connector_workspace_id IS NULL
            AND connector_connection_id IS NULL
            AND connector_contract_version IS NULL
            AND connector_route_revision IS NULL
            AND connector_route_fingerprint IS NULL
            AND connector_target_fingerprint IS NULL
            AND status IN ('succeeded', 'failed', 'cancelled', 'dead_lettered')
        )
        OR
        (
            connector_workspace_id IS NOT NULL
            AND connector_connection_id IS NOT NULL
            AND connector_contract_version IS NOT NULL
            AND connector_route_revision IS NOT NULL
            AND connector_route_fingerprint IS NOT NULL
            AND connector_target_fingerprint IS NOT NULL
            AND connector_workspace_id = workflow_workspace_id
            AND connector_workspace_id
                ~ '^[a-z0-9][a-z0-9_-]{2,199}$'
            AND octet_length(connector_workspace_id) <= 200
            AND connector_connection_id
                ~ '^[a-z][a-z0-9_-]{2,199}$'
            AND connector_contract_version >= 1
            AND connector_route_revision >= 1
            AND connector_route_fingerprint ~ '^[0-9a-f]{64}$'
            AND connector_target_fingerprint ~ '^[0-9a-f]{64}$'
        )
    ),
    ADD CONSTRAINT execution_jobs_connector_target_fk
    FOREIGN KEY (
        connector_workspace_id,
        connector_connection_id,
        connector_contract_version,
        connector_route_revision,
        connector_route_fingerprint,
        connector_target_fingerprint
    )
    REFERENCES schemabridge_control.connector_route_revisions (
        workspace_id,
        connection_id,
        contract_version,
        route_revision,
        route_fingerprint,
        target_fingerprint
    )
    MATCH SIMPLE;

ALTER TABLE schemabridge_control.semantic_join_profile_jobs
    ADD COLUMN connector_contract_version bigint,
    ADD COLUMN connector_route_revision bigint,
    ADD COLUMN connector_route_fingerprint char(64),
    ADD COLUMN connector_target_fingerprint char(64);

ALTER TABLE schemabridge_control.semantic_join_profile_jobs
    ADD CONSTRAINT semantic_profile_jobs_connector_target_shape
    CHECK (
        (
            connector_contract_version IS NULL
            AND connector_route_revision IS NULL
            AND connector_route_fingerprint IS NULL
            AND connector_target_fingerprint IS NULL
            AND status IN ('completed', 'failed')
        )
        OR
        (
            connector_contract_version IS NOT NULL
            AND connector_route_revision IS NOT NULL
            AND connector_route_fingerprint IS NOT NULL
            AND connector_target_fingerprint IS NOT NULL
            AND connector_contract_version >= 1
            AND connector_route_revision >= 1
            AND connector_route_fingerprint ~ '^[0-9a-f]{64}$'
            AND connector_target_fingerprint ~ '^[0-9a-f]{64}$'
        )
    ),
    ADD CONSTRAINT semantic_profile_jobs_connector_target_fk
    FOREIGN KEY (
        workspace_id,
        connection_id,
        connector_contract_version,
        connector_route_revision,
        connector_route_fingerprint,
        connector_target_fingerprint
    )
    REFERENCES schemabridge_control.connector_route_revisions (
        workspace_id,
        connection_id,
        contract_version,
        route_revision,
        route_fingerprint,
        target_fingerprint
    )
    MATCH SIMPLE;

CREATE INDEX semantic_join_profile_job_global_claim_idx
    ON schemabridge_control.semantic_join_profile_jobs (
        available_at,
        requested_at,
        workspace_id,
        connection_id,
        job_id
    )
    WHERE status IN ('requested', 'retry_wait')
      AND connector_target_fingerprint IS NOT NULL;

CREATE INDEX semantic_join_profile_job_global_expired_lease_idx
    ON schemabridge_control.semantic_join_profile_jobs (
        lease_expires_at,
        workspace_id,
        connection_id,
        job_id
    )
    WHERE status = 'leased'
      AND connector_target_fingerprint IS NOT NULL;

CREATE FUNCTION schemabridge_control.reject_connector_immutable_mutation()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, schemabridge_control
AS $$
BEGIN
    RAISE EXCEPTION 'immutable connector state cannot be changed'
        USING ERRCODE = '55000';
END;
$$;

CREATE FUNCTION schemabridge_control.guard_catalog_generation_semantic_identity()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
DECLARE
    binding_record record;
    base_record record;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF (
            SESSION_USER NOT IN ('schemabridge_catalog', 'schemabridge_migrator')
            OR NEW.status <> 'staging'
            OR NEW.source_identity_fingerprint IS NOT NULL
            OR NEW.catalog_identity_fingerprint IS NOT NULL
            OR NEW.type_contract_fingerprint IS NOT NULL
        ) THEN
            RAISE EXCEPTION 'new catalog generation identity is invalid'
                USING ERRCODE = '55000';
        END IF;
        SELECT
            binding.source_identity_fingerprint,
            binding.catalog_identity_fingerprint,
            binding.type_contract_fingerprint
        INTO binding_record
        FROM schemabridge_control.catalog_refresh_semantic_bindings AS binding
        WHERE binding.workspace_id = NEW.workspace_id
          AND binding.connection_id = NEW.connection_id
          AND binding.refresh_id = NEW.refresh_id;
        IF binding_record.source_identity_fingerprint IS NULL THEN
            RAISE EXCEPTION 'catalog refresh semantic identity is unavailable'
                USING ERRCODE = '55000';
        END IF;
        IF NEW.refresh_mode = 'delta' THEN
            SELECT
                generation.source_identity_fingerprint,
                generation.catalog_identity_fingerprint,
                generation.type_contract_fingerprint
            INTO base_record
            FROM schemabridge_control.catalog_generations AS generation
            WHERE generation.workspace_id = NEW.workspace_id
              AND generation.connection_id = NEW.connection_id
              AND generation.generation = NEW.base_generation
              AND generation.status = 'completed';
            IF (
                base_record.source_identity_fingerprint IS NULL
                OR base_record.source_identity_fingerprint
                    <> binding_record.source_identity_fingerprint
                OR base_record.catalog_identity_fingerprint
                    <> binding_record.catalog_identity_fingerprint
                OR base_record.type_contract_fingerprint
                    <> binding_record.type_contract_fingerprint
            ) THEN
                RAISE EXCEPTION 'catalog delta semantic identity is stale'
                    USING ERRCODE = '55000';
            END IF;
        END IF;
        NEW.source_identity_fingerprint :=
            binding_record.source_identity_fingerprint;
        NEW.catalog_identity_fingerprint :=
            binding_record.catalog_identity_fingerprint;
        NEW.type_contract_fingerprint :=
            binding_record.type_contract_fingerprint;
        RETURN NEW;
    END IF;

    IF (
        NEW.source_identity_fingerprint
            IS DISTINCT FROM OLD.source_identity_fingerprint
        OR NEW.catalog_identity_fingerprint
            IS DISTINCT FROM OLD.catalog_identity_fingerprint
        OR NEW.type_contract_fingerprint
            IS DISTINCT FROM OLD.type_contract_fingerprint
    ) THEN
        RAISE EXCEPTION 'catalog generation semantic identity is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION schemabridge_control.guard_connector_route_head()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, schemabridge_control
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'connector route head cannot be deleted'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.head_revision <> 1 OR NEW.status <> 'enabled' THEN
            RAISE EXCEPTION 'connector route head must start enabled at revision one'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;
    IF (
        NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
        OR NEW.connection_id IS DISTINCT FROM OLD.connection_id
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
        OR NEW.head_revision <> OLD.head_revision + 1
        OR NEW.updated_at <= OLD.updated_at
        OR (
            NEW.status = 'enabled'
            AND NEW.route_revision <= OLD.route_revision
        )
        OR (
            NEW.status = 'disabled'
            AND (
                OLD.status <> 'enabled'
                OR NEW.contract_version <> OLD.contract_version
                OR NEW.route_revision <> OLD.route_revision
                OR NEW.route_fingerprint <> OLD.route_fingerprint
                OR NEW.target_fingerprint <> OLD.target_fingerprint
            )
        )
    ) THEN
        RAISE EXCEPTION 'connector route head compare-and-swap is invalid'
            USING ERRCODE = '40001';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION schemabridge_control.guard_execution_job_connector_target()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
DECLARE
    resolved_contract_version bigint;
BEGIN
    IF SESSION_USER NOT IN (
        'schemabridge_api',
        'schemabridge_worker',
        'schemabridge_migrator'
    ) THEN
        RAISE EXCEPTION 'execution job connector target role is invalid'
            USING ERRCODE = '42501';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF (
            NEW.connector_workspace_id IS NULL
            OR NEW.connector_connection_id IS NULL
            OR NEW.connector_route_revision IS NULL
            OR NEW.connector_route_fingerprint IS NULL
            OR NEW.connector_target_fingerprint IS NULL
        ) THEN
            RAISE EXCEPTION 'new execution job connector target is required'
                USING ERRCODE = '55000';
        END IF;
        SELECT head.contract_version
        INTO resolved_contract_version
        FROM schemabridge_control.connector_route_heads AS head
        JOIN schemabridge_control.catalog_connections AS connection
          ON connection.workspace_id = head.workspace_id
         AND connection.connection_id = head.connection_id
        JOIN schemabridge_control.connector_contract_revisions AS contract
          ON contract.workspace_id = head.workspace_id
         AND contract.connection_id = head.connection_id
         AND contract.contract_version = head.contract_version
        JOIN schemabridge_control.catalog_generations AS generation
          ON generation.workspace_id = connection.workspace_id
         AND generation.connection_id = connection.connection_id
         AND generation.generation = connection.active_generation
         AND generation.status = 'completed'
         AND generation.source_identity_fingerprint
                = contract.source_identity_fingerprint
         AND generation.catalog_identity_fingerprint
                = contract.catalog_identity_fingerprint
         AND generation.type_contract_fingerprint
                = contract.type_contract_fingerprint
        WHERE head.workspace_id = NEW.connector_workspace_id
          AND head.connection_id = NEW.connector_connection_id
          AND head.route_revision = NEW.connector_route_revision
          AND head.route_fingerprint = NEW.connector_route_fingerprint
          AND head.target_fingerprint = NEW.connector_target_fingerprint
          AND head.status = 'enabled'
          AND connection.status = 'enabled'
          AND contract.type_contract_version = 1
          AND contract.type_contract_fingerprint
                = '07e1d8336a0dab019b080f114a1d31e082aed2b434ab84a1be59d98e2d000589';
        IF (
            resolved_contract_version IS NULL
            OR (
                NEW.connector_contract_version IS NOT NULL
                AND NEW.connector_contract_version
                    <> resolved_contract_version
            )
        ) THEN
            RAISE EXCEPTION 'execution job connector target is not current'
                USING ERRCODE = '55000';
        END IF;
        NEW.connector_contract_version := resolved_contract_version;
    ELSIF TG_OP = 'UPDATE' AND (
        NEW.connector_workspace_id
            IS DISTINCT FROM OLD.connector_workspace_id
        OR NEW.connector_connection_id
            IS DISTINCT FROM OLD.connector_connection_id
        OR NEW.connector_contract_version
            IS DISTINCT FROM OLD.connector_contract_version
        OR NEW.connector_route_revision
            IS DISTINCT FROM OLD.connector_route_revision
        OR NEW.connector_route_fingerprint
            IS DISTINCT FROM OLD.connector_route_fingerprint
        OR NEW.connector_target_fingerprint
            IS DISTINCT FROM OLD.connector_target_fingerprint
    ) THEN
        RAISE EXCEPTION 'execution job connector target is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

CREATE FUNCTION schemabridge_control.guard_profile_job_connector_target()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
DECLARE
    resolved_contract_version bigint;
BEGIN
    IF SESSION_USER NOT IN (
        'schemabridge_reconciler',
        'schemabridge_worker',
        'schemabridge_migrator'
    ) THEN
        RAISE EXCEPTION 'profile job connector target role is invalid'
            USING ERRCODE = '42501';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF (
            NEW.connector_route_revision IS NULL
            OR NEW.connector_route_fingerprint IS NULL
            OR NEW.connector_target_fingerprint IS NULL
        ) THEN
            RAISE EXCEPTION 'new profile job connector target is required'
                USING ERRCODE = '55000';
        END IF;
        SELECT head.contract_version
        INTO resolved_contract_version
        FROM schemabridge_control.connector_route_heads AS head
        JOIN schemabridge_control.catalog_connections AS connection
          ON connection.workspace_id = head.workspace_id
         AND connection.connection_id = head.connection_id
        JOIN schemabridge_control.connector_contract_revisions AS contract
          ON contract.workspace_id = head.workspace_id
         AND contract.connection_id = head.connection_id
         AND contract.contract_version = head.contract_version
        JOIN schemabridge_control.catalog_generations AS generation
          ON generation.workspace_id = connection.workspace_id
         AND generation.connection_id = connection.connection_id
         AND generation.generation = connection.active_generation
         AND generation.status = 'completed'
         AND generation.source_identity_fingerprint
                = contract.source_identity_fingerprint
         AND generation.catalog_identity_fingerprint
                = contract.catalog_identity_fingerprint
         AND generation.type_contract_fingerprint
                = contract.type_contract_fingerprint
        WHERE head.workspace_id = NEW.workspace_id
          AND head.connection_id = NEW.connection_id
          AND head.route_revision = NEW.connector_route_revision
          AND head.route_fingerprint = NEW.connector_route_fingerprint
          AND head.target_fingerprint = NEW.connector_target_fingerprint
          AND head.status = 'enabled'
          AND connection.status = 'enabled'
          AND contract.type_contract_version = 1
          AND contract.type_contract_fingerprint
                = '07e1d8336a0dab019b080f114a1d31e082aed2b434ab84a1be59d98e2d000589';
        IF (
            resolved_contract_version IS NULL
            OR (
                NEW.connector_contract_version IS NOT NULL
                AND NEW.connector_contract_version
                    <> resolved_contract_version
            )
        ) THEN
            RAISE EXCEPTION 'profile job connector target is not current'
                USING ERRCODE = '55000';
        END IF;
        NEW.connector_contract_version := resolved_contract_version;
    ELSIF TG_OP = 'UPDATE' AND (
        NEW.connector_contract_version
            IS DISTINCT FROM OLD.connector_contract_version
        OR NEW.connector_route_revision
            IS DISTINCT FROM OLD.connector_route_revision
        OR NEW.connector_route_fingerprint
            IS DISTINCT FROM OLD.connector_route_fingerprint
        OR NEW.connector_target_fingerprint
            IS DISTINCT FROM OLD.connector_target_fingerprint
    ) THEN
        RAISE EXCEPTION 'profile job connector target is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

CREATE TRIGGER catalog_generation_semantic_identity_guard
BEFORE INSERT OR UPDATE
ON schemabridge_control.catalog_generations
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.guard_catalog_generation_semantic_identity();

CREATE TRIGGER catalog_refresh_semantic_bindings_immutable
BEFORE UPDATE OR DELETE
ON schemabridge_control.catalog_refresh_semantic_bindings
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.reject_connector_immutable_mutation();

CREATE TRIGGER connector_contract_revisions_immutable
BEFORE UPDATE OR DELETE
ON schemabridge_control.connector_contract_revisions
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.reject_connector_immutable_mutation();

CREATE TRIGGER connector_route_revisions_immutable
BEFORE UPDATE OR DELETE
ON schemabridge_control.connector_route_revisions
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.reject_connector_immutable_mutation();

CREATE TRIGGER connector_private_route_revisions_immutable
BEFORE UPDATE OR DELETE
ON schemabridge_control.connector_private_route_revisions
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.reject_connector_immutable_mutation();

CREATE TRIGGER connector_route_audit_immutable
BEFORE UPDATE OR DELETE
ON schemabridge_control.connector_route_audit
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.reject_connector_immutable_mutation();

CREATE TRIGGER connector_route_heads_guard
BEFORE INSERT OR UPDATE OR DELETE
ON schemabridge_control.connector_route_heads
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.guard_connector_route_head();

CREATE TRIGGER execution_jobs_connector_target_guard
BEFORE INSERT OR UPDATE
ON schemabridge_control.execution_jobs
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.guard_execution_job_connector_target();

CREATE TRIGGER semantic_profile_jobs_connector_target_guard
BEFORE INSERT OR UPDATE
ON schemabridge_control.semantic_join_profile_jobs
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.guard_profile_job_connector_target();

CREATE FUNCTION schemabridge_control.apply_connector_route_change(
    p_workspace_id varchar,
    p_connection_id varchar,
    p_operation varchar,
    p_expected_head_revision bigint,
    p_contract_version bigint,
    p_route_revision bigint,
    p_route_fingerprint char,
    p_expected_reader varchar,
    p_type_contract_version integer,
    p_type_contract_fingerprint char,
    p_source_identity_fingerprint char,
    p_catalog_identity_fingerprint char,
    p_explain_timeout_ms integer,
    p_max_response_bytes integer,
    p_max_total_cost numeric,
    p_max_estimated_rows bigint,
    p_max_plan_nodes integer,
    p_max_plan_depth integer,
    p_max_plan_width integer,
    p_cost_budget_fingerprint char,
    p_contract_fingerprint char,
    p_target_fingerprint char,
    p_preflight_binding_ref varchar,
    p_catalog_binding_ref varchar,
    p_execution_binding_ref varchar,
    p_profile_binding_ref varchar,
    p_proposal_fingerprint char,
    p_approval_id varchar,
    p_approval_fingerprint char,
    p_actor_id varchar,
    p_idempotency_digest char,
    p_audit_id varchar,
    p_audit_fingerprint char,
    p_head_fingerprint char,
    p_confirmation varchar
)
RETURNS TABLE (
    workspace_id varchar(200),
    connection_id varchar(200),
    head_revision bigint,
    contract_version bigint,
    route_revision bigint,
    route_fingerprint char(64),
    target_fingerprint char(64),
    status varchar(16),
    audit_id varchar(96)
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
DECLARE
    observed_at timestamptz;
    expected_confirmation varchar(64);
    connection_status varchar(16);
    current_head schemabridge_control.connector_route_heads%ROWTYPE;
    current_contract schemabridge_control.connector_contract_revisions%ROWTYPE;
    replay_audit schemabridge_control.connector_route_audit%ROWTYPE;
    current_head_exists boolean := false;
    replay_exists boolean := false;
    computed_cost_fingerprint char(64);
    computed_target_fingerprint char(64);
    next_status varchar(16);
BEGIN
    IF SESSION_USER <> 'schemabridge_migrator' THEN
        RAISE EXCEPTION 'connector route operator role is invalid'
            USING ERRCODE = '42501';
    END IF;

    expected_confirmation := CASE p_operation
        WHEN 'create' THEN 'CREATE CONNECTOR ROUTE'
        WHEN 'rotate' THEN 'ROTATE CONNECTOR ROUTE'
        WHEN 'disable' THEN 'DISABLE CONNECTOR ROUTE'
        ELSE NULL
    END;
    IF (
        p_workspace_id !~ '^[a-z0-9][a-z0-9_-]{2,199}$'
        OR octet_length(p_workspace_id) > 200
        OR p_connection_id !~ '^[a-z][a-z0-9_-]{2,199}$'
        OR p_expected_head_revision NOT BETWEEN 0 AND 9223372036854775806
        OR p_contract_version NOT BETWEEN 1 AND 9223372036854775807
        OR p_route_revision NOT BETWEEN 1 AND 9223372036854775807
        OR p_route_fingerprint !~ '^[0-9a-f]{64}$'
        OR p_expected_reader !~ '^[a-z_][a-z0-9_]{0,62}$'
        OR p_type_contract_version IS DISTINCT FROM 1
        OR p_type_contract_fingerprint IS DISTINCT FROM
            '07e1d8336a0dab019b080f114a1d31e082aed2b434ab84a1be59d98e2d000589'
        OR p_source_identity_fingerprint !~ '^[0-9a-f]{64}$'
        OR p_catalog_identity_fingerprint !~ '^[0-9a-f]{64}$'
        OR p_explain_timeout_ms NOT BETWEEN 1 AND 60000
        OR p_max_response_bytes NOT BETWEEN 1 AND 4194304
        OR p_max_total_cost < 0
        OR p_max_total_cost > 999999999999999999
        OR scale(p_max_total_cost) > 6
        OR p_max_estimated_rows NOT BETWEEN 0 AND 9223372036854775807
        OR p_max_plan_nodes NOT BETWEEN 1 AND 100000
        OR p_max_plan_depth NOT BETWEEN 1 AND 256
        OR p_max_plan_width NOT BETWEEN 0 AND 2147483647
        OR p_cost_budget_fingerprint !~ '^[0-9a-f]{64}$'
        OR p_contract_fingerprint !~ '^[0-9a-f]{64}$'
        OR p_target_fingerprint !~ '^[0-9a-f]{64}$'
        OR p_proposal_fingerprint !~ '^[0-9a-f]{64}$'
        OR p_approval_id !~ '^[a-z][a-z0-9_-]{2,199}$'
        OR p_approval_fingerprint !~ '^[0-9a-f]{64}$'
        OR p_actor_id !~ '^[a-z0-9][a-z0-9_-]{2,199}$'
        OR octet_length(p_actor_id) > 200
        OR p_idempotency_digest !~ '^[0-9a-f]{64}$'
        OR p_audit_id !~ '^connector_route_audit_[0-9a-f]{64}$'
        OR p_audit_fingerprint !~ '^[0-9a-f]{64}$'
        OR p_head_fingerprint !~ '^[0-9a-f]{64}$'
        OR expected_confirmation IS NULL
        OR p_confirmation IS DISTINCT FROM expected_confirmation
    ) THEN
        RAISE EXCEPTION 'connector route change input is invalid'
            USING ERRCODE = '22023';
    END IF;

    IF p_operation = 'disable' THEN
        IF (
            p_preflight_binding_ref IS NOT NULL
            OR p_catalog_binding_ref IS NOT NULL
            OR p_execution_binding_ref IS NOT NULL
            OR p_profile_binding_ref IS NOT NULL
        ) THEN
            RAISE EXCEPTION 'connector route disable input is invalid'
                USING ERRCODE = '22023';
        END IF;
    ELSIF (
        p_preflight_binding_ref !~ '^[a-z][a-z0-9._:-]{2,199}$'
        OR p_preflight_binding_ref ~ '://'
        OR p_preflight_binding_ref ~ '@'
        OR p_catalog_binding_ref !~ '^[a-z][a-z0-9._:-]{2,199}$'
        OR p_catalog_binding_ref ~ '://'
        OR p_catalog_binding_ref ~ '@'
        OR p_execution_binding_ref !~ '^[a-z][a-z0-9._:-]{2,199}$'
        OR p_execution_binding_ref ~ '://'
        OR p_execution_binding_ref ~ '@'
        OR p_profile_binding_ref !~ '^[a-z][a-z0-9._:-]{2,199}$'
        OR p_profile_binding_ref ~ '://'
        OR p_profile_binding_ref ~ '@'
        OR p_preflight_binding_ref = p_catalog_binding_ref
        OR p_preflight_binding_ref = p_execution_binding_ref
        OR p_preflight_binding_ref = p_profile_binding_ref
        OR p_catalog_binding_ref = p_execution_binding_ref
        OR p_catalog_binding_ref = p_profile_binding_ref
        OR p_execution_binding_ref = p_profile_binding_ref
    ) THEN
        RAISE EXCEPTION 'connector route capability input is invalid'
            USING ERRCODE = '22023';
    END IF;

    computed_cost_fingerprint :=
        schemabridge_control.connector_cost_budget_fingerprint(
            p_explain_timeout_ms,
            p_max_response_bytes,
            p_max_total_cost,
            p_max_estimated_rows,
            p_max_plan_nodes,
            p_max_plan_depth,
            p_max_plan_width
        );
    computed_target_fingerprint :=
        schemabridge_control.connector_target_fingerprint(
            p_workspace_id,
            p_connection_id,
            p_route_revision,
            p_route_fingerprint,
            p_expected_reader,
            p_type_contract_fingerprint,
            p_source_identity_fingerprint,
            p_catalog_identity_fingerprint,
            p_explain_timeout_ms,
            p_max_response_bytes,
            p_max_total_cost,
            p_max_estimated_rows,
            p_max_plan_nodes,
            p_max_plan_depth,
            p_max_plan_width,
            p_cost_budget_fingerprint
        );
    IF (
        p_cost_budget_fingerprint <> computed_cost_fingerprint
        OR p_target_fingerprint <> computed_target_fingerprint
    ) THEN
        RAISE EXCEPTION 'connector route public fingerprint is invalid'
            USING ERRCODE = '55000';
    END IF;

    PERFORM pg_advisory_xact_lock(
        hashtextextended(
            concat_ws(
                '|',
                'connector_route_idempotency_v1',
                p_workspace_id,
                p_idempotency_digest
            ),
            0
        )
    );

    SELECT *
    INTO replay_audit
    FROM schemabridge_control.connector_route_audit AS audit
    WHERE audit.workspace_id = p_workspace_id
      AND audit.idempotency_digest = p_idempotency_digest;
    replay_exists := FOUND;

    IF replay_exists THEN
        IF (
            replay_audit.connection_id <> p_connection_id
            OR replay_audit.operation <> p_operation
            OR replay_audit.expected_head_revision <> p_expected_head_revision
            OR replay_audit.contract_version <> p_contract_version
            OR replay_audit.route_revision <> p_route_revision
            OR replay_audit.route_fingerprint <> p_route_fingerprint
            OR replay_audit.target_fingerprint <> p_target_fingerprint
            OR replay_audit.proposal_fingerprint <> p_proposal_fingerprint
            OR replay_audit.approval_id <> p_approval_id
            OR replay_audit.approval_fingerprint <> p_approval_fingerprint
            OR replay_audit.actor_id <> p_actor_id
            OR replay_audit.audit_id <> p_audit_id
            OR replay_audit.audit_fingerprint <> p_audit_fingerprint
            OR replay_audit.head_fingerprint <> p_head_fingerprint
        ) THEN
            RAISE EXCEPTION 'connector route idempotency conflict'
                USING ERRCODE = '23505';
        END IF;
        IF NOT EXISTS (
            SELECT 1
            FROM schemabridge_control.connector_contract_revisions AS contract
            WHERE contract.workspace_id = p_workspace_id
              AND contract.connection_id = p_connection_id
              AND contract.contract_version = p_contract_version
              AND contract.expected_reader = p_expected_reader
              AND contract.type_contract_version = p_type_contract_version
              AND contract.type_contract_fingerprint
                    = p_type_contract_fingerprint
              AND contract.source_identity_fingerprint
                    = p_source_identity_fingerprint
              AND contract.catalog_identity_fingerprint
                    = p_catalog_identity_fingerprint
              AND contract.explain_timeout_ms = p_explain_timeout_ms
              AND contract.max_response_bytes = p_max_response_bytes
              AND contract.max_total_cost = p_max_total_cost
              AND contract.max_estimated_rows = p_max_estimated_rows
              AND contract.max_plan_nodes = p_max_plan_nodes
              AND contract.max_plan_depth = p_max_plan_depth
              AND contract.max_plan_width = p_max_plan_width
              AND contract.cost_budget_fingerprint
                    = p_cost_budget_fingerprint
              AND contract.contract_fingerprint = p_contract_fingerprint
        ) THEN
            RAISE EXCEPTION 'connector route idempotency conflict'
                USING ERRCODE = '23505';
        END IF;
        IF p_operation <> 'disable' AND NOT EXISTS (
            SELECT 1
            FROM schemabridge_control.connector_private_route_revisions AS private
            WHERE private.workspace_id = p_workspace_id
              AND private.connection_id = p_connection_id
              AND private.route_revision = p_route_revision
            GROUP BY
                private.workspace_id,
                private.connection_id,
                private.route_revision
            HAVING
                count(*) = 4
                AND bool_and(
                    CASE private.capability
                        WHEN 'preflight' THEN
                            private.credential_binding_ref
                                = p_preflight_binding_ref
                        WHEN 'catalog' THEN
                            private.credential_binding_ref
                                = p_catalog_binding_ref
                        WHEN 'execution' THEN
                            private.credential_binding_ref
                                = p_execution_binding_ref
                        WHEN 'profile' THEN
                            private.credential_binding_ref
                                = p_profile_binding_ref
                        ELSE false
                    END
                )
        ) THEN
            RAISE EXCEPTION 'connector route idempotency conflict'
                USING ERRCODE = '23505';
        END IF;

        RETURN QUERY
        SELECT
            replay_audit.workspace_id,
            replay_audit.connection_id,
            replay_audit.resulting_head_revision,
            replay_audit.contract_version,
            replay_audit.route_revision,
            replay_audit.route_fingerprint,
            replay_audit.target_fingerprint,
            replay_audit.resulting_status,
            replay_audit.audit_id;
        RETURN;
    END IF;

    SELECT connection.status
    INTO connection_status
    FROM schemabridge_control.catalog_connections AS connection
    WHERE connection.workspace_id = p_workspace_id
      AND connection.connection_id = p_connection_id
    FOR UPDATE;
    IF connection_status IS NULL THEN
        RAISE EXCEPTION 'connector route connection is unavailable'
            USING ERRCODE = '55000';
    END IF;
    IF p_operation <> 'disable' AND connection_status <> 'enabled' THEN
        RAISE EXCEPTION 'connector route connection is disabled'
            USING ERRCODE = '55000';
    END IF;

    SELECT *
    INTO current_head
    FROM schemabridge_control.connector_route_heads AS head
    WHERE head.workspace_id = p_workspace_id
      AND head.connection_id = p_connection_id
    FOR UPDATE;
    current_head_exists := FOUND;

    IF p_operation = 'create' THEN
        IF current_head_exists OR p_expected_head_revision <> 0 THEN
            RAISE EXCEPTION 'connector route head revision conflict'
                USING ERRCODE = '40001';
        END IF;
        IF p_contract_version <> 1 OR p_route_revision <> 1 THEN
            RAISE EXCEPTION 'connector route initial revision is invalid'
                USING ERRCODE = '55000';
        END IF;
    ELSE
        IF (
            NOT current_head_exists
            OR current_head.head_revision <> p_expected_head_revision
        ) THEN
            RAISE EXCEPTION 'connector route head revision conflict'
                USING ERRCODE = '40001';
        END IF;
        IF p_operation = 'rotate' AND (
            p_route_revision <> current_head.route_revision + 1
            OR p_contract_version < current_head.contract_version
        ) THEN
            RAISE EXCEPTION 'connector route rotation revision is invalid'
                USING ERRCODE = '55000';
        END IF;
        IF p_operation = 'disable' AND (
            current_head.status <> 'enabled'
            OR p_contract_version <> current_head.contract_version
            OR p_route_revision <> current_head.route_revision
            OR p_route_fingerprint <> current_head.route_fingerprint
            OR p_target_fingerprint <> current_head.target_fingerprint
        ) THEN
            RAISE EXCEPTION 'connector route disable target is stale'
                USING ERRCODE = '40001';
        END IF;
    END IF;

    -- Timestamp only after every route-serialization lock has been acquired.
    observed_at := clock_timestamp();

    IF p_operation <> 'disable' THEN
        INSERT INTO schemabridge_control.connector_contract_revisions (
            workspace_id,
            connection_id,
            contract_version,
            connector_kind,
            sql_dialect,
            expected_reader,
            type_contract_version,
            type_contract_fingerprint,
            source_identity_fingerprint,
            catalog_identity_fingerprint,
            cost_budget_version,
            explain_timeout_ms,
            max_response_bytes,
            max_total_cost,
            max_estimated_rows,
            max_plan_nodes,
            max_plan_depth,
            max_plan_width,
            cost_budget_fingerprint,
            contract_fingerprint,
            approval_id,
            approval_fingerprint,
            approved_by_actor_id,
            approved_at,
            created_at
        ) VALUES (
            p_workspace_id,
            p_connection_id,
            p_contract_version,
            'postgresql',
            'postgresql',
            p_expected_reader,
            p_type_contract_version,
            p_type_contract_fingerprint,
            p_source_identity_fingerprint,
            p_catalog_identity_fingerprint,
            1,
            p_explain_timeout_ms,
            p_max_response_bytes,
            p_max_total_cost,
            p_max_estimated_rows,
            p_max_plan_nodes,
            p_max_plan_depth,
            p_max_plan_width,
            p_cost_budget_fingerprint,
            p_contract_fingerprint,
            p_approval_id,
            p_approval_fingerprint,
            p_actor_id,
            observed_at,
            observed_at
        )
        ON CONFLICT ON CONSTRAINT connector_contract_revisions_pkey
        DO NOTHING;

        SELECT *
        INTO current_contract
        FROM schemabridge_control.connector_contract_revisions AS contract
        WHERE contract.workspace_id = p_workspace_id
          AND contract.connection_id = p_connection_id
          AND contract.contract_version = p_contract_version;
        IF (
            current_contract.workspace_id IS NULL
            OR current_contract.connector_kind <> 'postgresql'
            OR current_contract.sql_dialect <> 'postgresql'
            OR current_contract.expected_reader <> p_expected_reader
            OR current_contract.type_contract_version
                <> p_type_contract_version
            OR current_contract.type_contract_fingerprint
                <> p_type_contract_fingerprint
            OR current_contract.source_identity_fingerprint
                <> p_source_identity_fingerprint
            OR current_contract.catalog_identity_fingerprint
                <> p_catalog_identity_fingerprint
            OR current_contract.explain_timeout_ms <> p_explain_timeout_ms
            OR current_contract.max_response_bytes <> p_max_response_bytes
            OR current_contract.max_total_cost <> p_max_total_cost
            OR current_contract.max_estimated_rows <> p_max_estimated_rows
            OR current_contract.max_plan_nodes <> p_max_plan_nodes
            OR current_contract.max_plan_depth <> p_max_plan_depth
            OR current_contract.max_plan_width <> p_max_plan_width
            OR current_contract.cost_budget_fingerprint
                <> p_cost_budget_fingerprint
            OR current_contract.contract_fingerprint
                <> p_contract_fingerprint
        ) THEN
            RAISE EXCEPTION 'connector contract version conflict'
                USING ERRCODE = '23505';
        END IF;

        INSERT INTO schemabridge_control.connector_route_revisions (
            workspace_id,
            connection_id,
            route_revision,
            contract_version,
            route_fingerprint,
            target_fingerprint,
            proposal_fingerprint,
            approval_id,
            approval_fingerprint,
            created_by_actor_id,
            created_at
        ) VALUES (
            p_workspace_id,
            p_connection_id,
            p_route_revision,
            p_contract_version,
            p_route_fingerprint,
            p_target_fingerprint,
            p_proposal_fingerprint,
            p_approval_id,
            p_approval_fingerprint,
            p_actor_id,
            observed_at
        );

        INSERT INTO
            schemabridge_control.connector_private_route_revisions (
                workspace_id,
                connection_id,
                route_revision,
                capability,
                credential_binding_ref,
                created_by_actor_id,
                created_at
            )
        VALUES
            (
                p_workspace_id,
                p_connection_id,
                p_route_revision,
                'preflight',
                p_preflight_binding_ref,
                p_actor_id,
                observed_at
            ),
            (
                p_workspace_id,
                p_connection_id,
                p_route_revision,
                'catalog',
                p_catalog_binding_ref,
                p_actor_id,
                observed_at
            ),
            (
                p_workspace_id,
                p_connection_id,
                p_route_revision,
                'execution',
                p_execution_binding_ref,
                p_actor_id,
                observed_at
            ),
            (
                p_workspace_id,
                p_connection_id,
                p_route_revision,
                'profile',
                p_profile_binding_ref,
                p_actor_id,
                observed_at
            );
    ELSE
        SELECT *
        INTO current_contract
        FROM schemabridge_control.connector_contract_revisions AS contract
        WHERE contract.workspace_id = p_workspace_id
          AND contract.connection_id = p_connection_id
          AND contract.contract_version = p_contract_version;
        IF (
            current_contract.workspace_id IS NULL
            OR current_contract.expected_reader <> p_expected_reader
            OR current_contract.type_contract_version
                <> p_type_contract_version
            OR current_contract.type_contract_fingerprint
                <> p_type_contract_fingerprint
            OR current_contract.source_identity_fingerprint
                <> p_source_identity_fingerprint
            OR current_contract.catalog_identity_fingerprint
                <> p_catalog_identity_fingerprint
            OR current_contract.explain_timeout_ms <> p_explain_timeout_ms
            OR current_contract.max_response_bytes <> p_max_response_bytes
            OR current_contract.max_total_cost <> p_max_total_cost
            OR current_contract.max_estimated_rows <> p_max_estimated_rows
            OR current_contract.max_plan_nodes <> p_max_plan_nodes
            OR current_contract.max_plan_depth <> p_max_plan_depth
            OR current_contract.max_plan_width <> p_max_plan_width
            OR current_contract.cost_budget_fingerprint
                <> p_cost_budget_fingerprint
            OR current_contract.contract_fingerprint
                <> p_contract_fingerprint
        ) THEN
            RAISE EXCEPTION 'connector route disable contract is stale'
                USING ERRCODE = '40001';
        END IF;
    END IF;

    next_status := CASE
        WHEN p_operation = 'disable' THEN 'disabled'
        ELSE 'enabled'
    END;
    IF p_operation = 'create' THEN
        INSERT INTO schemabridge_control.connector_route_heads (
            workspace_id,
            connection_id,
            head_revision,
            contract_version,
            route_revision,
            route_fingerprint,
            target_fingerprint,
            status,
            head_fingerprint,
            updated_by_actor_id,
            created_at,
            updated_at
        ) VALUES (
            p_workspace_id,
            p_connection_id,
            1,
            p_contract_version,
            p_route_revision,
            p_route_fingerprint,
            p_target_fingerprint,
            next_status,
            p_head_fingerprint,
            p_actor_id,
            observed_at,
            observed_at
        );
    ELSE
        UPDATE schemabridge_control.connector_route_heads AS head
        SET head_revision = p_expected_head_revision + 1,
            contract_version = p_contract_version,
            route_revision = p_route_revision,
            route_fingerprint = p_route_fingerprint,
            target_fingerprint = p_target_fingerprint,
            status = next_status,
            head_fingerprint = p_head_fingerprint,
            updated_by_actor_id = p_actor_id,
            updated_at = observed_at
        WHERE head.workspace_id = p_workspace_id
          AND head.connection_id = p_connection_id
          AND head.head_revision = p_expected_head_revision;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'connector route head revision conflict'
                USING ERRCODE = '40001';
        END IF;
    END IF;

    INSERT INTO schemabridge_control.connector_route_audit (
        audit_id,
        workspace_id,
        connection_id,
        operation,
        expected_head_revision,
        resulting_head_revision,
        previous_contract_version,
        previous_route_revision,
        previous_target_fingerprint,
        contract_version,
        route_revision,
        route_fingerprint,
        target_fingerprint,
        resulting_status,
        head_fingerprint,
        proposal_fingerprint,
        approval_id,
        approval_fingerprint,
        idempotency_digest,
        actor_id,
        occurred_at,
        audit_fingerprint
    ) VALUES (
        p_audit_id,
        p_workspace_id,
        p_connection_id,
        p_operation,
        p_expected_head_revision,
        p_expected_head_revision + 1,
        CASE
            WHEN current_head_exists THEN current_head.contract_version
            ELSE NULL
        END,
        CASE
            WHEN current_head_exists THEN current_head.route_revision
            ELSE NULL
        END,
        CASE
            WHEN current_head_exists THEN current_head.target_fingerprint
            ELSE NULL
        END,
        p_contract_version,
        p_route_revision,
        p_route_fingerprint,
        p_target_fingerprint,
        next_status,
        p_head_fingerprint,
        p_proposal_fingerprint,
        p_approval_id,
        p_approval_fingerprint,
        p_idempotency_digest,
        p_actor_id,
        observed_at,
        p_audit_fingerprint
    );

    RETURN QUERY
    SELECT
        p_workspace_id,
        p_connection_id,
        p_expected_head_revision + 1,
        p_contract_version,
        p_route_revision,
        p_route_fingerprint::char(64),
        p_target_fingerprint::char(64),
        next_status,
        p_audit_id::varchar(96);
END;
$$;

CREATE FUNCTION schemabridge_control.load_current_connector_target(
    p_workspace_id varchar,
    p_connection_id varchar
)
RETURNS TABLE (
    workspace_id varchar(200),
    connection_id varchar(200),
    head_revision bigint,
    route_status varchar(16),
    connection_status varchar(16),
    contract_version bigint,
    connector_kind varchar(32),
    sql_dialect varchar(32),
    route_revision bigint,
    route_fingerprint char(64),
    target_fingerprint char(64),
    expected_reader varchar(63),
    type_contract_version integer,
    type_contract_fingerprint char(64),
    source_identity_fingerprint char(64),
    catalog_identity_fingerprint char(64),
    cost_budget_version integer,
    explain_timeout_ms integer,
    max_response_bytes integer,
    max_total_cost numeric(24, 6),
    max_estimated_rows bigint,
    max_plan_nodes integer,
    max_plan_depth integer,
    max_plan_width integer,
    cost_budget_fingerprint char(64)
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
BEGIN
    IF SESSION_USER NOT IN (
        'schemabridge_runtime',
        'schemabridge_reconciler',
        'schemabridge_catalog',
        'schemabridge_migrator'
    ) THEN
        RAISE EXCEPTION 'connector target role is invalid'
            USING ERRCODE = '42501';
    END IF;
    IF (
        p_workspace_id !~ '^[a-z0-9][a-z0-9_-]{2,199}$'
        OR octet_length(p_workspace_id) > 200
        OR p_connection_id !~ '^[a-z][a-z0-9_-]{2,199}$'
    ) THEN
        RAISE EXCEPTION 'connector target input is invalid'
            USING ERRCODE = '22023';
    END IF;

    RETURN QUERY
    SELECT
        head.workspace_id,
        head.connection_id,
        head.head_revision,
        head.status,
        connection.status,
        contract.contract_version,
        contract.connector_kind,
        contract.sql_dialect,
        route.route_revision,
        route.route_fingerprint,
        route.target_fingerprint,
        contract.expected_reader,
        contract.type_contract_version,
        contract.type_contract_fingerprint,
        contract.source_identity_fingerprint,
        contract.catalog_identity_fingerprint,
        contract.cost_budget_version,
        contract.explain_timeout_ms,
        contract.max_response_bytes,
        contract.max_total_cost,
        contract.max_estimated_rows,
        contract.max_plan_nodes,
        contract.max_plan_depth,
        contract.max_plan_width,
        contract.cost_budget_fingerprint
    FROM schemabridge_control.connector_route_heads AS head
    JOIN schemabridge_control.catalog_connections AS connection
      ON connection.workspace_id = head.workspace_id
     AND connection.connection_id = head.connection_id
    JOIN schemabridge_control.connector_route_revisions AS route
      ON route.workspace_id = head.workspace_id
     AND route.connection_id = head.connection_id
     AND route.route_revision = head.route_revision
     AND route.contract_version = head.contract_version
     AND route.route_fingerprint = head.route_fingerprint
     AND route.target_fingerprint = head.target_fingerprint
    JOIN schemabridge_control.connector_contract_revisions AS contract
      ON contract.workspace_id = route.workspace_id
     AND contract.connection_id = route.connection_id
     AND contract.contract_version = route.contract_version
    LEFT JOIN schemabridge_control.catalog_generations AS generation
      ON generation.workspace_id = connection.workspace_id
     AND generation.connection_id = connection.connection_id
     AND generation.generation = connection.active_generation
    WHERE head.workspace_id = p_workspace_id
      AND head.connection_id = p_connection_id
      AND contract.type_contract_version = 1
      AND contract.type_contract_fingerprint
            = '07e1d8336a0dab019b080f114a1d31e082aed2b434ab84a1be59d98e2d000589'
      AND (
          SESSION_USER IN ('schemabridge_catalog', 'schemabridge_migrator')
          OR (
              generation.status = 'completed'
              AND generation.source_identity_fingerprint
                    = contract.source_identity_fingerprint
              AND generation.catalog_identity_fingerprint
                    = contract.catalog_identity_fingerprint
              AND generation.type_contract_fingerprint
                    = contract.type_contract_fingerprint
          )
      );
END;
$$;

CREATE FUNCTION schemabridge_control.load_current_preflight_connector_route(
    p_workspace_id varchar,
    p_connection_id varchar,
    p_route_revision bigint,
    p_target_fingerprint char
)
RETURNS TABLE (
    workspace_id varchar(200),
    connection_id varchar(200),
    contract_version bigint,
    route_revision bigint,
    target_fingerprint char(64),
    sql_dialect varchar(32),
    expected_reader varchar(63),
    source_identity_fingerprint char(64),
    credential_binding_ref varchar(200)
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
BEGIN
    IF SESSION_USER NOT IN ('schemabridge_runtime', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'preflight connector route role is invalid'
            USING ERRCODE = '42501';
    END IF;
    IF (
        p_workspace_id !~ '^[a-z0-9][a-z0-9_-]{2,199}$'
        OR octet_length(p_workspace_id) > 200
        OR p_connection_id !~ '^[a-z][a-z0-9_-]{2,199}$'
        OR p_route_revision < 1
        OR p_target_fingerprint !~ '^[0-9a-f]{64}$'
    ) THEN
        RAISE EXCEPTION 'preflight connector route input is invalid'
            USING ERRCODE = '22023';
    END IF;

    RETURN QUERY
    SELECT
        head.workspace_id,
        head.connection_id,
        head.contract_version,
        head.route_revision,
        head.target_fingerprint,
        contract.sql_dialect,
        contract.expected_reader,
        contract.source_identity_fingerprint,
        private.credential_binding_ref
    FROM schemabridge_control.connector_route_heads AS head
    JOIN schemabridge_control.catalog_connections AS connection
      ON connection.workspace_id = head.workspace_id
     AND connection.connection_id = head.connection_id
    JOIN schemabridge_control.connector_contract_revisions AS contract
      ON contract.workspace_id = head.workspace_id
     AND contract.connection_id = head.connection_id
     AND contract.contract_version = head.contract_version
    LEFT JOIN schemabridge_control.catalog_generations AS generation
      ON generation.workspace_id = connection.workspace_id
     AND generation.connection_id = connection.connection_id
     AND generation.generation = connection.active_generation
    JOIN schemabridge_control.connector_private_route_revisions AS private
      ON private.workspace_id = head.workspace_id
     AND private.connection_id = head.connection_id
     AND private.route_revision = head.route_revision
     AND private.capability = 'preflight'
    WHERE head.workspace_id = p_workspace_id
      AND head.connection_id = p_connection_id
      AND head.route_revision = p_route_revision
      AND head.target_fingerprint = p_target_fingerprint
      AND connection.status = 'enabled'
      AND head.status = 'enabled'
      AND contract.connector_kind = 'postgresql'
      AND contract.sql_dialect = 'postgresql'
      AND contract.type_contract_version = 1
      AND contract.type_contract_fingerprint
            = '07e1d8336a0dab019b080f114a1d31e082aed2b434ab84a1be59d98e2d000589'
      AND (
          SESSION_USER = 'schemabridge_migrator'
          OR (
              generation.status = 'completed'
              AND generation.source_identity_fingerprint
                    = contract.source_identity_fingerprint
              AND generation.catalog_identity_fingerprint
                    = contract.catalog_identity_fingerprint
              AND generation.type_contract_fingerprint
                    = contract.type_contract_fingerprint
          )
      );
END;
$$;

CREATE FUNCTION schemabridge_control.load_owned_catalog_connector_route(
    p_workspace_id varchar,
    p_connection_id varchar,
    p_refresh_id varchar,
    p_indexer_id varchar,
    p_lease_capability varchar,
    p_fencing_token bigint,
    p_contract_version bigint,
    p_route_revision bigint,
    p_target_fingerprint char
)
RETURNS TABLE (
    workspace_id varchar(200),
    connection_id varchar(200),
    source_kind varchar(32),
    environment varchar(80),
    catalog_scope varchar(200),
    platform_instance varchar(200),
    catalog_identity_fingerprint char(64),
    contract_version bigint,
    route_revision bigint,
    target_fingerprint char(64),
    credential_binding_ref varchar(200)
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
DECLARE
    binding_record record;
    connection_record record;
    v_lease_capability_digest char(64);
    refresh_record record;
    route_record record;
BEGIN
    IF SESSION_USER NOT IN ('schemabridge_catalog', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'catalog connector route role is invalid'
            USING ERRCODE = '42501';
    END IF;
    IF (
        p_workspace_id IS NULL
        OR p_workspace_id !~ '^[a-z0-9][a-z0-9_-]{2,199}$'
        OR octet_length(p_workspace_id) > 200
        OR p_connection_id IS NULL
        OR p_connection_id !~ '^[a-z][a-z0-9_-]{2,199}$'
        OR p_refresh_id IS NULL
        OR p_refresh_id !~ '^[a-z][a-z0-9_-]{2,199}$'
        OR p_indexer_id !~ '^[a-z][a-z0-9_.:-]{2,199}$'
        OR p_lease_capability IS NULL
        OR p_lease_capability <> trim(p_lease_capability)
        OR octet_length(p_lease_capability) NOT BETWEEN 32 AND 1024
        OR p_fencing_token < 1
        OR p_contract_version < 1
        OR p_route_revision < 1
        OR p_target_fingerprint !~ '^[0-9a-f]{64}$'
    ) THEN
        RAISE EXCEPTION 'catalog connector route input is invalid'
            USING ERRCODE = '22023';
    END IF;
    v_lease_capability_digest := encode(
        sha256(convert_to(p_lease_capability, 'UTF8')),
        'hex'
    );

    -- Serialize in the same order as route rotation and final activation.
    SELECT
        connection.status,
        connection.source_kind,
        connection.environment,
        connection.catalog_scope,
        connection.platform_instance
    INTO connection_record
    FROM schemabridge_control.catalog_connections AS connection
    WHERE connection.workspace_id = p_workspace_id
      AND connection.connection_id = p_connection_id
    FOR UPDATE;

    SELECT
        head.status,
        head.contract_version,
        head.route_revision,
        head.route_fingerprint,
        head.target_fingerprint,
        contract.source_identity_fingerprint,
        contract.catalog_identity_fingerprint,
        contract.type_contract_fingerprint
    INTO route_record
    FROM schemabridge_control.connector_route_heads AS head
    JOIN schemabridge_control.connector_contract_revisions AS contract
      ON contract.workspace_id = head.workspace_id
     AND contract.connection_id = head.connection_id
     AND contract.contract_version = head.contract_version
    WHERE head.workspace_id = p_workspace_id
      AND head.connection_id = p_connection_id
      AND contract.type_contract_version = 1
      AND contract.type_contract_fingerprint
            = '07e1d8336a0dab019b080f114a1d31e082aed2b434ab84a1be59d98e2d000589'
    FOR SHARE OF head;

    SELECT
        refresh.status,
        refresh.lease_owner_id,
        refresh.lease_capability_digest,
        refresh.fencing_token,
        refresh.lease_expires_at
    INTO refresh_record
    FROM schemabridge_control.catalog_refresh_runs AS refresh
    WHERE refresh.workspace_id = p_workspace_id
      AND refresh.connection_id = p_connection_id
      AND refresh.refresh_id = p_refresh_id
    FOR UPDATE;

    IF (
        connection_record.status IS DISTINCT FROM 'enabled'
        OR route_record.status IS DISTINCT FROM 'enabled'
        OR route_record.contract_version IS DISTINCT FROM p_contract_version
        OR route_record.route_revision IS DISTINCT FROM p_route_revision
        OR route_record.target_fingerprint IS DISTINCT FROM p_target_fingerprint
        OR refresh_record.status NOT IN ('leased', 'staging')
        OR refresh_record.lease_owner_id IS DISTINCT FROM p_indexer_id
        OR refresh_record.lease_capability_digest
            IS DISTINCT FROM v_lease_capability_digest
        OR refresh_record.fencing_token IS DISTINCT FROM p_fencing_token
        OR refresh_record.lease_expires_at IS NULL
        OR refresh_record.lease_expires_at <= clock_timestamp()
    ) THEN
        RETURN;
    END IF;

    INSERT INTO schemabridge_control.catalog_refresh_semantic_bindings (
        workspace_id,
        connection_id,
        refresh_id,
        contract_version,
        route_revision,
        route_fingerprint,
        target_fingerprint,
        source_identity_fingerprint,
        catalog_identity_fingerprint,
        type_contract_fingerprint,
        bound_at
    ) VALUES (
        p_workspace_id,
        p_connection_id,
        p_refresh_id,
        route_record.contract_version,
        route_record.route_revision,
        route_record.route_fingerprint,
        route_record.target_fingerprint,
        route_record.source_identity_fingerprint,
        route_record.catalog_identity_fingerprint,
        route_record.type_contract_fingerprint,
        clock_timestamp()
    )
    ON CONFLICT DO NOTHING;

    SELECT *
    INTO binding_record
    FROM schemabridge_control.catalog_refresh_semantic_bindings AS binding
    WHERE binding.workspace_id = p_workspace_id
      AND binding.connection_id = p_connection_id
      AND binding.refresh_id = p_refresh_id;
    IF (
        binding_record.contract_version IS DISTINCT FROM p_contract_version
        OR binding_record.route_revision IS DISTINCT FROM p_route_revision
        OR binding_record.route_fingerprint
            IS DISTINCT FROM route_record.route_fingerprint
        OR binding_record.target_fingerprint IS DISTINCT FROM p_target_fingerprint
        OR binding_record.source_identity_fingerprint
            IS DISTINCT FROM route_record.source_identity_fingerprint
        OR binding_record.catalog_identity_fingerprint
            IS DISTINCT FROM route_record.catalog_identity_fingerprint
        OR binding_record.type_contract_fingerprint
            IS DISTINCT FROM route_record.type_contract_fingerprint
    ) THEN
        RETURN;
    END IF;

    RETURN QUERY
    SELECT
        connection.workspace_id,
        connection.connection_id,
        connection.source_kind,
        connection.environment,
        connection.catalog_scope,
        connection.platform_instance,
        contract.catalog_identity_fingerprint,
        head.contract_version,
        head.route_revision,
        head.target_fingerprint,
        private.credential_binding_ref
    FROM schemabridge_control.catalog_refresh_runs AS refresh
    JOIN schemabridge_control.catalog_connections AS connection
      ON connection.workspace_id = refresh.workspace_id
     AND connection.connection_id = refresh.connection_id
    JOIN schemabridge_control.connector_route_heads AS head
      ON head.workspace_id = connection.workspace_id
     AND head.connection_id = connection.connection_id
    JOIN schemabridge_control.connector_contract_revisions AS contract
      ON contract.workspace_id = head.workspace_id
     AND contract.connection_id = head.connection_id
     AND contract.contract_version = head.contract_version
    JOIN schemabridge_control.connector_private_route_revisions AS private
      ON private.workspace_id = head.workspace_id
     AND private.connection_id = head.connection_id
     AND private.route_revision = head.route_revision
     AND private.capability = 'catalog'
    WHERE refresh.workspace_id = p_workspace_id
      AND refresh.connection_id = p_connection_id
      AND refresh.refresh_id = p_refresh_id
      AND refresh.status IN ('leased', 'staging')
      AND refresh.lease_owner_id = p_indexer_id
      AND refresh.lease_capability_digest = v_lease_capability_digest
      AND refresh.fencing_token = p_fencing_token
      AND refresh.lease_expires_at > clock_timestamp()
      AND connection.status = 'enabled'
      AND head.status = 'enabled'
      AND head.contract_version = p_contract_version
      AND head.route_revision = p_route_revision
      AND head.target_fingerprint = p_target_fingerprint
      AND contract.type_contract_version = 1
      AND contract.type_contract_fingerprint
            = '07e1d8336a0dab019b080f114a1d31e082aed2b434ab84a1be59d98e2d000589'
      AND contract.source_identity_fingerprint
            = binding_record.source_identity_fingerprint
      AND contract.catalog_identity_fingerprint
            = binding_record.catalog_identity_fingerprint
      AND contract.type_contract_fingerprint
            = binding_record.type_contract_fingerprint;
END;
$$;

CREATE FUNCTION schemabridge_control.activate_catalog_generation_for_target(
    p_workspace_id varchar,
    p_connection_id varchar,
    p_refresh_id varchar,
    p_indexer_id varchar,
    p_lease_capability varchar,
    p_expected_base_generation bigint,
    p_target_generation bigint,
    p_fencing_token bigint,
    p_inventory_fingerprint char(64),
    p_contract_version bigint,
    p_route_revision bigint,
    p_target_fingerprint char(64)
)
RETURNS TABLE (
    activated_asset_count bigint,
    activated_field_count bigint
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
DECLARE
    connection_status varchar(16);
    generation_record record;
    lease_capability_digest char(64);
    policy_workspace_id varchar(200);
    refresh_record record;
    route_head record;
BEGIN
    IF SESSION_USER NOT IN ('schemabridge_catalog', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'catalog target activation role is invalid'
            USING ERRCODE = '42501';
    END IF;
    IF (
        p_workspace_id IS NULL
        OR p_workspace_id !~ '^[a-z0-9][a-z0-9_-]{2,199}$'
        OR octet_length(p_workspace_id) > 200
        OR p_connection_id IS NULL
        OR p_connection_id !~ '^[a-z][a-z0-9_-]{2,199}$'
        OR p_refresh_id IS NULL
        OR p_refresh_id !~ '^[a-z][a-z0-9_-]{2,199}$'
        OR p_indexer_id IS NULL
        OR p_indexer_id !~ '^[a-z][a-z0-9_.:-]{2,199}$'
        OR p_lease_capability IS NULL
        OR p_lease_capability <> trim(p_lease_capability)
        OR octet_length(p_lease_capability) NOT BETWEEN 32 AND 1024
        OR (
            p_expected_base_generation IS NOT NULL
            AND p_expected_base_generation < 1
        )
        OR p_target_generation IS NULL
        OR p_target_generation < 1
        OR p_fencing_token IS NULL
        OR p_fencing_token < 1
        OR p_inventory_fingerprint IS NULL
        OR p_inventory_fingerprint !~ '^[0-9a-f]{64}$'
        OR p_contract_version IS NULL
        OR p_contract_version < 1
        OR p_route_revision IS NULL
        OR p_route_revision < 1
        OR p_target_fingerprint IS NULL
        OR p_target_fingerprint !~ '^[0-9a-f]{64}$'
    ) THEN
        RAISE EXCEPTION 'catalog target activation input is invalid'
            USING ERRCODE = '22023';
    END IF;
    lease_capability_digest := encode(
        sha256(convert_to(p_lease_capability, 'UTF8')),
        'hex'
    );

    -- Match the route-change lock order: connection first, then route head.
    -- The shared head lock is retained through activation and prevents a
    -- concurrent rotation or disable from crossing the promotion boundary.
    SELECT connection.status
    INTO connection_status
    FROM schemabridge_control.catalog_connections AS connection
    WHERE connection.workspace_id = p_workspace_id
      AND connection.connection_id = p_connection_id
    FOR UPDATE;
    IF connection_status IS DISTINCT FROM 'enabled' THEN
        RAISE EXCEPTION 'catalog connector target is stale'
            USING ERRCODE = '40001';
    END IF;

    SELECT
        head.contract_version,
        head.route_revision,
        head.target_fingerprint,
        head.status,
        contract.source_identity_fingerprint,
        contract.catalog_identity_fingerprint,
        contract.type_contract_fingerprint
    INTO route_head
    FROM schemabridge_control.connector_route_heads AS head
    JOIN schemabridge_control.connector_contract_revisions AS contract
      ON contract.workspace_id = head.workspace_id
     AND contract.connection_id = head.connection_id
     AND contract.contract_version = head.contract_version
    WHERE head.workspace_id = p_workspace_id
      AND head.connection_id = p_connection_id
      AND contract.type_contract_version = 1
      AND contract.type_contract_fingerprint
            = '07e1d8336a0dab019b080f114a1d31e082aed2b434ab84a1be59d98e2d000589'
    FOR SHARE;
    IF (
        route_head.contract_version IS NULL
        OR route_head.status <> 'enabled'
        OR route_head.contract_version <> p_contract_version
        OR route_head.route_revision <> p_route_revision
        OR route_head.target_fingerprint <> p_target_fingerprint
    ) THEN
        RAISE EXCEPTION 'catalog connector target is stale'
            USING ERRCODE = '40001';
    END IF;

    SELECT
        refresh.refresh_id,
        refresh.status,
        refresh.base_generation,
        refresh.target_generation,
        refresh.lease_owner_id,
        refresh.lease_capability_digest,
        refresh.fencing_token,
        refresh.lease_expires_at,
        refresh.source_complete
    INTO refresh_record
    FROM schemabridge_control.catalog_refresh_runs AS refresh
    WHERE refresh.workspace_id = p_workspace_id
      AND refresh.connection_id = p_connection_id
      AND refresh.refresh_id = p_refresh_id
    FOR UPDATE;

    SELECT
        generation.generation,
        generation.refresh_id,
        generation.status,
        generation.base_generation,
        generation.source_identity_fingerprint,
        generation.catalog_identity_fingerprint,
        generation.type_contract_fingerprint
    INTO generation_record
    FROM schemabridge_control.catalog_generations AS generation
    WHERE generation.workspace_id = p_workspace_id
      AND generation.connection_id = p_connection_id
      AND generation.generation = p_target_generation
    FOR UPDATE;

    SELECT policy.workspace_id
    INTO policy_workspace_id
    FROM schemabridge_control.tenant_capacity_policies AS policy
    WHERE policy.workspace_id = p_workspace_id
    FOR UPDATE;

    IF (
        refresh_record.refresh_id IS NULL
        OR refresh_record.status <> 'staging'
        OR refresh_record.base_generation
            IS DISTINCT FROM p_expected_base_generation
        OR refresh_record.target_generation IS DISTINCT FROM p_target_generation
        OR refresh_record.lease_owner_id IS DISTINCT FROM p_indexer_id
        OR refresh_record.lease_capability_digest
            IS DISTINCT FROM lease_capability_digest
        OR refresh_record.fencing_token IS DISTINCT FROM p_fencing_token
        OR refresh_record.lease_expires_at IS NULL
        OR refresh_record.lease_expires_at <= clock_timestamp()
        OR NOT refresh_record.source_complete
        OR generation_record.generation IS NULL
        OR generation_record.refresh_id IS DISTINCT FROM p_refresh_id
        OR generation_record.status <> 'staging'
        OR generation_record.base_generation
            IS DISTINCT FROM p_expected_base_generation
        OR generation_record.source_identity_fingerprint
            IS DISTINCT FROM route_head.source_identity_fingerprint
        OR generation_record.catalog_identity_fingerprint
            IS DISTINCT FROM route_head.catalog_identity_fingerprint
        OR generation_record.type_contract_fingerprint
            IS DISTINCT FROM route_head.type_contract_fingerprint
        OR policy_workspace_id IS DISTINCT FROM p_workspace_id
    ) THEN
        RAISE EXCEPTION 'catalog refresh completion ownership is invalid'
            USING ERRCODE = '55000';
    END IF;

    RETURN QUERY
    SELECT *
    FROM schemabridge_control.activate_catalog_generation(
        p_workspace_id,
        p_connection_id,
        p_refresh_id,
        p_expected_base_generation,
        p_target_generation,
        p_fencing_token,
        lease_capability_digest,
        p_inventory_fingerprint
    );
END;
$$;

CREATE FUNCTION schemabridge_control.load_owned_execution_connector_route(
    p_job_workspace_id varchar,
    p_connector_workspace_id varchar,
    p_job_id varchar,
    p_worker_id varchar,
    p_lease_capability varchar,
    p_fencing_token bigint,
    p_connection_id varchar,
    p_contract_version bigint,
    p_route_revision bigint,
    p_target_fingerprint char
)
RETURNS TABLE (
    job_workspace_id varchar(200),
    connector_workspace_id varchar(200),
    job_id varchar(200),
    connection_id varchar(200),
    contract_version bigint,
    route_revision bigint,
    target_fingerprint char(64),
    sql_dialect varchar(32),
    expected_reader varchar(63),
    source_identity_fingerprint char(64),
    credential_binding_ref varchar(200)
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
BEGIN
    IF SESSION_USER NOT IN ('schemabridge_worker', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'execution connector route role is invalid'
            USING ERRCODE = '42501';
    END IF;
    IF (
        p_job_workspace_id !~ '^[a-z0-9][a-z0-9_-]{2,199}$'
        OR octet_length(p_job_workspace_id) > 200
        OR p_connector_workspace_id !~ '^[a-z0-9][a-z0-9_-]{2,199}$'
        OR octet_length(p_connector_workspace_id) > 200
        OR p_job_id !~ '^[a-z0-9][a-z0-9_-]{2,199}$'
        OR p_worker_id IS NULL
        OR length(trim(p_worker_id)) NOT BETWEEN 1 AND 200
        OR octet_length(p_worker_id) > 200
        OR p_lease_capability IS NULL
        OR p_lease_capability <> trim(p_lease_capability)
        OR octet_length(p_lease_capability) NOT BETWEEN 32 AND 1024
        OR p_fencing_token < 1
        OR p_connection_id !~ '^[a-z][a-z0-9_-]{2,199}$'
        OR p_contract_version < 1
        OR p_route_revision < 1
        OR p_target_fingerprint !~ '^[0-9a-f]{64}$'
    ) THEN
        RAISE EXCEPTION 'execution connector route input is invalid'
            USING ERRCODE = '22023';
    END IF;

    RETURN QUERY
    SELECT
        job.workspace_id,
        job.connector_workspace_id,
        job.job_id,
        job.connector_connection_id,
        job.connector_contract_version,
        job.connector_route_revision,
        job.connector_target_fingerprint,
        contract.sql_dialect,
        contract.expected_reader,
        contract.source_identity_fingerprint,
        private.credential_binding_ref
    FROM schemabridge_control.execution_jobs AS job
    JOIN schemabridge_control.catalog_connections AS connection
      ON connection.workspace_id = job.connector_workspace_id
     AND connection.connection_id = job.connector_connection_id
    JOIN schemabridge_control.connector_route_heads AS head
      ON head.workspace_id = job.connector_workspace_id
     AND head.connection_id = job.connector_connection_id
     AND head.contract_version = job.connector_contract_version
     AND head.route_revision = job.connector_route_revision
     AND head.route_fingerprint = job.connector_route_fingerprint
     AND head.target_fingerprint = job.connector_target_fingerprint
    JOIN schemabridge_control.connector_contract_revisions AS contract
      ON contract.workspace_id = head.workspace_id
     AND contract.connection_id = head.connection_id
     AND contract.contract_version = head.contract_version
    JOIN schemabridge_control.catalog_generations AS generation
      ON generation.workspace_id = connection.workspace_id
     AND generation.connection_id = connection.connection_id
     AND generation.generation = connection.active_generation
     AND generation.status = 'completed'
     AND generation.source_identity_fingerprint
            = contract.source_identity_fingerprint
     AND generation.catalog_identity_fingerprint
            = contract.catalog_identity_fingerprint
     AND generation.type_contract_fingerprint
            = contract.type_contract_fingerprint
    JOIN schemabridge_control.connector_private_route_revisions AS private
      ON private.workspace_id = head.workspace_id
     AND private.connection_id = head.connection_id
     AND private.route_revision = head.route_revision
     AND private.capability = 'execution'
    WHERE job.workspace_id = p_job_workspace_id
      AND job.connector_workspace_id = p_connector_workspace_id
      AND job.job_id = p_job_id
      AND job.status = 'leased'
      AND job.lease_owner_id = p_worker_id
      AND job.lease_token_digest = encode(
          sha256(convert_to(p_lease_capability, 'UTF8')),
          'hex'
      )
      AND job.fencing_token = p_fencing_token
      AND job.lease_expires_at > clock_timestamp()
      AND job.authorization_expires_at > clock_timestamp()
      AND job.connector_connection_id = p_connection_id
      AND job.connector_contract_version = p_contract_version
      AND job.connector_route_revision = p_route_revision
      AND job.connector_target_fingerprint = p_target_fingerprint
      AND connection.status = 'enabled'
      AND head.status = 'enabled'
      AND contract.connector_kind = 'postgresql'
      AND contract.sql_dialect = 'postgresql'
      AND contract.type_contract_version = 1
      AND contract.type_contract_fingerprint
            = '07e1d8336a0dab019b080f114a1d31e082aed2b434ab84a1be59d98e2d000589';
END;
$$;

CREATE FUNCTION schemabridge_control.load_owned_profile_connector_route(
    p_workspace_id varchar,
    p_job_id varchar,
    p_worker_id varchar,
    p_lease_capability varchar,
    p_fencing_token bigint,
    p_connection_id varchar,
    p_contract_version bigint,
    p_route_revision bigint,
    p_target_fingerprint char
)
RETURNS TABLE (
    workspace_id varchar(200),
    job_id varchar(80),
    connection_id varchar(200),
    contract_version bigint,
    route_revision bigint,
    target_fingerprint char(64),
    sql_dialect varchar(32),
    expected_reader varchar(63),
    source_identity_fingerprint char(64),
    credential_binding_ref varchar(200)
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
BEGIN
    IF SESSION_USER NOT IN ('schemabridge_worker', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'profile connector route role is invalid'
            USING ERRCODE = '42501';
    END IF;
    IF (
        p_workspace_id !~ '^[a-z0-9][a-z0-9_-]{2,199}$'
        OR octet_length(p_workspace_id) > 200
        OR p_job_id !~ '^profile_job_[0-9a-f]{64}$'
        OR p_worker_id !~ '^[a-z][a-z0-9_-]{2,199}$'
        OR p_lease_capability IS NULL
        OR p_lease_capability <> trim(p_lease_capability)
        OR octet_length(p_lease_capability) NOT BETWEEN 32 AND 1024
        OR p_fencing_token < 1
        OR p_connection_id !~ '^[a-z][a-z0-9_-]{2,199}$'
        OR p_contract_version < 1
        OR p_route_revision < 1
        OR p_target_fingerprint !~ '^[0-9a-f]{64}$'
    ) THEN
        RAISE EXCEPTION 'profile connector route input is invalid'
            USING ERRCODE = '22023';
    END IF;

    RETURN QUERY
    SELECT
        job.workspace_id,
        job.job_id,
        job.connection_id,
        job.connector_contract_version,
        job.connector_route_revision,
        job.connector_target_fingerprint,
        contract.sql_dialect,
        contract.expected_reader,
        contract.source_identity_fingerprint,
        private.credential_binding_ref
    FROM schemabridge_control.semantic_join_profile_jobs AS job
    JOIN schemabridge_control.catalog_connections AS connection
      ON connection.workspace_id = job.workspace_id
     AND connection.connection_id = job.connection_id
    JOIN schemabridge_control.connector_route_heads AS head
      ON head.workspace_id = job.workspace_id
     AND head.connection_id = job.connection_id
     AND head.contract_version = job.connector_contract_version
     AND head.route_revision = job.connector_route_revision
     AND head.route_fingerprint = job.connector_route_fingerprint
     AND head.target_fingerprint = job.connector_target_fingerprint
    JOIN schemabridge_control.connector_contract_revisions AS contract
      ON contract.workspace_id = head.workspace_id
     AND contract.connection_id = head.connection_id
     AND contract.contract_version = head.contract_version
    JOIN schemabridge_control.catalog_generations AS generation
      ON generation.workspace_id = connection.workspace_id
     AND generation.connection_id = connection.connection_id
     AND generation.generation = connection.active_generation
     AND generation.status = 'completed'
     AND generation.source_identity_fingerprint
            = contract.source_identity_fingerprint
     AND generation.catalog_identity_fingerprint
            = contract.catalog_identity_fingerprint
     AND generation.type_contract_fingerprint
            = contract.type_contract_fingerprint
    JOIN schemabridge_control.connector_private_route_revisions AS private
      ON private.workspace_id = head.workspace_id
     AND private.connection_id = head.connection_id
     AND private.route_revision = head.route_revision
     AND private.capability = 'profile'
    WHERE job.workspace_id = p_workspace_id
      AND job.job_id = p_job_id
      AND job.status = 'leased'
      AND job.lease_owner_id = p_worker_id
      AND job.lease_capability_digest = encode(
          sha256(convert_to(p_lease_capability, 'UTF8')),
          'hex'
      )
      AND job.fencing_token = p_fencing_token
      AND job.lease_expires_at > clock_timestamp()
      AND job.connection_id = p_connection_id
      AND job.connector_contract_version = p_contract_version
      AND job.connector_route_revision = p_route_revision
      AND job.connector_target_fingerprint = p_target_fingerprint
      AND connection.status = 'enabled'
      AND head.status = 'enabled'
      AND contract.connector_kind = 'postgresql'
      AND contract.sql_dialect = 'postgresql'
      AND contract.type_contract_version = 1
      AND contract.type_contract_fingerprint
            = '07e1d8336a0dab019b080f114a1d31e082aed2b434ab84a1be59d98e2d000589';
END;
$$;

REVOKE INSERT ON
    schemabridge_control.catalog_connection_routes
    FROM schemabridge_api;

REVOKE EXECUTE ON FUNCTION
    schemabridge_control.load_owned_catalog_connection_route(
        varchar,
        varchar,
        varchar,
        varchar,
        varchar,
        bigint
    )
    FROM schemabridge_catalog, schemabridge_migrator;

REVOKE EXECUTE ON FUNCTION
    schemabridge_control.activate_catalog_generation(
        varchar,
        varchar,
        varchar,
        bigint,
        bigint,
        bigint,
        char,
        char
    )
    FROM schemabridge_catalog;

-- A newly indexed tenant generation can cross the activation boundary before
-- autovacuum has collected planner statistics.  The v5 change-capture FULL
-- joins then have a valid hash/merge path but can otherwise select a
-- catastrophically underestimated nested-loop plan.  Keep this setting local
-- to the trigger function; callers retain their own planner configuration.
ALTER FUNCTION schemabridge_control.capture_catalog_generation_change()
    SET enable_nestloop = off;

-- The v4 activation primitive is an internal implementation detail. Both it
-- and the only public v9 wrapper have the same non-runtime technical owner so
-- SECURITY DEFINER can compose them without granting catalog a direct bypass.
ALTER FUNCTION schemabridge_control.activate_catalog_generation(
    varchar,
    varchar,
    varchar,
    bigint,
    bigint,
    bigint,
    char,
    char
) OWNER TO schemabridge_migrator;

ALTER FUNCTION schemabridge_control.activate_catalog_generation_for_target(
    varchar,
    varchar,
    varchar,
    varchar,
    varchar,
    bigint,
    bigint,
    bigint,
    char,
    bigint,
    bigint,
    char
) OWNER TO schemabridge_migrator;

REVOKE ALL ON
    schemabridge_control.catalog_refresh_semantic_bindings,
    schemabridge_control.connector_contract_revisions,
    schemabridge_control.connector_route_revisions,
    schemabridge_control.connector_private_route_revisions,
    schemabridge_control.connector_route_heads,
    schemabridge_control.connector_route_audit
    FROM PUBLIC;

REVOKE ALL ON FUNCTION
    schemabridge_control.resolve_query_studio_physical_type(
        varchar,
        varchar
    ),
    schemabridge_control.connector_canonical_cost(numeric),
    schemabridge_control.connector_cost_budget_fingerprint(
        integer,
        integer,
        numeric,
        bigint,
        integer,
        integer,
        integer
    ),
    schemabridge_control.connector_target_fingerprint(
        varchar,
        varchar,
        bigint,
        char,
        varchar,
        char,
        char,
        char,
        integer,
        integer,
        numeric,
        bigint,
        integer,
        integer,
        integer,
        char
    ),
    schemabridge_control.guard_catalog_generation_semantic_identity(),
    schemabridge_control.reject_connector_immutable_mutation(),
    schemabridge_control.guard_connector_route_head(),
    schemabridge_control.guard_execution_job_connector_target(),
    schemabridge_control.guard_profile_job_connector_target(),
    schemabridge_control.apply_connector_route_change(
        varchar,
        varchar,
        varchar,
        bigint,
        bigint,
        bigint,
        char,
        varchar,
        integer,
        char,
        char,
        char,
        integer,
        integer,
        numeric,
        bigint,
        integer,
        integer,
        integer,
        char,
        char,
        char,
        varchar,
        varchar,
        varchar,
        varchar,
        char,
        varchar,
        char,
        varchar,
        char,
        varchar,
        char,
        char,
        varchar
    ),
    schemabridge_control.load_current_connector_target(varchar, varchar),
    schemabridge_control.load_current_preflight_connector_route(
        varchar,
        varchar,
        bigint,
        char
    ),
    schemabridge_control.load_owned_catalog_connector_route(
        varchar,
        varchar,
        varchar,
        varchar,
        varchar,
        bigint,
        bigint,
        bigint,
        char
    ),
    schemabridge_control.activate_catalog_generation_for_target(
        varchar,
        varchar,
        varchar,
        varchar,
        varchar,
        bigint,
        bigint,
        bigint,
        char,
        bigint,
        bigint,
        char
    ),
    schemabridge_control.load_owned_execution_connector_route(
        varchar,
        varchar,
        varchar,
        varchar,
        varchar,
        bigint,
        varchar,
        bigint,
        bigint,
        char
    ),
    schemabridge_control.load_owned_profile_connector_route(
        varchar,
        varchar,
        varchar,
        varchar,
        bigint,
        varchar,
        bigint,
        bigint,
        char
    )
    FROM PUBLIC;

GRANT INSERT (
    connector_workspace_id,
    connector_connection_id,
    connector_route_revision,
    connector_route_fingerprint,
    connector_target_fingerprint
) ON schemabridge_control.execution_jobs
    TO schemabridge_api;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.apply_connector_route_change(
        varchar,
        varchar,
        varchar,
        bigint,
        bigint,
        bigint,
        char,
        varchar,
        integer,
        char,
        char,
        char,
        integer,
        integer,
        numeric,
        bigint,
        integer,
        integer,
        integer,
        char,
        char,
        char,
        varchar,
        varchar,
        varchar,
        varchar,
        char,
        varchar,
        char,
        varchar,
        char,
        varchar,
        char,
        char,
        varchar
    )
    TO schemabridge_migrator;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.load_current_connector_target(varchar, varchar)
    TO
        schemabridge_runtime,
        schemabridge_reconciler,
        schemabridge_catalog,
        schemabridge_migrator;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.load_current_preflight_connector_route(
        varchar,
        varchar,
        bigint,
        char
    )
    TO schemabridge_runtime, schemabridge_migrator;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.load_owned_catalog_connector_route(
        varchar,
        varchar,
        varchar,
        varchar,
        varchar,
        bigint,
        bigint,
        bigint,
        char
    )
    TO schemabridge_catalog, schemabridge_migrator;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.activate_catalog_generation_for_target(
        varchar,
        varchar,
        varchar,
        varchar,
        varchar,
        bigint,
        bigint,
        bigint,
        char,
        bigint,
        bigint,
        char
    )
    TO schemabridge_catalog, schemabridge_migrator;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.load_owned_execution_connector_route(
        varchar,
        varchar,
        varchar,
        varchar,
        varchar,
        bigint,
        varchar,
        bigint,
        bigint,
        char
    ),
    schemabridge_control.load_owned_profile_connector_route(
        varchar,
        varchar,
        varchar,
        varchar,
        bigint,
        varchar,
        bigint,
        bigint,
        char
    )
    TO schemabridge_worker, schemabridge_migrator;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.reject_connector_immutable_mutation(),
    schemabridge_control.guard_connector_route_head(),
    schemabridge_control.guard_execution_job_connector_target(),
    schemabridge_control.guard_profile_job_connector_target()
    TO schemabridge_migrator;
