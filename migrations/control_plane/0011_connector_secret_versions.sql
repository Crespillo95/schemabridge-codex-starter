CREATE TABLE schemabridge_control.connector_private_route_secret_versions (
    workspace_id varchar(200) NOT NULL,
    connection_id varchar(200) NOT NULL,
    route_revision bigint NOT NULL CHECK (route_revision >= 1),
    capability varchar(16) NOT NULL
        CHECK (capability IN ('preflight', 'catalog', 'execution', 'profile')),
    provider_secret_version bigint NOT NULL
        CHECK (
            provider_secret_version BETWEEN 1 AND 9223372036854775807
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
    CONSTRAINT connector_private_route_secret_version_fk
        FOREIGN KEY (
            workspace_id,
            connection_id,
            route_revision,
            capability
        )
        REFERENCES schemabridge_control.connector_private_route_revisions (
            workspace_id,
            connection_id,
            route_revision,
            capability
        )
);

CREATE TRIGGER connector_private_route_secret_versions_immutable
BEFORE UPDATE OR DELETE
ON schemabridge_control.connector_private_route_secret_versions
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.reject_connector_immutable_mutation();

CREATE FUNCTION schemabridge_control.require_connector_private_secret_version()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM schemabridge_control.connector_private_route_secret_versions AS secret_version
        WHERE secret_version.workspace_id = NEW.workspace_id
          AND secret_version.connection_id = NEW.connection_id
          AND secret_version.route_revision = NEW.route_revision
          AND secret_version.capability = NEW.capability
    ) THEN
        RAISE EXCEPTION 'connector private route secret version is missing'
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER connector_private_route_secret_version_required
AFTER INSERT
ON schemabridge_control.connector_private_route_revisions
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION schemabridge_control.require_connector_private_secret_version();

CREATE FUNCTION schemabridge_control.apply_connector_route_change_v2(
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
    p_preflight_secret_version bigint,
    p_catalog_secret_version bigint,
    p_execution_secret_version bigint,
    p_profile_secret_version bigint,
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
    applied record;
    exact_version_count integer;
BEGIN
    IF SESSION_USER <> 'schemabridge_migrator' THEN
        RAISE EXCEPTION 'connector route operator role is invalid'
            USING ERRCODE = '42501';
    END IF;
    IF (
        p_operation IN ('create', 'rotate')
        AND (
            p_preflight_secret_version IS NULL
            OR p_preflight_secret_version NOT BETWEEN 1 AND 9223372036854775807
            OR p_catalog_secret_version IS NULL
            OR p_catalog_secret_version NOT BETWEEN 1 AND 9223372036854775807
            OR p_execution_secret_version IS NULL
            OR p_execution_secret_version NOT BETWEEN 1 AND 9223372036854775807
            OR p_profile_secret_version IS NULL
            OR p_profile_secret_version NOT BETWEEN 1 AND 9223372036854775807
        )
    ) OR (
        p_operation = 'disable'
        AND (
            p_preflight_secret_version IS NOT NULL
            OR p_catalog_secret_version IS NOT NULL
            OR p_execution_secret_version IS NOT NULL
            OR p_profile_secret_version IS NOT NULL
        )
    ) THEN
        RAISE EXCEPTION 'connector provider secret versions are invalid'
            USING ERRCODE = '22023';
    END IF;

    SELECT *
    INTO STRICT applied
    FROM schemabridge_control.apply_connector_route_change(
        p_workspace_id,
        p_connection_id,
        p_operation,
        p_expected_head_revision,
        p_contract_version,
        p_route_revision,
        p_route_fingerprint,
        p_expected_reader,
        p_type_contract_version,
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
        p_cost_budget_fingerprint,
        p_contract_fingerprint,
        p_target_fingerprint,
        p_preflight_binding_ref,
        p_catalog_binding_ref,
        p_execution_binding_ref,
        p_profile_binding_ref,
        p_proposal_fingerprint,
        p_approval_id,
        p_approval_fingerprint,
        p_actor_id,
        p_idempotency_digest,
        p_audit_id,
        p_audit_fingerprint,
        p_head_fingerprint,
        p_confirmation
    );

    IF p_operation IN ('create', 'rotate') THEN
        INSERT INTO
            schemabridge_control.connector_private_route_secret_versions (
                workspace_id,
                connection_id,
                route_revision,
                capability,
                provider_secret_version,
                created_by_actor_id,
                created_at
            )
        VALUES
            (
                p_workspace_id,
                p_connection_id,
                p_route_revision,
                'preflight',
                p_preflight_secret_version,
                p_actor_id,
                clock_timestamp()
            ),
            (
                p_workspace_id,
                p_connection_id,
                p_route_revision,
                'catalog',
                p_catalog_secret_version,
                p_actor_id,
                clock_timestamp()
            ),
            (
                p_workspace_id,
                p_connection_id,
                p_route_revision,
                'execution',
                p_execution_secret_version,
                p_actor_id,
                clock_timestamp()
            ),
            (
                p_workspace_id,
                p_connection_id,
                p_route_revision,
                'profile',
                p_profile_secret_version,
                p_actor_id,
                clock_timestamp()
            )
        ON CONFLICT DO NOTHING;

        SELECT count(*)::integer
        INTO exact_version_count
        FROM schemabridge_control.connector_private_route_secret_versions AS secret_version
        WHERE secret_version.workspace_id = p_workspace_id
          AND secret_version.connection_id = p_connection_id
          AND secret_version.route_revision = p_route_revision
          AND (
              (
                  secret_version.capability = 'preflight'
                  AND secret_version.provider_secret_version
                        = p_preflight_secret_version
              )
              OR (
                  secret_version.capability = 'catalog'
                  AND secret_version.provider_secret_version
                        = p_catalog_secret_version
              )
              OR (
                  secret_version.capability = 'execution'
                  AND secret_version.provider_secret_version
                        = p_execution_secret_version
              )
              OR (
                  secret_version.capability = 'profile'
                  AND secret_version.provider_secret_version
                        = p_profile_secret_version
              )
          );
        IF exact_version_count <> 4 THEN
            RAISE EXCEPTION 'connector provider secret version conflict'
                USING ERRCODE = '23505';
        END IF;
    END IF;

    RETURN QUERY
    SELECT
        applied.workspace_id,
        applied.connection_id,
        applied.head_revision,
        applied.contract_version,
        applied.route_revision,
        applied.route_fingerprint,
        applied.target_fingerprint,
        applied.status,
        applied.audit_id;
END;
$$;

CREATE FUNCTION schemabridge_control.load_current_preflight_connector_route_v2(
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
    credential_binding_ref varchar(200),
    provider_secret_version bigint
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
BEGIN
    RETURN QUERY
    SELECT
        route.workspace_id,
        route.connection_id,
        route.contract_version,
        route.route_revision,
        route.target_fingerprint,
        route.sql_dialect,
        route.expected_reader,
        route.source_identity_fingerprint,
        route.credential_binding_ref,
        secret_version.provider_secret_version
    FROM schemabridge_control.load_current_preflight_connector_route(
        p_workspace_id,
        p_connection_id,
        p_route_revision,
        p_target_fingerprint
    ) AS route
    JOIN schemabridge_control.connector_private_route_secret_versions AS secret_version
      ON secret_version.workspace_id = route.workspace_id
     AND secret_version.connection_id = route.connection_id
     AND secret_version.route_revision = route.route_revision
     AND secret_version.capability = 'preflight';
END;
$$;

CREATE FUNCTION schemabridge_control.load_owned_catalog_connector_route_v2(
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
    credential_binding_ref varchar(200),
    provider_secret_version bigint
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
BEGIN
    RETURN QUERY
    SELECT
        route.workspace_id,
        route.connection_id,
        route.source_kind,
        route.environment,
        route.catalog_scope,
        route.platform_instance,
        route.catalog_identity_fingerprint,
        route.contract_version,
        route.route_revision,
        route.target_fingerprint,
        route.credential_binding_ref,
        secret_version.provider_secret_version
    FROM schemabridge_control.load_owned_catalog_connector_route(
        p_workspace_id,
        p_connection_id,
        p_refresh_id,
        p_indexer_id,
        p_lease_capability,
        p_fencing_token,
        p_contract_version,
        p_route_revision,
        p_target_fingerprint
    ) AS route
    JOIN schemabridge_control.connector_private_route_secret_versions AS secret_version
      ON secret_version.workspace_id = route.workspace_id
     AND secret_version.connection_id = route.connection_id
     AND secret_version.route_revision = route.route_revision
     AND secret_version.capability = 'catalog';
END;
$$;

CREATE FUNCTION schemabridge_control.load_owned_execution_connector_route_v2(
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
    credential_binding_ref varchar(200),
    provider_secret_version bigint
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
BEGIN
    RETURN QUERY
    SELECT
        route.job_workspace_id,
        route.connector_workspace_id,
        route.job_id,
        route.connection_id,
        route.contract_version,
        route.route_revision,
        route.target_fingerprint,
        route.sql_dialect,
        route.expected_reader,
        route.source_identity_fingerprint,
        route.credential_binding_ref,
        secret_version.provider_secret_version
    FROM schemabridge_control.load_owned_execution_connector_route(
        p_job_workspace_id,
        p_connector_workspace_id,
        p_job_id,
        p_worker_id,
        p_lease_capability,
        p_fencing_token,
        p_connection_id,
        p_contract_version,
        p_route_revision,
        p_target_fingerprint
    ) AS route
    JOIN schemabridge_control.connector_private_route_secret_versions AS secret_version
      ON secret_version.workspace_id = route.connector_workspace_id
     AND secret_version.connection_id = route.connection_id
     AND secret_version.route_revision = route.route_revision
     AND secret_version.capability = 'execution';
END;
$$;

CREATE FUNCTION schemabridge_control.load_owned_profile_connector_route_v2(
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
    credential_binding_ref varchar(200),
    provider_secret_version bigint
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, schemabridge_control
AS $$
BEGIN
    RETURN QUERY
    SELECT
        route.workspace_id,
        route.job_id,
        route.connection_id,
        route.contract_version,
        route.route_revision,
        route.target_fingerprint,
        route.sql_dialect,
        route.expected_reader,
        route.source_identity_fingerprint,
        route.credential_binding_ref,
        secret_version.provider_secret_version
    FROM schemabridge_control.load_owned_profile_connector_route(
        p_workspace_id,
        p_job_id,
        p_worker_id,
        p_lease_capability,
        p_fencing_token,
        p_connection_id,
        p_contract_version,
        p_route_revision,
        p_target_fingerprint
    ) AS route
    JOIN schemabridge_control.connector_private_route_secret_versions AS secret_version
      ON secret_version.workspace_id = route.workspace_id
     AND secret_version.connection_id = route.connection_id
     AND secret_version.route_revision = route.route_revision
     AND secret_version.capability = 'profile';
END;
$$;

REVOKE ALL PRIVILEGES
    ON schemabridge_control.connector_private_route_secret_versions
    FROM PUBLIC;

REVOKE EXECUTE ON FUNCTION
    schemabridge_control.require_connector_private_secret_version(),
    schemabridge_control.apply_connector_route_change_v2(
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
        bigint,
        bigint,
        bigint,
        bigint,
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
    schemabridge_control.load_current_preflight_connector_route_v2(
        varchar,
        varchar,
        bigint,
        char
    ),
    schemabridge_control.load_owned_catalog_connector_route_v2(
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
    schemabridge_control.load_owned_execution_connector_route_v2(
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
    schemabridge_control.load_owned_profile_connector_route_v2(
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

REVOKE EXECUTE ON FUNCTION
    schemabridge_control.load_current_preflight_connector_route(
        varchar,
        varchar,
        bigint,
        char
    )
    FROM schemabridge_runtime;

REVOKE EXECUTE ON FUNCTION
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
    FROM schemabridge_catalog;

REVOKE EXECUTE ON FUNCTION
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
    FROM schemabridge_worker;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.apply_connector_route_change_v2(
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
        bigint,
        bigint,
        bigint,
        bigint,
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
    schemabridge_control.load_current_preflight_connector_route_v2(
        varchar,
        varchar,
        bigint,
        char
    )
    TO schemabridge_runtime, schemabridge_migrator;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.load_owned_catalog_connector_route_v2(
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
    schemabridge_control.load_owned_execution_connector_route_v2(
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
    schemabridge_control.load_owned_profile_connector_route_v2(
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
