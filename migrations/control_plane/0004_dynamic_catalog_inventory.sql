GRANT USAGE ON SCHEMA schemabridge_control
    TO schemabridge_catalog;

CREATE TABLE schemabridge_control.tenant_capacity_policies (
    workspace_id varchar(200) PRIMARY KEY
        CHECK (
            length(trim(workspace_id)) BETWEEN 1 AND 200
            AND octet_length(workspace_id) <= 200
        ),
    connection_limit integer NOT NULL
        CHECK (connection_limit BETWEEN 1 AND 100000),
    asset_limit bigint NOT NULL
        CHECK (asset_limit BETWEEN 1 AND 100000000),
    field_limit bigint NOT NULL
        CHECK (field_limit BETWEEN 1 AND 1000000000),
    api_requests_per_minute integer NOT NULL
        CHECK (api_requests_per_minute BETWEEN 1 AND 1000000),
    api_window_seconds integer NOT NULL DEFAULT 60
        CHECK (api_window_seconds = 60),
    nonterminal_job_limit integer NOT NULL
        CHECK (nonterminal_job_limit BETWEEN 1 AND 1000000),
    catalog_cursor_ttl_seconds integer NOT NULL DEFAULT 900
        CHECK (catalog_cursor_ttl_seconds = 900),
    generation_retention_seconds integer NOT NULL DEFAULT 1800
        CHECK (
            generation_retention_seconds BETWEEN 900 AND 2592000
            AND generation_retention_seconds >= catalog_cursor_ttl_seconds
        ),
    version bigint NOT NULL CHECK (version >= 1),
    updated_by varchar(200) NOT NULL
        CHECK (
            length(trim(updated_by)) BETWEEN 1 AND 200
            AND octet_length(updated_by) <= 200
        ),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CHECK (updated_at >= created_at)
);

CREATE TABLE schemabridge_control.tenant_capacity_policy_revisions (
    workspace_id varchar(200) NOT NULL,
    version bigint NOT NULL CHECK (version >= 1),
    connection_limit integer NOT NULL
        CHECK (connection_limit BETWEEN 1 AND 100000),
    asset_limit bigint NOT NULL
        CHECK (asset_limit BETWEEN 1 AND 100000000),
    field_limit bigint NOT NULL
        CHECK (field_limit BETWEEN 1 AND 1000000000),
    api_requests_per_minute integer NOT NULL
        CHECK (api_requests_per_minute BETWEEN 1 AND 1000000),
    nonterminal_job_limit integer NOT NULL
        CHECK (nonterminal_job_limit BETWEEN 1 AND 1000000),
    catalog_cursor_ttl_seconds integer NOT NULL
        CHECK (catalog_cursor_ttl_seconds = 900),
    generation_retention_seconds integer NOT NULL
        CHECK (
            generation_retention_seconds BETWEEN 900 AND 2592000
            AND generation_retention_seconds >= catalog_cursor_ttl_seconds
        ),
    updated_by varchar(200) NOT NULL
        CHECK (
            length(trim(updated_by)) BETWEEN 1 AND 200
            AND octet_length(updated_by) <= 200
        ),
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, version),
    CONSTRAINT tenant_capacity_policy_revisions_policy_fk
        FOREIGN KEY (workspace_id)
        REFERENCES schemabridge_control.tenant_capacity_policies (workspace_id)
);

CREATE TABLE schemabridge_control.api_rate_limit_windows (
    workspace_id varchar(200) NOT NULL
        CHECK (
            length(trim(workspace_id)) BETWEEN 1 AND 200
            AND octet_length(workspace_id) <= 200
        ),
    principal_digest char(64) NOT NULL
        CHECK (principal_digest ~ '^[0-9a-f]{64}$'),
    operation_scope varchar(80) NOT NULL
        CHECK (operation_scope ~ '^[a-z][a-z0-9_:-]{1,79}$'),
    window_started_at timestamptz NOT NULL,
    window_expires_at timestamptz NOT NULL,
    request_count integer NOT NULL CHECK (request_count >= 1),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (
        workspace_id,
        principal_digest,
        operation_scope,
        window_started_at
    ),
    CHECK (window_expires_at > window_started_at),
    CHECK (updated_at >= created_at)
);

CREATE INDEX api_rate_limit_windows_expiry_idx
    ON schemabridge_control.api_rate_limit_windows (
        window_expires_at,
        workspace_id,
        principal_digest
    );

CREATE TABLE schemabridge_control.tenant_execution_capacity (
    workspace_id varchar(200) PRIMARY KEY
        CHECK (
            length(trim(workspace_id)) BETWEEN 1 AND 200
            AND octet_length(workspace_id) <= 200
        ),
    nonterminal_job_count integer NOT NULL
        CHECK (nonterminal_job_count >= 0),
    capacity_version bigint NOT NULL CHECK (capacity_version >= 0),
    updated_at timestamptz NOT NULL
);

INSERT INTO schemabridge_control.tenant_execution_capacity (
    workspace_id,
    nonterminal_job_count,
    capacity_version,
    updated_at
)
SELECT
    workspace_id,
    count(*)::integer,
    0,
    clock_timestamp()
FROM schemabridge_control.execution_jobs
WHERE status IN ('queued', 'leased', 'retry_wait', 'cancel_requested')
GROUP BY workspace_id;

CREATE TABLE schemabridge_control.tenant_job_schedule (
    workspace_id varchar(200) PRIMARY KEY
        CHECK (
            length(trim(workspace_id)) BETWEEN 1 AND 200
            AND octet_length(workspace_id) <= 200
        ),
    last_claimed_at timestamptz,
    claim_sequence bigint NOT NULL CHECK (claim_sequence >= 0),
    updated_at timestamptz NOT NULL
);

INSERT INTO schemabridge_control.tenant_job_schedule (
    workspace_id,
    last_claimed_at,
    claim_sequence,
    updated_at
)
SELECT DISTINCT
    workspace_id,
    NULL::timestamptz,
    0,
    clock_timestamp()
FROM schemabridge_control.execution_jobs;

CREATE INDEX execution_jobs_fair_claim_idx
    ON schemabridge_control.execution_jobs (
        workspace_id,
        available_at,
        created_at,
        job_id
    )
    WHERE status IN ('queued', 'retry_wait');

CREATE TABLE schemabridge_control.catalog_connections (
    workspace_id varchar(200) NOT NULL
        CHECK (
            length(trim(workspace_id)) BETWEEN 1 AND 200
            AND octet_length(workspace_id) <= 200
        ),
    connection_id varchar(200) NOT NULL
        CHECK (connection_id ~ '^[a-z][a-z0-9_-]{2,199}$'),
    display_name varchar(200) NOT NULL
        CHECK (
            length(trim(display_name)) BETWEEN 1 AND 200
            AND octet_length(display_name) <= 400
        ),
    source_kind varchar(32) NOT NULL
        CHECK (source_kind IN ('datahub_graphql', 'synthetic')),
    catalog_scope varchar(200) NOT NULL
        CHECK (
            length(trim(catalog_scope)) BETWEEN 1 AND 200
            AND octet_length(catalog_scope) <= 400
        ),
    environment varchar(80) NOT NULL
        CHECK (environment ~ '^[A-Za-z][A-Za-z0-9_.-]{0,79}$'),
    platform_instance varchar(200)
        CHECK (
            platform_instance IS NULL
            OR (
                length(trim(platform_instance)) BETWEEN 1 AND 200
                AND octet_length(platform_instance) <= 400
            )
        ),
    status varchar(16) NOT NULL CHECK (status IN ('enabled', 'disabled')),
    active_generation bigint CHECK (active_generation > 0),
    active_generation_fingerprint char(64)
        CHECK (
            active_generation_fingerprint IS NULL
            OR active_generation_fingerprint ~ '^[0-9a-f]{64}$'
        ),
    active_generation_completed_at timestamptz,
    registration_fingerprint char(64) NOT NULL
        CHECK (registration_fingerprint ~ '^[0-9a-f]{64}$'),
    idempotency_digest char(64) NOT NULL
        CHECK (idempotency_digest ~ '^[0-9a-f]{64}$'),
    created_by_actor_id varchar(200) NOT NULL
        CHECK (
            length(trim(created_by_actor_id)) BETWEEN 1 AND 200
            AND octet_length(created_by_actor_id) <= 200
        ),
    disabled_by_actor_id varchar(200)
        CHECK (
            disabled_by_actor_id IS NULL
            OR (
                length(trim(disabled_by_actor_id)) BETWEEN 1 AND 200
                AND octet_length(disabled_by_actor_id) <= 200
            )
        ),
    disabled_idempotency_digest char(64)
        CHECK (
            disabled_idempotency_digest IS NULL
            OR disabled_idempotency_digest ~ '^[0-9a-f]{64}$'
        ),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    disabled_at timestamptz,
    PRIMARY KEY (workspace_id, connection_id),
    UNIQUE (workspace_id, idempotency_digest),
    CHECK (updated_at >= created_at),
    CONSTRAINT catalog_connections_status_shape
        CHECK (
            (
                status = 'enabled'
                AND disabled_by_actor_id IS NULL
                AND disabled_idempotency_digest IS NULL
                AND disabled_at IS NULL
            )
            OR
            (
                status = 'disabled'
                AND disabled_by_actor_id IS NOT NULL
                AND disabled_idempotency_digest IS NOT NULL
                AND disabled_at IS NOT NULL
                AND disabled_at >= created_at
            )
        ),
    CONSTRAINT catalog_connections_generation_shape
        CHECK (
            (
                active_generation IS NULL
                AND active_generation_fingerprint IS NULL
                AND active_generation_completed_at IS NULL
            )
            OR
            (
                active_generation IS NOT NULL
                AND active_generation_fingerprint IS NOT NULL
                AND active_generation_completed_at IS NOT NULL
            )
        ),
    CONSTRAINT catalog_connections_policy_fk
        FOREIGN KEY (workspace_id)
        REFERENCES schemabridge_control.tenant_capacity_policies (workspace_id)
);

CREATE INDEX catalog_connections_page_idx
    ON schemabridge_control.catalog_connections (
        workspace_id,
        status,
        display_name,
        connection_id
    );

CREATE INDEX catalog_connections_all_page_idx
    ON schemabridge_control.catalog_connections (
        workspace_id,
        display_name,
        connection_id
    );

CREATE TABLE schemabridge_control.catalog_connection_routes (
    workspace_id varchar(200) NOT NULL,
    connection_id varchar(200) NOT NULL,
    credential_binding_ref varchar(200) NOT NULL
        CHECK (
            credential_binding_ref ~ '^[a-z][a-z0-9._:-]{2,199}$'
            AND credential_binding_ref !~ '://'
            AND credential_binding_ref !~ '@'
        ),
    route_fingerprint char(64) NOT NULL
        CHECK (route_fingerprint ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, connection_id),
    CONSTRAINT catalog_connection_routes_connection_fk
        FOREIGN KEY (workspace_id, connection_id)
        REFERENCES schemabridge_control.catalog_connections (
            workspace_id,
            connection_id
        )
);

CREATE TABLE schemabridge_control.catalog_refresh_runs (
    workspace_id varchar(200) NOT NULL,
    connection_id varchar(200) NOT NULL,
    refresh_id varchar(200) NOT NULL
        CHECK (refresh_id ~ '^[a-z][a-z0-9_-]{2,199}$'),
    refresh_mode varchar(16) NOT NULL
        CHECK (refresh_mode IN ('full', 'delta')),
    status varchar(16) NOT NULL
        CHECK (status IN ('requested', 'leased', 'staging', 'completed', 'failed')),
    base_generation bigint CHECK (base_generation > 0),
    target_generation bigint NOT NULL CHECK (target_generation > 0),
    request_fingerprint char(64) NOT NULL
        CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    idempotency_digest char(64) NOT NULL
        CHECK (idempotency_digest ~ '^[0-9a-f]{64}$'),
    requested_by_actor_id varchar(200) NOT NULL
        CHECK (
            length(trim(requested_by_actor_id)) BETWEEN 1 AND 200
            AND octet_length(requested_by_actor_id) <= 200
        ),
    lease_owner_id varchar(200)
        CHECK (
            lease_owner_id IS NULL
            OR lease_owner_id ~ '^[a-z][a-z0-9_.:-]{2,199}$'
        ),
    lease_capability_digest char(64)
        CHECK (
            lease_capability_digest IS NULL
            OR lease_capability_digest ~ '^[0-9a-f]{64}$'
        ),
    fencing_token bigint NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
    lease_acquired_at timestamptz,
    lease_heartbeat_at timestamptz,
    lease_expires_at timestamptz,
    source_checkpoint varchar(1024)
        CHECK (
            source_checkpoint IS NULL
            OR (
                length(source_checkpoint) BETWEEN 1 AND 1024
                AND octet_length(source_checkpoint) <= 4096
            )
        ),
    source_page_number bigint NOT NULL DEFAULT 0
        CHECK (source_page_number >= 0),
    source_page_fingerprint char(64)
        CHECK (
            source_page_fingerprint IS NULL
            OR source_page_fingerprint ~ '^[0-9a-f]{64}$'
        ),
    staged_asset_count bigint NOT NULL DEFAULT 0
        CHECK (staged_asset_count >= 0),
    staged_field_count bigint NOT NULL DEFAULT 0
        CHECK (staged_field_count >= 0),
    source_complete boolean NOT NULL DEFAULT false,
    inventory_fingerprint char(64)
        CHECK (
            inventory_fingerprint IS NULL
            OR inventory_fingerprint ~ '^[0-9a-f]{64}$'
        ),
    failure_code varchar(64)
        CHECK (
            failure_code IS NULL
            OR failure_code ~ '^[a-z][a-z0-9_]{1,63}$'
        ),
    requested_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    completed_at timestamptz,
    PRIMARY KEY (workspace_id, connection_id, refresh_id),
    UNIQUE (workspace_id, refresh_id),
    UNIQUE (workspace_id, connection_id, idempotency_digest),
    UNIQUE (workspace_id, connection_id, target_generation),
    CONSTRAINT catalog_refresh_connection_fk
        FOREIGN KEY (workspace_id, connection_id)
        REFERENCES schemabridge_control.catalog_connections (
            workspace_id,
            connection_id
        ),
    CHECK (updated_at >= requested_at),
    CHECK (base_generation IS NULL OR target_generation <> base_generation),
    CONSTRAINT catalog_refresh_lease_shape
        CHECK (
            (
                status IN ('leased', 'staging')
                AND lease_owner_id IS NOT NULL
                AND lease_capability_digest IS NOT NULL
                AND lease_acquired_at IS NOT NULL
                AND lease_heartbeat_at IS NOT NULL
                AND lease_expires_at IS NOT NULL
                AND lease_expires_at > lease_heartbeat_at
                AND completed_at IS NULL
                AND failure_code IS NULL
                AND inventory_fingerprint IS NULL
            )
            OR
            (
                status = 'requested'
                AND lease_owner_id IS NULL
                AND lease_capability_digest IS NULL
                AND lease_acquired_at IS NULL
                AND lease_heartbeat_at IS NULL
                AND lease_expires_at IS NULL
                AND completed_at IS NULL
                AND failure_code IS NULL
                AND inventory_fingerprint IS NULL
            )
            OR
            (
                status = 'completed'
                AND target_generation IS NOT NULL
                AND lease_capability_digest IS NULL
                AND lease_expires_at IS NULL
                AND source_complete
                AND inventory_fingerprint IS NOT NULL
                AND completed_at IS NOT NULL
                AND failure_code IS NULL
            )
            OR
            (
                status = 'failed'
                AND lease_capability_digest IS NULL
                AND lease_expires_at IS NULL
                AND inventory_fingerprint IS NULL
                AND completed_at IS NOT NULL
                AND failure_code IS NOT NULL
            )
        )
);

CREATE UNIQUE INDEX catalog_refresh_one_active_idx
    ON schemabridge_control.catalog_refresh_runs (
        workspace_id,
        connection_id
    )
    WHERE status IN ('requested', 'leased', 'staging');

CREATE INDEX catalog_refresh_claim_idx
    ON schemabridge_control.catalog_refresh_runs (
        requested_at,
        workspace_id,
        connection_id,
        refresh_id
    )
    WHERE status = 'requested';

CREATE INDEX catalog_refresh_expired_lease_idx
    ON schemabridge_control.catalog_refresh_runs (
        lease_expires_at,
        workspace_id,
        connection_id,
        refresh_id
    )
    WHERE status IN ('leased', 'staging');

CREATE TABLE schemabridge_control.catalog_generations (
    workspace_id varchar(200) NOT NULL,
    connection_id varchar(200) NOT NULL,
    generation bigint NOT NULL CHECK (generation > 0),
    refresh_id varchar(200) NOT NULL,
    base_generation bigint CHECK (base_generation > 0),
    refresh_mode varchar(16) NOT NULL
        CHECK (refresh_mode IN ('full', 'delta')),
    status varchar(16) NOT NULL
        CHECK (status IN ('staging', 'completed')),
    asset_count bigint CHECK (asset_count >= 0),
    field_count bigint CHECK (field_count >= 0),
    inventory_fingerprint char(64)
        CHECK (
            inventory_fingerprint IS NULL
            OR inventory_fingerprint ~ '^[0-9a-f]{64}$'
        ),
    created_at timestamptz NOT NULL,
    completed_at timestamptz,
    retain_until timestamptz,
    PRIMARY KEY (workspace_id, connection_id, generation),
    UNIQUE (workspace_id, connection_id, refresh_id),
    CONSTRAINT catalog_generations_connection_fk
        FOREIGN KEY (workspace_id, connection_id)
        REFERENCES schemabridge_control.catalog_connections (
            workspace_id,
            connection_id
        ),
    CONSTRAINT catalog_generations_refresh_fk
        FOREIGN KEY (workspace_id, connection_id, refresh_id)
        REFERENCES schemabridge_control.catalog_refresh_runs (
            workspace_id,
            connection_id,
            refresh_id
        ),
    CONSTRAINT catalog_generations_completion_shape
        CHECK (
            (
                status = 'staging'
                AND asset_count IS NULL
                AND field_count IS NULL
                AND inventory_fingerprint IS NULL
                AND completed_at IS NULL
                AND retain_until IS NULL
            )
            OR
            (
                status = 'completed'
                AND asset_count IS NOT NULL
                AND field_count IS NOT NULL
                AND inventory_fingerprint IS NOT NULL
                AND completed_at IS NOT NULL
                AND retain_until IS NOT NULL
                AND retain_until >= completed_at
            )
        )
);

ALTER TABLE schemabridge_control.catalog_connections
    ADD CONSTRAINT catalog_connections_active_generation_fk
    FOREIGN KEY (workspace_id, connection_id, active_generation)
    REFERENCES schemabridge_control.catalog_generations (
        workspace_id,
        connection_id,
        generation
    );

CREATE INDEX catalog_generations_retention_idx
    ON schemabridge_control.catalog_generations (
        retain_until,
        workspace_id,
        connection_id,
        generation
    )
    WHERE status = 'completed';

CREATE TABLE schemabridge_control.catalog_assets (
    workspace_id varchar(200) NOT NULL,
    connection_id varchar(200) NOT NULL,
    generation bigint NOT NULL,
    asset_key char(64) NOT NULL CHECK (asset_key ~ '^[0-9a-f]{64}$'),
    asset_id varchar(500) NOT NULL
        CHECK (
            length(asset_id) BETWEEN 1 AND 500
            AND octet_length(asset_id) <= 2000
        ),
    qualified_name varchar(500) NOT NULL
        CHECK (
            length(qualified_name) BETWEEN 1 AND 500
            AND octet_length(qualified_name) <= 2000
        ),
    asset_sort_key varchar(512) NOT NULL
        CHECK (
            length(asset_sort_key) BETWEEN 1 AND 512
            AND octet_length(asset_sort_key) <= 512
        ),
    platform varchar(100) NOT NULL
        CHECK (
            length(platform) BETWEEN 1 AND 100
            AND octet_length(platform) <= 400
        ),
    environment varchar(100) NOT NULL
        CHECK (
            length(environment) BETWEEN 1 AND 100
            AND octet_length(environment) <= 400
        ),
    database_name varchar(200)
        CHECK (
            database_name IS NULL
            OR (
                length(database_name) BETWEEN 1 AND 200
                AND octet_length(database_name) <= 400
            )
        ),
    schema_name varchar(200)
        CHECK (
            schema_name IS NULL
            OR (
                length(schema_name) BETWEEN 1 AND 200
                AND octet_length(schema_name) <= 400
            )
        ),
    table_name varchar(200)
        CHECK (
            table_name IS NULL
            OR (
                length(table_name) BETWEEN 1 AND 200
                AND octet_length(table_name) <= 400
            )
        ),
    display_name varchar(200) NOT NULL
        CHECK (
            length(trim(display_name)) BETWEEN 1 AND 200
            AND octet_length(display_name) <= 400
        ),
    description varchar(4000)
        CHECK (
            description IS NULL
            OR octet_length(description) <= 16000
        ),
    field_count bigint NOT NULL CHECK (field_count BETWEEN 0 AND 1000000000),
    metadata_fingerprint char(64) NOT NULL
        CHECK (metadata_fingerprint ~ '^[0-9a-f]{64}$'),
    observed_at timestamptz NOT NULL,
    search_document tsvector GENERATED ALWAYS AS (
        to_tsvector(
            'pg_catalog.simple'::regconfig,
            coalesce(asset_id, '')
                || ' '
                || coalesce(qualified_name, '')
                || ' '
                || coalesce(display_name, '')
                || ' '
                || coalesce(schema_name, '')
                || ' '
                || coalesce(table_name, '')
                || ' '
                || coalesce(description, '')
        )
    ) STORED,
    PRIMARY KEY (workspace_id, connection_id, generation, asset_key),
    UNIQUE (workspace_id, connection_id, generation, asset_id),
    CONSTRAINT catalog_assets_generation_fk
        FOREIGN KEY (workspace_id, connection_id, generation)
        REFERENCES schemabridge_control.catalog_generations (
            workspace_id,
            connection_id,
            generation
        )
        ON DELETE CASCADE
);

CREATE INDEX catalog_assets_keyset_idx
    ON schemabridge_control.catalog_assets (
        workspace_id,
        connection_id,
        generation,
        asset_sort_key,
        asset_key
    );

CREATE INDEX catalog_assets_exact_lookup_idx
    ON schemabridge_control.catalog_assets (
        workspace_id,
        connection_id,
        generation,
        schema_name,
        table_name,
        asset_key
    );

CREATE INDEX catalog_assets_platform_filter_idx
    ON schemabridge_control.catalog_assets (
        workspace_id,
        connection_id,
        generation,
        lower(platform),
        asset_sort_key,
        asset_key
    );

CREATE INDEX catalog_assets_schema_filter_idx
    ON schemabridge_control.catalog_assets (
        workspace_id,
        connection_id,
        generation,
        lower(schema_name),
        asset_sort_key,
        asset_key
    );

CREATE INDEX catalog_assets_search_idx
    ON schemabridge_control.catalog_assets
    USING gin (search_document);

CREATE FUNCTION schemabridge_control.catalog_terms_text(varchar[])
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog
AS $$
    SELECT pg_catalog.array_to_string($1, ' ');
$$;

CREATE TABLE schemabridge_control.catalog_fields (
    workspace_id varchar(200) NOT NULL,
    connection_id varchar(200) NOT NULL,
    generation bigint NOT NULL,
    asset_key char(64) NOT NULL CHECK (asset_key ~ '^[0-9a-f]{64}$'),
    field_key char(64) NOT NULL CHECK (field_key ~ '^[0-9a-f]{64}$'),
    field_path varchar(200)[] NOT NULL
        CHECK (
            cardinality(field_path) BETWEEN 1 AND 64
            AND array_position(field_path, NULL) IS NULL
            AND octet_length(array_to_string(field_path, '.')) <= 12800
        ),
    field_sort_key varchar(512) NOT NULL
        CHECK (
            length(field_sort_key) BETWEEN 1 AND 512
            AND octet_length(field_sort_key) <= 512
        ),
    field_name varchar(200) NOT NULL
        CHECK (
            length(field_name) BETWEEN 1 AND 200
            AND octet_length(field_name) <= 400
        ),
    ordinal_position integer NOT NULL
        CHECK (ordinal_position BETWEEN 0 AND 1000000),
    native_type varchar(200)
        CHECK (
            native_type IS NULL
            OR (
                length(native_type) BETWEEN 1 AND 200
                AND octet_length(native_type) <= 400
            )
        ),
    normalized_type varchar(32)
        CHECK (
            normalized_type IS NULL
            OR normalized_type IN (
                'string',
                'integer',
                'decimal',
                'float',
                'boolean',
                'date',
                'timestamp',
                'binary',
                'struct',
                'array',
                'unknown'
            )
        ),
    nullable boolean,
    is_part_of_key boolean,
    tags varchar(200)[] NOT NULL DEFAULT '{}'
        CHECK (
            cardinality(tags) <= 100
            AND array_position(tags, NULL) IS NULL
            AND octet_length(array_to_string(tags, '')) <= 20000
        ),
    glossary_terms varchar(200)[] NOT NULL DEFAULT '{}'
        CHECK (
            cardinality(glossary_terms) <= 100
            AND array_position(glossary_terms, NULL) IS NULL
            AND octet_length(array_to_string(glossary_terms, '')) <= 20000
        ),
    description varchar(4000)
        CHECK (
            description IS NULL
            OR octet_length(description) <= 16000
        ),
    metadata_fingerprint char(64) NOT NULL
        CHECK (metadata_fingerprint ~ '^[0-9a-f]{64}$'),
    observed_at timestamptz NOT NULL,
    search_document tsvector GENERATED ALWAYS AS (
        to_tsvector(
            'pg_catalog.simple'::regconfig,
            coalesce(field_name, '')
                || ' '
                || coalesce(description, '')
                || ' '
                || coalesce(native_type, '')
                || ' '
                || coalesce(schemabridge_control.catalog_terms_text(tags), '')
                || ' '
                || coalesce(
                    schemabridge_control.catalog_terms_text(glossary_terms),
                    ''
                )
        )
    ) STORED,
    PRIMARY KEY (
        workspace_id,
        connection_id,
        generation,
        asset_key,
        field_key
    ),
    UNIQUE (
        workspace_id,
        connection_id,
        generation,
        asset_key,
        field_path
    ),
    CONSTRAINT catalog_fields_asset_fk
        FOREIGN KEY (
            workspace_id,
            connection_id,
            generation,
            asset_key
        )
        REFERENCES schemabridge_control.catalog_assets (
            workspace_id,
            connection_id,
            generation,
            asset_key
        )
        ON DELETE CASCADE
);

CREATE INDEX catalog_fields_keyset_idx
    ON schemabridge_control.catalog_fields (
        workspace_id,
        connection_id,
        generation,
        asset_key,
        field_sort_key,
        field_key
    );

CREATE INDEX catalog_fields_exact_lookup_idx
    ON schemabridge_control.catalog_fields (
        workspace_id,
        connection_id,
        generation,
        lower(field_name),
        field_key
    );

CREATE INDEX catalog_fields_native_type_filter_idx
    ON schemabridge_control.catalog_fields (
        workspace_id,
        connection_id,
        generation,
        asset_key,
        lower(native_type),
        field_sort_key,
        field_key
    );

CREATE INDEX catalog_fields_search_idx
    ON schemabridge_control.catalog_fields
    USING gin (search_document);

CREATE TABLE schemabridge_control.catalog_tombstones (
    workspace_id varchar(200) NOT NULL,
    connection_id varchar(200) NOT NULL,
    observed_missing_in_generation bigint NOT NULL
        CHECK (observed_missing_in_generation > 0),
    resource_kind varchar(16) NOT NULL
        CHECK (resource_kind IN ('asset', 'field')),
    asset_key char(64) NOT NULL CHECK (asset_key ~ '^[0-9a-f]{64}$'),
    asset_id varchar(500) NOT NULL
        CHECK (
            length(asset_id) BETWEEN 1 AND 500
            AND octet_length(asset_id) <= 2000
        ),
    resource_key char(64) NOT NULL
        CHECK (resource_key ~ '^[0-9a-f]{64}$'),
    field_key char(64)
        CHECK (field_key IS NULL OR field_key ~ '^[0-9a-f]{64}$'),
    field_path varchar(200)[]
        CHECK (
            field_path IS NULL
            OR (
                cardinality(field_path) BETWEEN 1 AND 64
                AND array_position(field_path, NULL) IS NULL
                AND octet_length(array_to_string(field_path, '.')) <= 12800
            )
        ),
    removed_from_generation bigint NOT NULL
        CHECK (removed_from_generation > 0),
    prior_metadata_fingerprint char(64) NOT NULL
        CHECK (prior_metadata_fingerprint ~ '^[0-9a-f]{64}$'),
    removal_reason varchar(32) NOT NULL
        CHECK (removal_reason IN ('full_reconciliation_absence', 'delta_delete')),
    observed_at timestamptz NOT NULL,
    PRIMARY KEY (
        workspace_id,
        connection_id,
        observed_missing_in_generation,
        resource_kind,
        asset_key,
        resource_key
    ),
    CONSTRAINT catalog_tombstone_resource_shape
        CHECK (
            (
                resource_kind = 'asset'
                AND field_key IS NULL
                AND field_path IS NULL
                AND resource_key = asset_key
            )
            OR
            (
                resource_kind = 'field'
                AND field_key IS NOT NULL
                AND field_path IS NOT NULL
                AND resource_key = field_key
            )
        ),
    CONSTRAINT catalog_tombstones_connection_fk
        FOREIGN KEY (workspace_id, connection_id)
        REFERENCES schemabridge_control.catalog_connections (
            workspace_id,
            connection_id
        )
);

CREATE INDEX catalog_tombstones_resource_idx
    ON schemabridge_control.catalog_tombstones (
        workspace_id,
        connection_id,
        resource_kind,
        asset_key,
        field_key,
        observed_missing_in_generation
    );

CREATE FUNCTION schemabridge_control.guard_tenant_capacity_policy()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, schemabridge_control
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'tenant capacity policies cannot be deleted'
            USING ERRCODE = '55000';
    END IF;

    IF TG_OP = 'UPDATE' THEN
        IF (
            NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
            OR NEW.created_at IS DISTINCT FROM OLD.created_at
            OR NEW.version <= OLD.version
            OR NEW.updated_at <= OLD.updated_at
        ) THEN
            RAISE EXCEPTION 'tenant capacity policy revision is invalid'
                USING ERRCODE = '55000';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

CREATE FUNCTION schemabridge_control.apply_tenant_capacity_policy(
    p_workspace_id varchar,
    p_expected_version bigint,
    p_connection_limit integer,
    p_asset_limit bigint,
    p_field_limit bigint,
    p_api_requests_per_minute integer,
    p_nonterminal_job_limit integer,
    p_generation_retention_seconds integer,
    p_updated_by varchar,
    p_confirmation varchar
)
RETURNS TABLE (
    workspace_id varchar(200),
    version bigint,
    connection_limit integer,
    asset_limit bigint,
    field_limit bigint,
    api_requests_per_minute integer,
    nonterminal_job_limit integer,
    catalog_cursor_ttl_seconds integer,
    generation_retention_seconds integer,
    updated_by varchar(200),
    updated_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
DECLARE
    current_policy schemabridge_control.tenant_capacity_policies%ROWTYPE;
    next_version bigint;
    observed_at timestamptz := clock_timestamp();
BEGIN
    IF SESSION_USER <> 'schemabridge_migrator' THEN
        RAISE EXCEPTION 'tenant capacity policy operator role is invalid'
            USING ERRCODE = '42501';
    END IF;
    IF (
        p_workspace_id !~ '^[a-z0-9][a-z0-9_-]{2,199}$'
        OR octet_length(p_workspace_id) > 200
        OR p_expected_version NOT BETWEEN 0 AND 9223372036854775806
        OR p_connection_limit NOT BETWEEN 1 AND 100000
        OR p_asset_limit NOT BETWEEN 1 AND 100000000
        OR p_field_limit NOT BETWEEN 1 AND 1000000000
        OR p_api_requests_per_minute NOT BETWEEN 1 AND 1000000
        OR p_nonterminal_job_limit NOT BETWEEN 1 AND 1000000
        OR p_generation_retention_seconds NOT BETWEEN 900 AND 2592000
        OR p_updated_by !~ '^[a-z0-9][a-z0-9_-]{2,199}$'
        OR octet_length(p_updated_by) > 200
        OR p_confirmation <> 'APPLY TENANT CAPACITY POLICY'
    ) THEN
        RAISE EXCEPTION 'tenant capacity policy input is invalid'
            USING ERRCODE = '22023';
    END IF;

    SELECT *
    INTO current_policy
    FROM schemabridge_control.tenant_capacity_policies AS policy
    WHERE policy.workspace_id = p_workspace_id
    FOR UPDATE;

    IF current_policy.workspace_id IS NULL THEN
        IF p_expected_version <> 0 THEN
            RAISE EXCEPTION 'tenant capacity policy version conflict'
                USING ERRCODE = '40001';
        END IF;
        next_version := 1;
        INSERT INTO schemabridge_control.tenant_capacity_policies (
            workspace_id,
            connection_limit,
            asset_limit,
            field_limit,
            api_requests_per_minute,
            api_window_seconds,
            nonterminal_job_limit,
            catalog_cursor_ttl_seconds,
            generation_retention_seconds,
            version,
            updated_by,
            created_at,
            updated_at
        ) VALUES (
            p_workspace_id,
            p_connection_limit,
            p_asset_limit,
            p_field_limit,
            p_api_requests_per_minute,
            60,
            p_nonterminal_job_limit,
            900,
            p_generation_retention_seconds,
            next_version,
            p_updated_by,
            observed_at,
            observed_at
        );
    ELSE
        IF current_policy.version <> p_expected_version THEN
            RAISE EXCEPTION 'tenant capacity policy version conflict'
                USING ERRCODE = '40001';
        END IF;
        next_version := current_policy.version + 1;
        UPDATE schemabridge_control.tenant_capacity_policies AS policy
        SET connection_limit = p_connection_limit,
            asset_limit = p_asset_limit,
            field_limit = p_field_limit,
            api_requests_per_minute = p_api_requests_per_minute,
            nonterminal_job_limit = p_nonterminal_job_limit,
            generation_retention_seconds = p_generation_retention_seconds,
            version = next_version,
            updated_by = p_updated_by,
            updated_at = observed_at
        WHERE policy.workspace_id = p_workspace_id;
    END IF;

    INSERT INTO schemabridge_control.tenant_capacity_policy_revisions (
        workspace_id,
        version,
        connection_limit,
        asset_limit,
        field_limit,
        api_requests_per_minute,
        nonterminal_job_limit,
        catalog_cursor_ttl_seconds,
        generation_retention_seconds,
        updated_by,
        updated_at
    ) VALUES (
        p_workspace_id,
        next_version,
        p_connection_limit,
        p_asset_limit,
        p_field_limit,
        p_api_requests_per_minute,
        p_nonterminal_job_limit,
        900,
        p_generation_retention_seconds,
        p_updated_by,
        observed_at
    );

    RETURN QUERY
    SELECT
        policy.workspace_id,
        policy.version,
        policy.connection_limit,
        policy.asset_limit,
        policy.field_limit,
        policy.api_requests_per_minute,
        policy.nonterminal_job_limit,
        policy.catalog_cursor_ttl_seconds,
        policy.generation_retention_seconds,
        policy.updated_by,
        policy.updated_at
    FROM schemabridge_control.tenant_capacity_policies AS policy
    WHERE policy.workspace_id = p_workspace_id;
END;
$$;

CREATE FUNCTION schemabridge_control.admit_catalog_connection(
    p_workspace_id varchar
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
DECLARE
    connection_capacity_limit integer;
    current_connections bigint;
BEGIN
    IF SESSION_USER NOT IN ('schemabridge_api', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'catalog connection admission role is invalid'
            USING ERRCODE = '42501';
    END IF;
    IF (
        p_workspace_id IS NULL
        OR length(trim(p_workspace_id)) NOT BETWEEN 1 AND 200
        OR octet_length(p_workspace_id) > 200
    ) THEN
        RAISE EXCEPTION 'catalog connection admission input is invalid'
            USING ERRCODE = '22023';
    END IF;

    SELECT policy.connection_limit
    INTO connection_capacity_limit
    FROM schemabridge_control.tenant_capacity_policies AS policy
    WHERE policy.workspace_id = p_workspace_id
    FOR UPDATE;
    IF connection_capacity_limit IS NULL THEN
        RAISE EXCEPTION 'tenant capacity policy is unavailable'
            USING ERRCODE = '55000';
    END IF;

    SELECT count(*)
    INTO current_connections
    FROM schemabridge_control.catalog_connections
    WHERE workspace_id = p_workspace_id
      AND status = 'enabled';
    IF current_connections >= connection_capacity_limit THEN
        RAISE EXCEPTION 'tenant connection capacity is exhausted'
            USING ERRCODE = '53300';
    END IF;
END;
$$;

CREATE FUNCTION schemabridge_control.request_catalog_refresh(
    p_workspace_id varchar,
    p_connection_id varchar,
    p_refresh_id varchar,
    p_refresh_mode varchar,
    p_request_fingerprint char,
    p_idempotency_digest char,
    p_requested_by_actor_id varchar,
    p_requested_at timestamptz
)
RETURNS TABLE (
    observed_request_fingerprint char(64),
    request_replayed boolean,
    refresh_id varchar(200),
    workspace_id varchar(200),
    connection_id varchar(200),
    refresh_mode varchar(16),
    status varchar(16),
    base_generation bigint,
    target_generation bigint,
    source_page_number bigint,
    staged_asset_count bigint,
    staged_field_count bigint,
    source_complete boolean,
    inventory_fingerprint char(64),
    failure_code varchar(64),
    requested_at timestamptz,
    updated_at timestamptz,
    completed_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
DECLARE
    inserted_record boolean;
BEGIN
    IF SESSION_USER NOT IN ('schemabridge_api', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'catalog refresh request role is invalid'
            USING ERRCODE = '42501';
    END IF;
    IF (
        p_workspace_id IS NULL
        OR length(trim(p_workspace_id)) NOT BETWEEN 1 AND 200
        OR octet_length(p_workspace_id) > 200
        OR p_connection_id !~ '^[a-z][a-z0-9_-]{2,199}$'
        OR p_refresh_id !~ '^[a-z][a-z0-9_-]{2,199}$'
        OR p_refresh_mode NOT IN ('full', 'delta')
        OR p_request_fingerprint !~ '^[0-9a-f]{64}$'
        OR p_idempotency_digest !~ '^[0-9a-f]{64}$'
        OR p_requested_by_actor_id IS NULL
        OR length(trim(p_requested_by_actor_id)) NOT BETWEEN 1 AND 200
        OR octet_length(p_requested_by_actor_id) > 200
        OR p_requested_at IS NULL
    ) THEN
        RAISE EXCEPTION 'catalog refresh request input is invalid'
            USING ERRCODE = '22023';
    END IF;

    INSERT INTO schemabridge_control.catalog_refresh_runs (
        workspace_id,
        connection_id,
        refresh_id,
        refresh_mode,
        status,
        request_fingerprint,
        idempotency_digest,
        requested_by_actor_id,
        requested_at,
        updated_at
    ) VALUES (
        p_workspace_id,
        p_connection_id,
        p_refresh_id,
        p_refresh_mode,
        'requested',
        p_request_fingerprint,
        p_idempotency_digest,
        p_requested_by_actor_id,
        p_requested_at,
        p_requested_at
    )
    ON CONFLICT DO NOTHING
    RETURNING true
    INTO inserted_record;

    RETURN QUERY
    SELECT
        refresh.request_fingerprint,
        NOT coalesce(inserted_record, false),
        refresh.refresh_id,
        refresh.workspace_id,
        refresh.connection_id,
        refresh.refresh_mode,
        refresh.status,
        coalesce(refresh.base_generation, 0),
        refresh.target_generation,
        refresh.source_page_number,
        refresh.staged_asset_count,
        refresh.staged_field_count,
        refresh.source_complete,
        refresh.inventory_fingerprint,
        refresh.failure_code,
        refresh.requested_at,
        refresh.updated_at,
        refresh.completed_at
    FROM schemabridge_control.catalog_refresh_runs AS refresh
    WHERE refresh.workspace_id = p_workspace_id
      AND refresh.connection_id = p_connection_id
      AND refresh.idempotency_digest = p_idempotency_digest;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'catalog refresh request conflicts with active state'
            USING ERRCODE = '23505';
    END IF;
END;
$$;

CREATE FUNCTION schemabridge_control.load_catalog_refresh_public(
    p_workspace_id varchar,
    p_refresh_id varchar
)
RETURNS TABLE (
    refresh_id varchar(200),
    workspace_id varchar(200),
    connection_id varchar(200),
    refresh_mode varchar(16),
    status varchar(16),
    base_generation bigint,
    target_generation bigint,
    source_page_number bigint,
    staged_asset_count bigint,
    staged_field_count bigint,
    source_complete boolean,
    inventory_fingerprint char(64),
    failure_code varchar(64),
    requested_at timestamptz,
    updated_at timestamptz,
    completed_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
BEGIN
    IF SESSION_USER NOT IN ('schemabridge_api', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'catalog refresh inspection role is invalid'
            USING ERRCODE = '42501';
    END IF;
    IF (
        p_workspace_id IS NULL
        OR length(trim(p_workspace_id)) NOT BETWEEN 1 AND 200
        OR octet_length(p_workspace_id) > 200
        OR p_refresh_id !~ '^[a-z][a-z0-9_-]{2,199}$'
    ) THEN
        RAISE EXCEPTION 'catalog refresh inspection input is invalid'
            USING ERRCODE = '22023';
    END IF;

    RETURN QUERY
    SELECT
        refresh.refresh_id,
        refresh.workspace_id,
        refresh.connection_id,
        refresh.refresh_mode,
        refresh.status,
        coalesce(refresh.base_generation, 0),
        refresh.target_generation,
        refresh.source_page_number,
        refresh.staged_asset_count,
        refresh.staged_field_count,
        refresh.source_complete,
        refresh.inventory_fingerprint,
        refresh.failure_code,
        refresh.requested_at,
        refresh.updated_at,
        refresh.completed_at
    FROM schemabridge_control.catalog_refresh_runs AS refresh
    WHERE refresh.workspace_id = p_workspace_id
      AND refresh.refresh_id = p_refresh_id;
END;
$$;

CREATE FUNCTION schemabridge_control.load_owned_catalog_connection_route(
    p_workspace_id varchar,
    p_connection_id varchar,
    p_refresh_id varchar,
    p_indexer_id varchar,
    p_lease_capability varchar,
    p_fencing_token bigint
)
RETURNS TABLE (
    workspace_id varchar(200),
    connection_id varchar(200),
    source_kind varchar(32),
    environment varchar(80),
    catalog_scope varchar(200),
    credential_binding_ref varchar(200),
    status varchar(16)
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
BEGIN
    IF SESSION_USER NOT IN ('schemabridge_catalog', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'catalog route role is invalid'
            USING ERRCODE = '42501';
    END IF;
    IF (
        p_workspace_id IS NULL
        OR length(trim(p_workspace_id)) NOT BETWEEN 1 AND 200
        OR octet_length(p_workspace_id) > 200
        OR p_connection_id !~ '^[a-z][a-z0-9_-]{2,199}$'
        OR p_refresh_id !~ '^[a-z][a-z0-9_-]{2,199}$'
        OR p_indexer_id !~ '^[a-z][a-z0-9_.:-]{2,199}$'
        OR p_lease_capability IS NULL
        OR p_lease_capability <> trim(p_lease_capability)
        OR octet_length(p_lease_capability) NOT BETWEEN 32 AND 1024
        OR p_fencing_token < 1
    ) THEN
        RAISE EXCEPTION 'catalog route input is invalid'
            USING ERRCODE = '22023';
    END IF;

    RETURN QUERY
    SELECT
        connection.workspace_id,
        connection.connection_id,
        connection.source_kind,
        connection.environment,
        connection.catalog_scope,
        route.credential_binding_ref,
        connection.status
    FROM schemabridge_control.catalog_refresh_runs AS refresh
    JOIN schemabridge_control.catalog_connections AS connection
      ON connection.workspace_id = refresh.workspace_id
     AND connection.connection_id = refresh.connection_id
    JOIN schemabridge_control.catalog_connection_routes AS route
      ON route.workspace_id = connection.workspace_id
     AND route.connection_id = connection.connection_id
    WHERE refresh.workspace_id = p_workspace_id
      AND refresh.connection_id = p_connection_id
      AND refresh.refresh_id = p_refresh_id
      AND refresh.status IN ('leased', 'staging')
      AND refresh.lease_owner_id = p_indexer_id
      AND refresh.lease_capability_digest = encode(
          sha256(convert_to(p_lease_capability, 'UTF8')),
          'hex'
      )
      AND refresh.fencing_token = p_fencing_token
      AND refresh.lease_expires_at > clock_timestamp()
      AND connection.status = 'enabled';
END;
$$;

CREATE FUNCTION schemabridge_control.guard_catalog_connection()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, schemabridge_control
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'catalog connections use logical disable only'
            USING ERRCODE = '55000';
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF (
            NEW.status <> 'enabled'
            OR NEW.active_generation IS NOT NULL
            OR NEW.active_generation_fingerprint IS NOT NULL
            OR NEW.active_generation_completed_at IS NOT NULL
            OR NEW.disabled_by_actor_id IS NOT NULL
            OR NEW.disabled_idempotency_digest IS NOT NULL
            OR NEW.disabled_at IS NOT NULL
        ) THEN
            RAISE EXCEPTION 'new catalog connection state is invalid'
                USING ERRCODE = '55000';
        END IF;
        PERFORM schemabridge_control.admit_catalog_connection(
            NEW.workspace_id
        );
        RETURN NEW;
    END IF;

    IF (
        NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
        OR NEW.connection_id IS DISTINCT FROM OLD.connection_id
        OR NEW.display_name IS DISTINCT FROM OLD.display_name
        OR NEW.source_kind IS DISTINCT FROM OLD.source_kind
        OR NEW.catalog_scope IS DISTINCT FROM OLD.catalog_scope
        OR NEW.environment IS DISTINCT FROM OLD.environment
        OR NEW.platform_instance IS DISTINCT FROM OLD.platform_instance
        OR NEW.registration_fingerprint IS DISTINCT FROM OLD.registration_fingerprint
        OR NEW.idempotency_digest IS DISTINCT FROM OLD.idempotency_digest
        OR NEW.created_by_actor_id IS DISTINCT FROM OLD.created_by_actor_id
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
        OR NEW.updated_at <= OLD.updated_at
    ) THEN
        RAISE EXCEPTION 'catalog connection identity cannot be changed'
            USING ERRCODE = '55000';
    END IF;

    IF CURRENT_USER = 'schemabridge_api' THEN
        IF (
            OLD.status <> 'enabled'
            OR NEW.status <> 'disabled'
            OR NEW.disabled_by_actor_id IS NULL
            OR NEW.disabled_idempotency_digest IS NULL
            OR NEW.disabled_at IS NULL
            OR NEW.active_generation IS DISTINCT FROM OLD.active_generation
            OR NEW.active_generation_fingerprint
                IS DISTINCT FROM OLD.active_generation_fingerprint
            OR NEW.active_generation_completed_at
                IS DISTINCT FROM OLD.active_generation_completed_at
        ) THEN
            RAISE EXCEPTION 'catalog connection disable transition is invalid'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF CURRENT_USER = 'schemabridge_migrator' THEN
        IF (
            NEW.status IS DISTINCT FROM OLD.status
            OR NEW.disabled_by_actor_id IS DISTINCT FROM OLD.disabled_by_actor_id
            OR NEW.disabled_idempotency_digest
                IS DISTINCT FROM OLD.disabled_idempotency_digest
            OR NEW.disabled_at IS DISTINCT FROM OLD.disabled_at
        ) THEN
            RAISE EXCEPTION 'catalog generation activation cannot change connection status'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'catalog connection mutation role is invalid'
        USING ERRCODE = '42501';
END;
$$;

CREATE FUNCTION schemabridge_control.reject_catalog_immutable_mutation()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, schemabridge_control
AS $$
BEGIN
    RAISE EXCEPTION 'immutable catalog record cannot be changed'
        USING ERRCODE = '55000';
END;
$$;

CREATE FUNCTION schemabridge_control.guard_catalog_refresh_lifecycle()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, schemabridge_control
AS $$
DECLARE
    connection_status varchar(16);
    active_generation_value bigint;
    next_generation_value bigint;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'catalog refresh records cannot be deleted'
            USING ERRCODE = '55000';
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF (
            CURRENT_USER NOT IN ('schemabridge_api', 'schemabridge_migrator')
            OR NEW.status <> 'requested'
            OR NEW.base_generation IS NOT NULL
            OR NEW.target_generation IS NOT NULL
            OR NEW.fencing_token <> 0
            OR NEW.source_page_number <> 0
            OR NEW.staged_asset_count <> 0
            OR NEW.staged_field_count <> 0
            OR NEW.source_complete
        ) THEN
            RAISE EXCEPTION 'catalog refresh request is invalid'
                USING ERRCODE = '55000';
        END IF;
        SELECT status, active_generation
        INTO connection_status, active_generation_value
        FROM schemabridge_control.catalog_connections
        WHERE workspace_id = NEW.workspace_id
          AND connection_id = NEW.connection_id
        FOR UPDATE;
        IF connection_status IS DISTINCT FROM 'enabled' THEN
            RAISE EXCEPTION 'catalog connection is unavailable for refresh'
                USING ERRCODE = '55000';
        END IF;
        SELECT greatest(
            coalesce(
                (
                    SELECT max(generation)
                    FROM schemabridge_control.catalog_generations
                    WHERE workspace_id = NEW.workspace_id
                      AND connection_id = NEW.connection_id
                ),
                0
            ),
            coalesce(
                (
                    SELECT max(target_generation)
                    FROM schemabridge_control.catalog_refresh_runs
                    WHERE workspace_id = NEW.workspace_id
                      AND connection_id = NEW.connection_id
                ),
                0
            )
        ) + 1
        INTO next_generation_value;
        NEW.base_generation := active_generation_value;
        NEW.target_generation := next_generation_value;
        RETURN NEW;
    END IF;

    IF (
        NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
        OR NEW.connection_id IS DISTINCT FROM OLD.connection_id
        OR NEW.refresh_id IS DISTINCT FROM OLD.refresh_id
        OR NEW.refresh_mode IS DISTINCT FROM OLD.refresh_mode
        OR NEW.request_fingerprint IS DISTINCT FROM OLD.request_fingerprint
        OR NEW.idempotency_digest IS DISTINCT FROM OLD.idempotency_digest
        OR NEW.requested_by_actor_id IS DISTINCT FROM OLD.requested_by_actor_id
        OR NEW.requested_at IS DISTINCT FROM OLD.requested_at
        OR NEW.updated_at <= OLD.updated_at
    ) THEN
        RAISE EXCEPTION 'catalog refresh identity cannot be changed'
            USING ERRCODE = '55000';
    END IF;

    IF OLD.status = 'requested' AND NEW.status = 'leased' THEN
        SELECT status, active_generation
        INTO connection_status, active_generation_value
        FROM schemabridge_control.catalog_connections
        WHERE workspace_id = NEW.workspace_id
          AND connection_id = NEW.connection_id;
        SELECT coalesce(max(generation), 0) + 1
        INTO next_generation_value
        FROM schemabridge_control.catalog_generations
        WHERE workspace_id = NEW.workspace_id
          AND connection_id = NEW.connection_id;
        IF (
            CURRENT_USER NOT IN ('schemabridge_catalog', 'schemabridge_migrator')
            OR connection_status IS DISTINCT FROM 'enabled'
            OR NEW.base_generation IS DISTINCT FROM active_generation_value
            OR NEW.fencing_token <> OLD.fencing_token + 1
            OR NEW.lease_owner_id IS NULL
            OR NEW.lease_capability_digest IS NULL
            OR NEW.lease_acquired_at IS NULL
            OR NEW.lease_heartbeat_at IS NULL
            OR NEW.lease_expires_at IS NULL
            OR NEW.lease_expires_at <= clock_timestamp()
            OR NEW.target_generation IS NULL
            OR (
                OLD.target_generation IS NULL
                AND NEW.target_generation <> next_generation_value
            )
            OR (
                OLD.target_generation IS NOT NULL
                AND NEW.target_generation <> OLD.target_generation
            )
            OR NEW.source_page_number <> OLD.source_page_number
            OR NEW.staged_asset_count <> OLD.staged_asset_count
            OR NEW.staged_field_count <> OLD.staged_field_count
            OR NEW.source_complete IS DISTINCT FROM OLD.source_complete
        ) THEN
            RAISE EXCEPTION 'catalog refresh claim is invalid'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.status = 'leased' AND NEW.status = 'staging' THEN
        IF (
            CURRENT_USER NOT IN ('schemabridge_catalog', 'schemabridge_migrator')
            OR NEW.fencing_token <> OLD.fencing_token
            OR NEW.lease_owner_id IS DISTINCT FROM OLD.lease_owner_id
            OR NEW.lease_capability_digest IS DISTINCT FROM OLD.lease_capability_digest
            OR NEW.base_generation IS DISTINCT FROM OLD.base_generation
            OR NEW.target_generation IS DISTINCT FROM OLD.target_generation
        ) THEN
            RAISE EXCEPTION 'catalog refresh staging transition is invalid'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF (
        OLD.status IN ('leased', 'staging')
        AND NEW.status = OLD.status
    ) THEN
        IF (
            CURRENT_USER NOT IN ('schemabridge_catalog', 'schemabridge_migrator')
            OR NEW.fencing_token <> OLD.fencing_token
            OR NEW.lease_owner_id IS DISTINCT FROM OLD.lease_owner_id
            OR NEW.lease_capability_digest IS DISTINCT FROM OLD.lease_capability_digest
            OR NEW.base_generation IS DISTINCT FROM OLD.base_generation
            OR NEW.target_generation IS DISTINCT FROM OLD.target_generation
            OR NEW.lease_heartbeat_at < OLD.lease_heartbeat_at
            OR NEW.lease_expires_at < OLD.lease_expires_at
            OR NEW.source_page_number NOT IN (
                OLD.source_page_number,
                OLD.source_page_number + 1
            )
        ) THEN
            RAISE EXCEPTION 'catalog refresh heartbeat or page transition is invalid'
                USING ERRCODE = '55000';
        END IF;
        IF NEW.source_page_number = OLD.source_page_number AND (
            NEW.source_checkpoint IS DISTINCT FROM OLD.source_checkpoint
            OR NEW.source_page_fingerprint IS DISTINCT FROM OLD.source_page_fingerprint
            OR NEW.staged_asset_count <> OLD.staged_asset_count
            OR NEW.staged_field_count <> OLD.staged_field_count
            OR NEW.source_complete IS DISTINCT FROM OLD.source_complete
        ) THEN
            RAISE EXCEPTION 'catalog refresh page replay changed persisted state'
                USING ERRCODE = '55000';
        END IF;
        IF NEW.source_page_number = OLD.source_page_number + 1 AND (
            NEW.source_page_fingerprint IS NULL
            OR (
                NEW.refresh_mode = 'full'
                AND (
                    NEW.staged_asset_count < OLD.staged_asset_count
                    OR NEW.staged_field_count < OLD.staged_field_count
                )
            )
            OR (OLD.source_complete AND NOT NEW.source_complete)
        ) THEN
            RAISE EXCEPTION 'catalog refresh page progress is invalid'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF (
        OLD.status IN ('leased', 'staging')
        AND NEW.status = 'requested'
    ) THEN
        IF (
            CURRENT_USER NOT IN ('schemabridge_catalog', 'schemabridge_migrator')
            OR OLD.lease_expires_at > clock_timestamp()
            OR NEW.fencing_token <> OLD.fencing_token
            OR NEW.base_generation IS DISTINCT FROM OLD.base_generation
            OR NEW.target_generation IS DISTINCT FROM OLD.target_generation
            OR NEW.source_checkpoint IS DISTINCT FROM OLD.source_checkpoint
            OR NEW.source_page_number <> OLD.source_page_number
            OR NEW.source_page_fingerprint IS DISTINCT FROM OLD.source_page_fingerprint
            OR NEW.staged_asset_count <> OLD.staged_asset_count
            OR NEW.staged_field_count <> OLD.staged_field_count
            OR NEW.source_complete IS DISTINCT FROM OLD.source_complete
        ) THEN
            RAISE EXCEPTION 'catalog refresh lease reclaim is invalid'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF (
        OLD.status IN ('requested', 'leased', 'staging')
        AND NEW.status = 'failed'
    ) THEN
        IF (
            CURRENT_USER NOT IN ('schemabridge_catalog', 'schemabridge_migrator')
            OR NEW.failure_code IS NULL
            OR NEW.completed_at IS NULL
        ) THEN
            RAISE EXCEPTION 'catalog refresh failure transition is invalid'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.status = 'staging' AND NEW.status = 'completed' THEN
        IF (
            CURRENT_USER <> 'schemabridge_migrator'
            OR NOT NEW.source_complete
            OR NEW.inventory_fingerprint IS NULL
            OR NEW.completed_at IS NULL
        ) THEN
            RAISE EXCEPTION 'catalog refresh completion transition is invalid'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'catalog refresh lifecycle transition is invalid'
        USING ERRCODE = '55000';
END;
$$;

CREATE FUNCTION schemabridge_control.guard_catalog_generation_lifecycle()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, schemabridge_control
AS $$
DECLARE
    refresh_record record;
    active_generation_value bigint;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF (
            CURRENT_USER NOT IN ('schemabridge_catalog', 'schemabridge_migrator')
            OR NEW.status <> 'staging'
        ) THEN
            RAISE EXCEPTION 'new catalog generation state is invalid'
                USING ERRCODE = '55000';
        END IF;
        SELECT status, refresh_mode, base_generation, target_generation
        INTO refresh_record
        FROM schemabridge_control.catalog_refresh_runs
        WHERE workspace_id = NEW.workspace_id
          AND connection_id = NEW.connection_id
          AND refresh_id = NEW.refresh_id;
        IF (
            refresh_record.status NOT IN ('leased', 'staging')
            OR refresh_record.refresh_mode <> NEW.refresh_mode
            OR refresh_record.base_generation IS DISTINCT FROM NEW.base_generation
            OR (
                refresh_record.target_generation IS NOT NULL
                AND refresh_record.target_generation <> NEW.generation
            )
        ) THEN
            RAISE EXCEPTION 'catalog generation does not match its refresh'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF TG_OP = 'UPDATE' THEN
        IF (
            CURRENT_USER = 'schemabridge_migrator'
            AND OLD.status = 'completed'
            AND NEW.status = 'completed'
            AND NEW.workspace_id IS NOT DISTINCT FROM OLD.workspace_id
            AND NEW.connection_id IS NOT DISTINCT FROM OLD.connection_id
            AND NEW.generation IS NOT DISTINCT FROM OLD.generation
            AND NEW.refresh_id IS NOT DISTINCT FROM OLD.refresh_id
            AND NEW.base_generation IS NOT DISTINCT FROM OLD.base_generation
            AND NEW.refresh_mode IS NOT DISTINCT FROM OLD.refresh_mode
            AND NEW.asset_count IS NOT DISTINCT FROM OLD.asset_count
            AND NEW.field_count IS NOT DISTINCT FROM OLD.field_count
            AND NEW.inventory_fingerprint
                IS NOT DISTINCT FROM OLD.inventory_fingerprint
            AND NEW.created_at IS NOT DISTINCT FROM OLD.created_at
            AND NEW.completed_at IS NOT DISTINCT FROM OLD.completed_at
            AND NEW.retain_until >= OLD.retain_until
        ) THEN
            RETURN NEW;
        END IF;
        IF (
            CURRENT_USER <> 'schemabridge_migrator'
            OR OLD.status <> 'staging'
            OR NEW.status <> 'completed'
            OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
            OR NEW.connection_id IS DISTINCT FROM OLD.connection_id
            OR NEW.generation IS DISTINCT FROM OLD.generation
            OR NEW.refresh_id IS DISTINCT FROM OLD.refresh_id
            OR NEW.base_generation IS DISTINCT FROM OLD.base_generation
            OR NEW.refresh_mode IS DISTINCT FROM OLD.refresh_mode
            OR NEW.created_at IS DISTINCT FROM OLD.created_at
        ) THEN
            RAISE EXCEPTION 'catalog generation completion is invalid'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF CURRENT_USER <> 'schemabridge_migrator' THEN
        RAISE EXCEPTION 'catalog generation deletion role is invalid'
            USING ERRCODE = '42501';
    END IF;
    SELECT active_generation
    INTO active_generation_value
    FROM schemabridge_control.catalog_connections
    WHERE workspace_id = OLD.workspace_id
      AND connection_id = OLD.connection_id;
    IF active_generation_value = OLD.generation THEN
        RAISE EXCEPTION 'catalog generation is not eligible for retention cleanup'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.status = 'completed' AND (
        OLD.retain_until IS NULL
        OR OLD.retain_until >= clock_timestamp()
    ) THEN
        RAISE EXCEPTION 'catalog generation is not eligible for retention cleanup'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.status = 'staging' AND NOT EXISTS (
        SELECT 1
        FROM schemabridge_control.catalog_refresh_runs
        WHERE workspace_id = OLD.workspace_id
          AND connection_id = OLD.connection_id
          AND refresh_id = OLD.refresh_id
          AND status = 'failed'
    ) THEN
        RAISE EXCEPTION 'catalog staging generation is still owned by an active refresh'
            USING ERRCODE = '55000';
    END IF;
    RETURN OLD;
END;
$$;

CREATE FUNCTION schemabridge_control.guard_catalog_generation_content()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, schemabridge_control
AS $$
DECLARE
    row_workspace_id varchar(200);
    row_connection_id varchar(200);
    row_generation bigint;
    generation_status varchar(16);
    active_generation_value bigint;
BEGIN
    IF TG_OP = 'UPDATE' AND (
        NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
        OR NEW.connection_id IS DISTINCT FROM OLD.connection_id
        OR NEW.generation IS DISTINCT FROM OLD.generation
        OR NEW.asset_key IS DISTINCT FROM OLD.asset_key
    ) THEN
        RAISE EXCEPTION 'catalog content identity cannot be changed'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'DELETE' THEN
        IF CURRENT_USER = 'schemabridge_migrator' THEN
            RETURN OLD;
        END IF;
        row_workspace_id := OLD.workspace_id;
        row_connection_id := OLD.connection_id;
        row_generation := OLD.generation;
    ELSE
        row_workspace_id := NEW.workspace_id;
        row_connection_id := NEW.connection_id;
        row_generation := NEW.generation;
    END IF;

    SELECT generation.status, connection.active_generation
    INTO generation_status, active_generation_value
    FROM schemabridge_control.catalog_generations AS generation
    JOIN schemabridge_control.catalog_connections AS connection
      ON connection.workspace_id = generation.workspace_id
     AND connection.connection_id = generation.connection_id
    WHERE generation.workspace_id = row_workspace_id
      AND generation.connection_id = row_connection_id
      AND generation.generation = row_generation;

    IF (
        generation_status IS DISTINCT FROM 'staging'
        OR active_generation_value = row_generation
    ) THEN
        RAISE EXCEPTION 'catalog content may change only in an inactive staging generation'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION schemabridge_control.guard_catalog_asset_identity()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, schemabridge_control
AS $$
BEGIN
    IF (
        NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
        OR NEW.connection_id IS DISTINCT FROM OLD.connection_id
        OR NEW.generation IS DISTINCT FROM OLD.generation
        OR NEW.asset_key IS DISTINCT FROM OLD.asset_key
        OR NEW.asset_id IS DISTINCT FROM OLD.asset_id
    ) THEN
        RAISE EXCEPTION 'catalog asset identity cannot be changed'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION schemabridge_control.guard_catalog_field_identity()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, schemabridge_control
AS $$
BEGIN
    IF (
        NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
        OR NEW.connection_id IS DISTINCT FROM OLD.connection_id
        OR NEW.generation IS DISTINCT FROM OLD.generation
        OR NEW.asset_key IS DISTINCT FROM OLD.asset_key
        OR NEW.field_key IS DISTINCT FROM OLD.field_key
        OR NEW.field_path IS DISTINCT FROM OLD.field_path
    ) THEN
        RAISE EXCEPTION 'catalog field identity cannot be changed'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION schemabridge_control.guard_catalog_tombstone()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, schemabridge_control
AS $$
DECLARE
    generation_record record;
    prior_fingerprint char(64);
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'immutable catalog record cannot be changed'
            USING ERRCODE = '55000';
    END IF;

    SELECT generation.status,
           generation.refresh_mode,
           generation.base_generation,
           refresh.status AS refresh_status
    INTO generation_record
    FROM schemabridge_control.catalog_generations AS generation
    JOIN schemabridge_control.catalog_refresh_runs AS refresh
      ON refresh.workspace_id = generation.workspace_id
     AND refresh.connection_id = generation.connection_id
     AND refresh.refresh_id = generation.refresh_id
    WHERE generation.workspace_id = NEW.workspace_id
      AND generation.connection_id = NEW.connection_id
      AND generation.generation = NEW.observed_missing_in_generation;

    IF (
        generation_record.status IS DISTINCT FROM 'staging'
        OR generation_record.refresh_status IS DISTINCT FROM 'staging'
        OR generation_record.base_generation
            IS DISTINCT FROM NEW.removed_from_generation
    ) THEN
        RAISE EXCEPTION 'catalog tombstone generation is invalid'
            USING ERRCODE = '55000';
    END IF;
    IF (
        (
            CURRENT_USER = 'schemabridge_catalog'
            AND (
                NEW.removal_reason <> 'delta_delete'
                OR generation_record.refresh_mode <> 'delta'
            )
        )
        OR
        (
            CURRENT_USER = 'schemabridge_migrator'
            AND (
                NEW.removal_reason <> 'full_reconciliation_absence'
                OR generation_record.refresh_mode <> 'full'
            )
        )
        OR CURRENT_USER NOT IN ('schemabridge_catalog', 'schemabridge_migrator')
    ) THEN
        RAISE EXCEPTION 'catalog tombstone role or mode is invalid'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.resource_kind = 'asset' THEN
        SELECT metadata_fingerprint
        INTO prior_fingerprint
        FROM schemabridge_control.catalog_assets
        WHERE workspace_id = NEW.workspace_id
          AND connection_id = NEW.connection_id
          AND generation = NEW.removed_from_generation
          AND asset_key = NEW.asset_key
          AND asset_id = NEW.asset_id;
    ELSE
        SELECT field.metadata_fingerprint
        INTO prior_fingerprint
        FROM schemabridge_control.catalog_fields AS field
        JOIN schemabridge_control.catalog_assets AS asset
          ON asset.workspace_id = field.workspace_id
         AND asset.connection_id = field.connection_id
         AND asset.generation = field.generation
         AND asset.asset_key = field.asset_key
        WHERE field.workspace_id = NEW.workspace_id
          AND field.connection_id = NEW.connection_id
          AND field.generation = NEW.removed_from_generation
          AND field.asset_key = NEW.asset_key
          AND field.field_key = NEW.field_key
          AND field.field_path = NEW.field_path
          AND asset.asset_id = NEW.asset_id;
    END IF;
    IF prior_fingerprint IS DISTINCT FROM NEW.prior_metadata_fingerprint THEN
        RAISE EXCEPTION 'catalog tombstone prior evidence is invalid'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION schemabridge_control.maintain_execution_job_capacity()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
DECLARE
    old_nonterminal boolean := false;
    new_nonterminal boolean := false;
    capacity_limit integer;
    current_count integer;
BEGIN
    IF TG_OP = 'INSERT' THEN
        new_nonterminal := NEW.status IN (
            'queued',
            'leased',
            'retry_wait',
            'cancel_requested'
        );
    ELSE
        old_nonterminal := OLD.status IN (
            'queued',
            'leased',
            'retry_wait',
            'cancel_requested'
        );
        new_nonterminal := NEW.status IN (
            'queued',
            'leased',
            'retry_wait',
            'cancel_requested'
        );
    END IF;

    IF new_nonterminal AND NOT old_nonterminal THEN
        SELECT nonterminal_job_limit
        INTO capacity_limit
        FROM schemabridge_control.tenant_capacity_policies
        WHERE workspace_id = NEW.workspace_id
        FOR UPDATE;
        IF capacity_limit IS NULL THEN
            RAISE EXCEPTION 'tenant capacity policy is unavailable'
                USING ERRCODE = '55000';
        END IF;
        INSERT INTO schemabridge_control.tenant_execution_capacity (
            workspace_id,
            nonterminal_job_count,
            capacity_version,
            updated_at
        ) VALUES (
            NEW.workspace_id,
            0,
            0,
            clock_timestamp()
        )
        ON CONFLICT (workspace_id) DO NOTHING;
        INSERT INTO schemabridge_control.tenant_job_schedule (
            workspace_id,
            last_claimed_at,
            claim_sequence,
            updated_at
        ) VALUES (
            NEW.workspace_id,
            NULL,
            0,
            clock_timestamp()
        )
        ON CONFLICT (workspace_id) DO NOTHING;
        SELECT nonterminal_job_count
        INTO current_count
        FROM schemabridge_control.tenant_execution_capacity
        WHERE workspace_id = NEW.workspace_id
        FOR UPDATE;
        IF current_count >= capacity_limit THEN
            RAISE EXCEPTION 'tenant execution capacity is exhausted'
                USING ERRCODE = '53300';
        END IF;
        UPDATE schemabridge_control.tenant_execution_capacity
        SET nonterminal_job_count = nonterminal_job_count + 1,
            capacity_version = capacity_version + 1,
            updated_at = clock_timestamp()
        WHERE workspace_id = NEW.workspace_id;
    ELSIF old_nonterminal AND NOT new_nonterminal THEN
        UPDATE schemabridge_control.tenant_execution_capacity
        SET nonterminal_job_count = nonterminal_job_count - 1,
            capacity_version = capacity_version + 1,
            updated_at = clock_timestamp()
        WHERE workspace_id = OLD.workspace_id
          AND nonterminal_job_count > 0;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'tenant execution capacity state is inconsistent'
                USING ERRCODE = '55000';
        END IF;
    END IF;
    RETURN NULL;
END;
$$;

CREATE FUNCTION schemabridge_control.admit_api_request(
    p_workspace_id varchar,
    p_principal_digest char(64),
    p_operation_scope varchar
)
RETURNS TABLE (
    is_allowed boolean,
    retry_after_seconds integer,
    remaining integer,
    request_limit integer,
    used integer
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
DECLARE
    observed_at timestamptz := clock_timestamp();
    configured_request_limit integer;
    window_seconds constant integer := 60;
    window_start timestamptz;
    window_end timestamptz;
    observed_count integer;
BEGIN
    IF (
        p_workspace_id IS NULL
        OR length(trim(p_workspace_id)) NOT BETWEEN 1 AND 200
        OR octet_length(p_workspace_id) > 200
        OR p_principal_digest !~ '^[0-9a-f]{64}$'
        OR p_operation_scope !~ '^[a-z][a-z0-9_:-]{1,79}$'
    ) THEN
        RAISE EXCEPTION 'API admission input is invalid'
            USING ERRCODE = '22023';
    END IF;

    SELECT api_requests_per_minute
    INTO configured_request_limit
    FROM schemabridge_control.tenant_capacity_policies
    WHERE workspace_id = p_workspace_id
    FOR SHARE;
    IF configured_request_limit IS NULL THEN
        RAISE EXCEPTION 'tenant capacity policy is unavailable'
            USING ERRCODE = '55000';
    END IF;

    window_start := to_timestamp(
        floor(extract(epoch FROM observed_at) / window_seconds)
            * window_seconds
    );
    window_end := window_start + make_interval(secs => window_seconds);

    WITH expired AS (
        SELECT rate_window.workspace_id,
               rate_window.principal_digest,
               rate_window.operation_scope,
               rate_window.window_started_at
        FROM schemabridge_control.api_rate_limit_windows AS rate_window
        WHERE rate_window.window_expires_at <= observed_at
        ORDER BY rate_window.window_expires_at,
                 rate_window.workspace_id,
                 rate_window.principal_digest,
                 rate_window.operation_scope,
                 rate_window.window_started_at
        FOR UPDATE SKIP LOCKED
        LIMIT 100
    )
    DELETE FROM schemabridge_control.api_rate_limit_windows AS rate_window
    USING expired
    WHERE rate_window.workspace_id = expired.workspace_id
      AND rate_window.principal_digest = expired.principal_digest
      AND rate_window.operation_scope = expired.operation_scope
      AND rate_window.window_started_at = expired.window_started_at;

    INSERT INTO schemabridge_control.api_rate_limit_windows (
        workspace_id,
        principal_digest,
        operation_scope,
        window_started_at,
        window_expires_at,
        request_count,
        created_at,
        updated_at
    ) VALUES (
        p_workspace_id,
        p_principal_digest,
        p_operation_scope,
        window_start,
        window_end,
        1,
        observed_at,
        observed_at
    )
    ON CONFLICT (
        workspace_id,
        principal_digest,
        operation_scope,
        window_started_at
    ) DO NOTHING
    RETURNING request_count INTO observed_count;

    IF observed_count IS NOT NULL THEN
        RETURN QUERY
        SELECT true, 0, configured_request_limit - 1, configured_request_limit, 1;
        RETURN;
    END IF;

    SELECT request_count
    INTO observed_count
    FROM schemabridge_control.api_rate_limit_windows
    WHERE workspace_id = p_workspace_id
      AND principal_digest = p_principal_digest
      AND operation_scope = p_operation_scope
      AND window_started_at = window_start
    FOR UPDATE;

    IF observed_count < configured_request_limit THEN
        UPDATE schemabridge_control.api_rate_limit_windows
        SET request_count = request_count + 1,
            updated_at = clock_timestamp()
        WHERE workspace_id = p_workspace_id
          AND principal_digest = p_principal_digest
          AND operation_scope = p_operation_scope
          AND window_started_at = window_start
        RETURNING request_count INTO observed_count;
        RETURN QUERY
        SELECT
            true,
            0,
            configured_request_limit - observed_count,
            configured_request_limit,
            observed_count;
        RETURN;
    END IF;

    RETURN QUERY
    SELECT
        false,
        greatest(
            1,
            ceil(extract(epoch FROM window_end - observed_at))::integer
        ),
        0,
        configured_request_limit,
        observed_count;
END;
$$;

CREATE FUNCTION schemabridge_control.lock_catalog_completion_scope(
    p_workspace_id varchar,
    p_connection_id varchar,
    p_refresh_id varchar,
    p_target_generation bigint,
    p_fencing_token bigint,
    p_lease_capability_digest char(64)
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
DECLARE
    connection_record record;
    refresh_record record;
    generation_record record;
    observed_at timestamptz := clock_timestamp();
BEGIN
    IF SESSION_USER <> 'schemabridge_catalog' THEN
        RAISE EXCEPTION 'catalog completion lock role is invalid'
            USING ERRCODE = '42501';
    END IF;
    IF (
        p_workspace_id IS NULL
        OR length(trim(p_workspace_id)) NOT BETWEEN 1 AND 200
        OR octet_length(p_workspace_id) > 200
        OR p_connection_id !~ '^[a-z][a-z0-9_-]{2,199}$'
        OR p_refresh_id !~ '^[a-z][a-z0-9_-]{2,199}$'
        OR p_target_generation IS NULL
        OR p_target_generation <= 0
        OR p_fencing_token IS NULL
        OR p_fencing_token <= 0
        OR p_lease_capability_digest !~ '^[0-9a-f]{64}$'
    ) THEN
        RAISE EXCEPTION 'catalog completion lock input is invalid'
            USING ERRCODE = '22023';
    END IF;

    SELECT status, active_generation
    INTO connection_record
    FROM schemabridge_control.catalog_connections
    WHERE workspace_id = p_workspace_id
      AND connection_id = p_connection_id
    FOR UPDATE;
    IF (
        connection_record.status IS NULL
        OR connection_record.status <> 'enabled'
    ) THEN
        RAISE EXCEPTION 'catalog completion ownership is invalid'
            USING ERRCODE = '55000';
    END IF;

    SELECT
        refresh_id,
        status,
        base_generation,
        target_generation,
        fencing_token,
        lease_capability_digest,
        lease_expires_at,
        source_complete
    INTO refresh_record
    FROM schemabridge_control.catalog_refresh_runs
    WHERE workspace_id = p_workspace_id
      AND connection_id = p_connection_id
      AND refresh_id = p_refresh_id
    FOR UPDATE;
    IF (
        refresh_record.refresh_id IS NULL
        OR refresh_record.status <> 'staging'
        OR refresh_record.target_generation <> p_target_generation
        OR refresh_record.fencing_token <> p_fencing_token
        OR refresh_record.lease_capability_digest
            <> p_lease_capability_digest
        OR refresh_record.lease_expires_at <= observed_at
        OR NOT refresh_record.source_complete
        OR connection_record.active_generation
            IS DISTINCT FROM refresh_record.base_generation
    ) THEN
        RAISE EXCEPTION 'catalog completion ownership is invalid'
            USING ERRCODE = '55000';
    END IF;

    SELECT generation, refresh_id, status, base_generation
    INTO generation_record
    FROM schemabridge_control.catalog_generations
    WHERE workspace_id = p_workspace_id
      AND connection_id = p_connection_id
      AND generation = p_target_generation
    FOR UPDATE;
    IF (
        generation_record.generation IS NULL
        OR generation_record.refresh_id <> p_refresh_id
        OR generation_record.status <> 'staging'
        OR generation_record.base_generation
            IS DISTINCT FROM refresh_record.base_generation
    ) THEN
        RAISE EXCEPTION 'catalog completion ownership is invalid'
            USING ERRCODE = '55000';
    END IF;
END;
$$;

CREATE FUNCTION schemabridge_control.activate_catalog_generation(
    p_workspace_id varchar,
    p_connection_id varchar,
    p_refresh_id varchar,
    p_expected_base_generation bigint,
    p_target_generation bigint,
    p_fencing_token bigint,
    p_lease_capability_digest char(64),
    p_inventory_fingerprint char(64)
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
    refresh_record record;
    connection_record record;
    policy_record record;
    generation_record record;
    actual_asset_count bigint;
    actual_field_count bigint;
    other_active_asset_count bigint;
    other_active_field_count bigint;
    completed_time timestamptz := clock_timestamp();
    changed_rows integer;
BEGIN
    IF SESSION_USER NOT IN ('schemabridge_catalog', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'catalog generation activation role is invalid'
            USING ERRCODE = '42501';
    END IF;
    IF (
        p_workspace_id IS NULL
        OR length(trim(p_workspace_id)) NOT BETWEEN 1 AND 200
        OR octet_length(p_workspace_id) > 200
        OR p_connection_id !~ '^[a-z][a-z0-9_-]{2,199}$'
        OR p_refresh_id !~ '^[a-z][a-z0-9_-]{2,199}$'
        OR p_target_generation IS NULL
        OR p_target_generation <= 0
        OR p_fencing_token IS NULL
        OR p_fencing_token <= 0
        OR p_lease_capability_digest !~ '^[0-9a-f]{64}$'
        OR p_inventory_fingerprint !~ '^[0-9a-f]{64}$'
    ) THEN
        RAISE EXCEPTION 'catalog generation activation input is invalid'
            USING ERRCODE = '22023';
    END IF;

    SELECT *
    INTO connection_record
    FROM schemabridge_control.catalog_connections
    WHERE workspace_id = p_workspace_id
      AND connection_id = p_connection_id
    FOR UPDATE;
    IF (
        connection_record.connection_id IS NULL
        OR connection_record.status <> 'enabled'
        OR connection_record.active_generation
            IS DISTINCT FROM p_expected_base_generation
    ) THEN
        RAISE EXCEPTION 'catalog generation compare-and-swap failed'
            USING ERRCODE = '40001';
    END IF;

    SELECT *
    INTO refresh_record
    FROM schemabridge_control.catalog_refresh_runs
    WHERE workspace_id = p_workspace_id
      AND connection_id = p_connection_id
      AND refresh_id = p_refresh_id
    FOR UPDATE;
    IF (
        refresh_record.refresh_id IS NULL
        OR refresh_record.status <> 'staging'
        OR refresh_record.base_generation
            IS DISTINCT FROM p_expected_base_generation
        OR refresh_record.target_generation <> p_target_generation
        OR refresh_record.fencing_token <> p_fencing_token
        OR refresh_record.lease_capability_digest
            <> p_lease_capability_digest
        OR refresh_record.lease_expires_at <= completed_time
        OR NOT refresh_record.source_complete
    ) THEN
        RAISE EXCEPTION 'catalog refresh completion ownership is invalid'
            USING ERRCODE = '55000';
    END IF;

    SELECT *
    INTO generation_record
    FROM schemabridge_control.catalog_generations
    WHERE workspace_id = p_workspace_id
      AND connection_id = p_connection_id
      AND generation = p_target_generation
    FOR UPDATE;
    IF (
        generation_record.generation IS NULL
        OR generation_record.status <> 'staging'
        OR generation_record.refresh_id <> p_refresh_id
        OR generation_record.base_generation
            IS DISTINCT FROM p_expected_base_generation
        OR generation_record.refresh_mode <> refresh_record.refresh_mode
    ) THEN
        RAISE EXCEPTION 'catalog staging generation is invalid'
            USING ERRCODE = '55000';
    END IF;

    SELECT *
    INTO policy_record
    FROM schemabridge_control.tenant_capacity_policies
    WHERE workspace_id = p_workspace_id
    FOR UPDATE;
    IF policy_record.workspace_id IS NULL THEN
        RAISE EXCEPTION 'tenant capacity policy is unavailable'
            USING ERRCODE = '55000';
    END IF;

    SELECT count(*)
    INTO actual_asset_count
    FROM schemabridge_control.catalog_assets
    WHERE workspace_id = p_workspace_id
      AND connection_id = p_connection_id
      AND generation = p_target_generation;
    SELECT count(*)
    INTO actual_field_count
    FROM schemabridge_control.catalog_fields
    WHERE workspace_id = p_workspace_id
      AND connection_id = p_connection_id
      AND generation = p_target_generation;

    SELECT
        coalesce(sum(generation.asset_count), 0),
        coalesce(sum(generation.field_count), 0)
    INTO other_active_asset_count, other_active_field_count
    FROM schemabridge_control.catalog_connections AS active_connection
    JOIN schemabridge_control.catalog_generations AS generation
      ON generation.workspace_id = active_connection.workspace_id
     AND generation.connection_id = active_connection.connection_id
     AND generation.generation = active_connection.active_generation
    WHERE active_connection.workspace_id = p_workspace_id
      AND active_connection.connection_id <> p_connection_id
      AND active_connection.status = 'enabled';

    IF (
        actual_asset_count <> refresh_record.staged_asset_count
        OR actual_field_count <> refresh_record.staged_field_count
    ) THEN
        RAISE EXCEPTION 'catalog generation counts are invalid'
            USING ERRCODE = '55000';
    END IF;

    IF (
        actual_asset_count + other_active_asset_count > policy_record.asset_limit
        OR actual_field_count + other_active_field_count > policy_record.field_limit
    ) THEN
        RAISE EXCEPTION 'catalog generation exceeds tenant capacity'
            USING ERRCODE = 'P2501';
    END IF;

    IF EXISTS (
        WITH field_count AS MATERIALIZED (
            SELECT
                field.asset_key,
                count(*)::integer AS actual_count
            FROM schemabridge_control.catalog_fields AS field
            WHERE field.workspace_id = p_workspace_id
              AND field.connection_id = p_connection_id
              AND field.generation = p_target_generation
            GROUP BY field.asset_key
        )
        SELECT 1
        FROM schemabridge_control.catalog_assets AS asset
        LEFT JOIN field_count
          ON field_count.asset_key = asset.asset_key
        WHERE asset.workspace_id = p_workspace_id
          AND asset.connection_id = p_connection_id
          AND asset.generation = p_target_generation
          AND asset.field_count <> coalesce(field_count.actual_count, 0)
    ) THEN
        RAISE EXCEPTION 'catalog asset field counts are invalid'
            USING ERRCODE = '55000';
    END IF;

    IF (
        refresh_record.refresh_mode = 'full'
        AND p_expected_base_generation IS NOT NULL
    ) THEN
        INSERT INTO schemabridge_control.catalog_tombstones (
            workspace_id,
            connection_id,
            observed_missing_in_generation,
            resource_kind,
            asset_key,
            asset_id,
            resource_key,
            field_key,
            field_path,
            removed_from_generation,
            prior_metadata_fingerprint,
            removal_reason,
            observed_at
        )
        SELECT
            previous.workspace_id,
            previous.connection_id,
            p_target_generation,
            'asset',
            previous.asset_key,
            previous.asset_id,
            previous.asset_key,
            NULL,
            NULL,
            p_expected_base_generation,
            previous.metadata_fingerprint,
            'full_reconciliation_absence',
            completed_time
        FROM schemabridge_control.catalog_assets AS previous
        WHERE previous.workspace_id = p_workspace_id
          AND previous.connection_id = p_connection_id
          AND previous.generation = p_expected_base_generation
          AND NOT EXISTS (
              SELECT 1
              FROM schemabridge_control.catalog_assets AS current
              WHERE current.workspace_id = previous.workspace_id
                AND current.connection_id = previous.connection_id
                AND current.generation = p_target_generation
                AND current.asset_key = previous.asset_key
          )
        ON CONFLICT DO NOTHING;

        INSERT INTO schemabridge_control.catalog_tombstones (
            workspace_id,
            connection_id,
            observed_missing_in_generation,
            resource_kind,
            asset_key,
            asset_id,
            resource_key,
            field_key,
            field_path,
            removed_from_generation,
            prior_metadata_fingerprint,
            removal_reason,
            observed_at
        )
        SELECT
            previous.workspace_id,
            previous.connection_id,
            p_target_generation,
            'field',
            previous.asset_key,
            previous_asset.asset_id,
            previous.field_key,
            previous.field_key,
            previous.field_path,
            p_expected_base_generation,
            previous.metadata_fingerprint,
            'full_reconciliation_absence',
            completed_time
        FROM schemabridge_control.catalog_fields AS previous
        JOIN schemabridge_control.catalog_assets AS previous_asset
          ON previous_asset.workspace_id = previous.workspace_id
         AND previous_asset.connection_id = previous.connection_id
         AND previous_asset.generation = previous.generation
         AND previous_asset.asset_key = previous.asset_key
        WHERE previous.workspace_id = p_workspace_id
          AND previous.connection_id = p_connection_id
          AND previous.generation = p_expected_base_generation
          AND NOT EXISTS (
              SELECT 1
              FROM schemabridge_control.catalog_fields AS current
              WHERE current.workspace_id = previous.workspace_id
                AND current.connection_id = previous.connection_id
                AND current.generation = p_target_generation
                AND current.asset_key = previous.asset_key
                AND current.field_key = previous.field_key
          )
        ON CONFLICT DO NOTHING;
    END IF;

    UPDATE schemabridge_control.catalog_generations
    SET status = 'completed',
        asset_count = actual_asset_count,
        field_count = actual_field_count,
        inventory_fingerprint = p_inventory_fingerprint,
        completed_at = completed_time,
        retain_until = completed_time
            + make_interval(secs => policy_record.generation_retention_seconds)
    WHERE workspace_id = p_workspace_id
      AND connection_id = p_connection_id
      AND generation = p_target_generation
      AND status = 'staging';
    GET DIAGNOSTICS changed_rows = ROW_COUNT;
    IF changed_rows <> 1 THEN
        RAISE EXCEPTION 'catalog generation completion lost ownership'
            USING ERRCODE = '40001';
    END IF;

    IF p_expected_base_generation IS NOT NULL THEN
        UPDATE schemabridge_control.catalog_generations
        SET retain_until = greatest(
            retain_until,
            completed_time
                + make_interval(secs => policy_record.catalog_cursor_ttl_seconds)
        )
        WHERE workspace_id = p_workspace_id
          AND connection_id = p_connection_id
          AND generation = p_expected_base_generation
          AND status = 'completed';
        GET DIAGNOSTICS changed_rows = ROW_COUNT;
        IF changed_rows <> 1 THEN
            RAISE EXCEPTION 'catalog base generation retention is unavailable'
                USING ERRCODE = '55000';
        END IF;
    END IF;

    UPDATE schemabridge_control.catalog_connections
    SET active_generation = p_target_generation,
        active_generation_fingerprint = p_inventory_fingerprint,
        active_generation_completed_at = completed_time,
        updated_at = completed_time
    WHERE workspace_id = p_workspace_id
      AND connection_id = p_connection_id
      AND status = 'enabled'
      AND active_generation IS NOT DISTINCT FROM p_expected_base_generation;
    GET DIAGNOSTICS changed_rows = ROW_COUNT;
    IF changed_rows <> 1 THEN
        RAISE EXCEPTION 'catalog generation compare-and-swap failed'
            USING ERRCODE = '40001';
    END IF;

    UPDATE schemabridge_control.catalog_refresh_runs
    SET status = 'completed',
        lease_capability_digest = NULL,
        lease_expires_at = NULL,
        inventory_fingerprint = p_inventory_fingerprint,
        updated_at = completed_time,
        completed_at = completed_time
    WHERE workspace_id = p_workspace_id
      AND connection_id = p_connection_id
      AND refresh_id = p_refresh_id
      AND status = 'staging';
    GET DIAGNOSTICS changed_rows = ROW_COUNT;
    IF changed_rows <> 1 THEN
        RAISE EXCEPTION 'catalog refresh completion lost ownership'
            USING ERRCODE = '40001';
    END IF;

    RETURN QUERY SELECT actual_asset_count, actual_field_count;
END;
$$;

CREATE FUNCTION schemabridge_control.prune_catalog_generations(
    p_workspace_id varchar,
    p_connection_id varchar,
    p_completed_before timestamptz,
    p_limit integer
)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
DECLARE
    removed_count integer;
BEGIN
    IF SESSION_USER NOT IN ('schemabridge_catalog', 'schemabridge_migrator') THEN
        RAISE EXCEPTION 'catalog retention role is invalid'
            USING ERRCODE = '42501';
    END IF;
    IF (
        p_workspace_id IS NULL
        OR length(trim(p_workspace_id)) NOT BETWEEN 1 AND 200
        OR octet_length(p_workspace_id) > 200
        OR p_connection_id !~ '^[a-z][a-z0-9_-]{2,199}$'
        OR p_completed_before IS NULL
        OR p_limit NOT BETWEEN 1 AND 100
    ) THEN
        RAISE EXCEPTION 'catalog retention input is invalid'
            USING ERRCODE = '22023';
    END IF;

    WITH removable AS (
        SELECT generation.workspace_id,
               generation.connection_id,
               generation.generation
        FROM schemabridge_control.catalog_generations AS generation
        JOIN schemabridge_control.catalog_connections AS connection
          ON connection.workspace_id = generation.workspace_id
         AND connection.connection_id = generation.connection_id
        WHERE generation.workspace_id = p_workspace_id
          AND generation.connection_id = p_connection_id
          AND generation.generation IS DISTINCT FROM connection.active_generation
          AND (
              (
                  generation.status = 'completed'
                  AND generation.completed_at < p_completed_before
                  AND generation.retain_until < clock_timestamp()
              )
              OR
              (
                  generation.status = 'staging'
                  AND EXISTS (
                      SELECT 1
                      FROM schemabridge_control.catalog_refresh_runs AS failed_refresh
                      WHERE failed_refresh.workspace_id = generation.workspace_id
                        AND failed_refresh.connection_id = generation.connection_id
                        AND failed_refresh.refresh_id = generation.refresh_id
                        AND failed_refresh.status = 'failed'
                        AND failed_refresh.completed_at < p_completed_before
                  )
              )
          )
          AND NOT EXISTS (
              SELECT 1
              FROM schemabridge_control.catalog_refresh_runs AS refresh
              WHERE refresh.workspace_id = generation.workspace_id
                AND refresh.connection_id = generation.connection_id
                AND refresh.status IN ('requested', 'leased', 'staging')
                AND (
                    refresh.base_generation = generation.generation
                    OR refresh.target_generation = generation.generation
                )
          )
        ORDER BY generation.retain_until, generation.generation
        FOR UPDATE OF generation SKIP LOCKED
        LIMIT p_limit
    )
    DELETE FROM schemabridge_control.catalog_generations AS generation
    USING removable
    WHERE generation.workspace_id = removable.workspace_id
      AND generation.connection_id = removable.connection_id
      AND generation.generation = removable.generation;
    GET DIAGNOSTICS removed_count = ROW_COUNT;
    RETURN removed_count;
END;
$$;

CREATE FUNCTION schemabridge_control.prune_catalog_generations(
    p_workspace_id varchar,
    p_connection_id varchar,
    p_limit integer
)
RETURNS integer
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
    SELECT schemabridge_control.prune_catalog_generations(
        p_workspace_id,
        p_connection_id,
        clock_timestamp(),
        p_limit
    );
$$;

CREATE TRIGGER tenant_capacity_policies_guard
BEFORE INSERT OR UPDATE OR DELETE
ON schemabridge_control.tenant_capacity_policies
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.guard_tenant_capacity_policy();

CREATE TRIGGER tenant_capacity_policy_revisions_immutable
BEFORE UPDATE OR DELETE
ON schemabridge_control.tenant_capacity_policy_revisions
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.reject_catalog_immutable_mutation();

CREATE TRIGGER catalog_connections_guard
BEFORE INSERT OR UPDATE OR DELETE
ON schemabridge_control.catalog_connections
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.guard_catalog_connection();

CREATE TRIGGER catalog_connection_routes_immutable
BEFORE UPDATE OR DELETE
ON schemabridge_control.catalog_connection_routes
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.reject_catalog_immutable_mutation();

CREATE TRIGGER catalog_refresh_runs_lifecycle
BEFORE INSERT OR UPDATE OR DELETE
ON schemabridge_control.catalog_refresh_runs
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.guard_catalog_refresh_lifecycle();

CREATE TRIGGER catalog_generations_lifecycle
BEFORE INSERT OR UPDATE OR DELETE
ON schemabridge_control.catalog_generations
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.guard_catalog_generation_lifecycle();

CREATE TRIGGER catalog_assets_staging_only
BEFORE INSERT OR UPDATE OR DELETE
ON schemabridge_control.catalog_assets
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.guard_catalog_generation_content();

CREATE TRIGGER catalog_assets_identity
BEFORE UPDATE
ON schemabridge_control.catalog_assets
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.guard_catalog_asset_identity();

CREATE TRIGGER catalog_fields_staging_only
BEFORE INSERT OR UPDATE OR DELETE
ON schemabridge_control.catalog_fields
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.guard_catalog_generation_content();

CREATE TRIGGER catalog_fields_identity
BEFORE UPDATE
ON schemabridge_control.catalog_fields
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.guard_catalog_field_identity();

CREATE TRIGGER catalog_tombstones_immutable
BEFORE INSERT OR UPDATE OR DELETE
ON schemabridge_control.catalog_tombstones
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.guard_catalog_tombstone();

CREATE TRIGGER execution_jobs_capacity
AFTER INSERT OR UPDATE
ON schemabridge_control.execution_jobs
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.maintain_execution_job_capacity();

REVOKE ALL ON
    schemabridge_control.tenant_capacity_policies,
    schemabridge_control.tenant_capacity_policy_revisions,
    schemabridge_control.api_rate_limit_windows,
    schemabridge_control.tenant_execution_capacity,
    schemabridge_control.tenant_job_schedule,
    schemabridge_control.catalog_connections,
    schemabridge_control.catalog_connection_routes,
    schemabridge_control.catalog_refresh_runs,
    schemabridge_control.catalog_generations,
    schemabridge_control.catalog_assets,
    schemabridge_control.catalog_fields,
    schemabridge_control.catalog_tombstones
    FROM PUBLIC;

REVOKE ALL ON FUNCTION
    schemabridge_control.catalog_terms_text(varchar[]),
    schemabridge_control.guard_tenant_capacity_policy(),
    schemabridge_control.apply_tenant_capacity_policy(
        varchar,
        bigint,
        integer,
        bigint,
        bigint,
        integer,
        integer,
        integer,
        varchar,
        varchar
    ),
    schemabridge_control.admit_catalog_connection(varchar),
    schemabridge_control.request_catalog_refresh(
        varchar,
        varchar,
        varchar,
        varchar,
        char,
        char,
        varchar,
        timestamptz
    ),
    schemabridge_control.load_catalog_refresh_public(varchar, varchar),
    schemabridge_control.load_owned_catalog_connection_route(
        varchar,
        varchar,
        varchar,
        varchar,
        varchar,
        bigint
    ),
    schemabridge_control.guard_catalog_connection(),
    schemabridge_control.reject_catalog_immutable_mutation(),
    schemabridge_control.guard_catalog_refresh_lifecycle(),
    schemabridge_control.guard_catalog_generation_lifecycle(),
    schemabridge_control.guard_catalog_generation_content(),
    schemabridge_control.guard_catalog_asset_identity(),
    schemabridge_control.guard_catalog_field_identity(),
    schemabridge_control.guard_catalog_tombstone(),
    schemabridge_control.maintain_execution_job_capacity(),
    schemabridge_control.admit_api_request(varchar, char, varchar),
    schemabridge_control.lock_catalog_completion_scope(
        varchar,
        varchar,
        varchar,
        bigint,
        bigint,
        char
    ),
    schemabridge_control.activate_catalog_generation(
        varchar,
        varchar,
        varchar,
        bigint,
        bigint,
        bigint,
        char,
        char
    ),
    schemabridge_control.prune_catalog_generations(
        varchar,
        varchar,
        timestamptz,
        integer
    ),
    schemabridge_control.prune_catalog_generations(varchar, varchar, integer)
    FROM PUBLIC;

GRANT SELECT ON schemabridge_control.schema_migrations
    TO schemabridge_catalog;

GRANT SELECT ON
    schemabridge_control.tenant_capacity_policies
    TO schemabridge_api;

GRANT SELECT, INSERT ON
    schemabridge_control.catalog_connections
    TO schemabridge_api;

GRANT UPDATE (
    status,
    disabled_by_actor_id,
    disabled_idempotency_digest,
    disabled_at,
    updated_at
) ON schemabridge_control.catalog_connections
    TO schemabridge_api;

GRANT INSERT ON
    schemabridge_control.catalog_connection_routes
    TO schemabridge_api;

GRANT SELECT ON
    schemabridge_control.catalog_generations,
    schemabridge_control.catalog_assets,
    schemabridge_control.catalog_fields,
    schemabridge_control.tenant_execution_capacity,
    schemabridge_control.tenant_job_schedule
    TO schemabridge_api;

GRANT SELECT ON
    schemabridge_control.tenant_capacity_policies,
    schemabridge_control.tenant_execution_capacity
    TO schemabridge_worker;

GRANT SELECT, INSERT ON
    schemabridge_control.tenant_job_schedule
    TO schemabridge_worker;

GRANT UPDATE (
    last_claimed_at,
    claim_sequence,
    updated_at
) ON schemabridge_control.tenant_job_schedule
    TO schemabridge_worker;

GRANT SELECT ON
    schemabridge_control.tenant_capacity_policies,
    schemabridge_control.catalog_connections,
    schemabridge_control.catalog_refresh_runs,
    schemabridge_control.catalog_generations,
    schemabridge_control.catalog_assets,
    schemabridge_control.catalog_fields,
    schemabridge_control.catalog_tombstones
    TO schemabridge_catalog;

GRANT UPDATE ON
    schemabridge_control.catalog_refresh_runs
    TO schemabridge_catalog;

GRANT INSERT ON
    schemabridge_control.catalog_generations,
    schemabridge_control.catalog_tombstones
    TO schemabridge_catalog;

GRANT INSERT, UPDATE, DELETE ON
    schemabridge_control.catalog_assets,
    schemabridge_control.catalog_fields
    TO schemabridge_catalog;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.catalog_terms_text(varchar[])
    TO schemabridge_api, schemabridge_catalog, schemabridge_migrator;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.guard_tenant_capacity_policy()
    TO schemabridge_migrator;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.apply_tenant_capacity_policy(
        varchar,
        bigint,
        integer,
        bigint,
        bigint,
        integer,
        integer,
        integer,
        varchar,
        varchar
    )
    TO schemabridge_migrator;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.admit_catalog_connection(varchar)
    TO schemabridge_api, schemabridge_migrator;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.request_catalog_refresh(
        varchar,
        varchar,
        varchar,
        varchar,
        char,
        char,
        varchar,
        timestamptz
    ),
    schemabridge_control.load_catalog_refresh_public(varchar, varchar)
    TO schemabridge_api, schemabridge_migrator;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.load_owned_catalog_connection_route(
        varchar,
        varchar,
        varchar,
        varchar,
        varchar,
        bigint
    )
    TO schemabridge_catalog, schemabridge_migrator;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.guard_catalog_connection()
    TO schemabridge_api, schemabridge_migrator;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.reject_catalog_immutable_mutation()
    TO schemabridge_api, schemabridge_catalog, schemabridge_migrator;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.guard_catalog_refresh_lifecycle()
    TO schemabridge_api, schemabridge_catalog, schemabridge_migrator;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.guard_catalog_generation_lifecycle(),
    schemabridge_control.guard_catalog_generation_content(),
    schemabridge_control.guard_catalog_asset_identity(),
    schemabridge_control.guard_catalog_field_identity(),
    schemabridge_control.guard_catalog_tombstone()
    TO schemabridge_catalog, schemabridge_migrator;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.maintain_execution_job_capacity()
    TO schemabridge_api, schemabridge_worker, schemabridge_migrator;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.admit_api_request(varchar, char, varchar)
    TO schemabridge_api, schemabridge_migrator;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.lock_catalog_completion_scope(
        varchar,
        varchar,
        varchar,
        bigint,
        bigint,
        char
    )
    TO schemabridge_catalog;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.activate_catalog_generation(
        varchar,
        varchar,
        varchar,
        bigint,
        bigint,
        bigint,
        char,
        char
    ),
    schemabridge_control.prune_catalog_generations(
        varchar,
        varchar,
        timestamptz,
        integer
    ),
    schemabridge_control.prune_catalog_generations(varchar, varchar, integer)
    TO schemabridge_catalog, schemabridge_migrator;
